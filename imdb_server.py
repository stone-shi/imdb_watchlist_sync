import requests
from bs4 import BeautifulSoup
import json
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, BackgroundTasks
from typing import List, Optional, Dict
import argparse
import sys
import os
import time
import logging
import threading

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("imdb-server")

# Try to load version
VERSION = "unknown"
if os.path.exists("version.txt"):
    try:
        with open("version.txt", "r") as _f:
            VERSION = _f.read().strip()
    except Exception:
        pass

app = FastAPI(title="IMDb Watchlist Server for *arr")

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    logger.info(f"{request.method} {request.url.path} - {response.status_code} ({process_time:.2f}s)")
    return response

# Global cache
CACHE_DIR = "data"
CACHE_FILE = os.path.join(CACHE_DIR, "watchlist_cache.json")
# Track which users are currently being scraped to avoid duplicates
SCRAPE_LOCKS = set()

def get_user_id(input_str: str) -> str:
    """Extracts user ID from URL or returns the ID as is."""
    if "imdb.com/user/" in input_str:
        parts = input_str.split("imdb.com/user/")[1].split("/")
        return parts[0]
    return input_str

def scrape_imdb_watchlist(user_id: str):
    user_id = get_user_id(user_id)
    url = f"https://www.imdb.com/user/{user_id}/watchlist"
    
    if user_id in SCRAPE_LOCKS:
        logger.info(f"Scrape for {user_id} already in progress, skipping.")
        return None
    
    SCRAPE_LOCKS.add(user_id)
    logger.info(f"Starting scrape for {url} using Undetected Mode...")
    
    from seleniumbase import Driver
    import logging as sbase_logging
    sbase_logging.getLogger('seleniumbase').setLevel(sbase_logging.WARNING)
    
    driver = None
    items = []
    try:
        # Initialize Driver in Undetected Mode
        driver = Driver(uc=True, headless=True)
        driver.uc_open_with_reconnect(url, 6)
        
        # Check if we are still on the challenge page
        if "challenge" in driver.page_source.lower() or len(driver.page_source) < 5000:
            logger.info("Challenge page detected, retrying...")
            time.sleep(2)
            driver.uc_open_with_reconnect(url, 6)
            
        page_source = driver.page_source
        soup = BeautifulSoup(page_source, 'html.parser')
        
        script_tag = soup.find('script', id='__NEXT_DATA__')
        if not script_tag:
            logger.error("Could not find __NEXT_DATA__ script tag.")
        else:
            data = json.loads(script_tag.string)
            props = data.get('props', {})
            pageProps = props.get('pageProps', {})
            mainColumnData = pageProps.get('mainColumnData', {})
            
            edges = []
            if 'prefilteredTitleList' in mainColumnData:
                edges = mainColumnData['prefilteredTitleList'].get('edges', [])
            elif 'watchlistData' in mainColumnData:
                edges = mainColumnData['watchlistData'].get('edges', [])
            elif 'predefinedList' in mainColumnData:
                plist = mainColumnData['predefinedList']
                if 'titleListItemSearch' in plist:
                    edges = plist['titleListItemSearch'].get('edges', [])
                elif 'titleListConnection' in plist:
                    edges = plist['titleListConnection'].get('edges', [])
            
            if not edges:
                for val in mainColumnData.values():
                    if isinstance(val, dict) and 'edges' in val:
                        edges = val['edges']
                        break
            
            for item in edges:
                node = item.get('node', {})
                if not node or not node.get('id'):
                    node = item.get('listItem', {})
                
                imdb_id = node.get('id')
                title_node = node.get('titleText') or node.get('title')
                title = "Unknown"
                if isinstance(title_node, dict):
                    title = title_node.get('text', "Unknown")
                elif title_node:
                    title = str(title_node)
                
                year_node = node.get('releaseYear')
                year = None
                if isinstance(year_node, dict):
                    year = year_node.get('year')
                elif year_node:
                    year = str(year_node)
                
                type_node = node.get('titleType', {})
                title_type = type_node.get('id') if isinstance(type_node, dict) else str(type_node)
                    
                if imdb_id:
                    items.append({
                        "title": title,
                        "imdb": imdb_id,
                        "imdbId": imdb_id,
                        "imdb_id": imdb_id,
                        "tmdbId": None,
                        "year": year,
                        "type": title_type # e.g., 'movie', 'tvSeries', 'tvMiniSeries'
                    })
            
            logger.info(f"Successfully scraped {len(items)} items from IMDb for {user_id}.")
            
            # Save to cache if we got results
            if items:
                cache = load_cache()
                cache[user_id] = {
                    "timestamp": time.time(),
                    "items": items
                }
                save_cache(cache)
                
    except Exception as e:
        logger.error(f"Error during selenium scraping for {user_id}: {e}")
    finally:
        SCRAPE_LOCKS.discard(user_id)
        if driver:
            try:
                driver.quit()
            except:
                pass
    
    return items

def load_cache():
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}

def save_cache(cache):
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR)
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f)

@app.get("/")
def read_root():
    return {
        "status": "online", 
        "info": "IMDb Watchlist Server for *arr",
        "version": VERSION,
        "endpoints": {
            "/radarr?user_id=...": "Radarr compatible JSON list",
            "/sonarr?user_id=...": "Sonarr compatible JSON list",
            "/stats": "Get cache statistics",
            "/search?q=...": "Search across all cached watchlists"
        }
    }

@app.get("/watchlist")
def get_watchlist(background_tasks: BackgroundTasks, user_id: str = Query(...), force: bool = False):
    user_id = get_user_id(user_id)
    cache = load_cache()
    
    cached_entry = cache.get(user_id)
    is_stale = not cached_entry or (time.time() - cached_entry.get('timestamp', 0) > 3600)
    
    if force or is_stale:
        logger.info(f"Cache is stale or force=True for {user_id}. Triggering background scrape.")
        background_tasks.add_task(scrape_imdb_watchlist, user_id)
    
    if cached_entry:
        logger.info(f"Returning cached data for {user_id} ({len(cached_entry['items'])} items).")
        return cached_entry['items']
    
    # If no cache exists, we HAVE to wait for the first scrape
    logger.info(f"No cache exists for {user_id}. Waiting for initial scrape...")
    items = scrape_imdb_watchlist(user_id)
    if not items:
        raise HTTPException(status_code=504, detail="Initial scrape failed. Try again in a minute.")
    return items

@app.get("/stats")
def get_stats():
    cache = load_cache()
    stats = []
    for uid, data in cache.items():
        stats.append({
            "user_id": uid,
            "count": len(data['items']),
            "last_updated": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(data['timestamp']))
        })
    return stats

@app.get("/search")
def search_cache(q: str = Query(..., description="Search query")):
    cache = load_cache()
    query = q.lower()
    results = []
    for uid, data in cache.items():
        for item in data['items']:
            if query in item['title'].lower():
                # Add uid to the item for context in search results
                result_item = item.copy()
                result_item["source_user_id"] = uid
                results.append(result_item)
    return results

@app.get("/radarr")
def get_radarr_list(background_tasks: BackgroundTasks, user_id: str = Query(...), force: bool = False):
    items = get_watchlist(background_tasks, user_id, force)
    # Filter for movies
    movie_types = ['movie', 'tvMovie', 'video']
    return [i for i in items if i.get('type') in movie_types or i.get('type') is None]

@app.get("/sonarr")
def get_sonarr_list(background_tasks: BackgroundTasks, user_id: str = Query(...), force: bool = False):
    items = get_watchlist(background_tasks, user_id, force)
    # Filter for TV shows
    tv_types = ['tvSeries', 'tvMiniSeries', 'tvSpecial', 'tvShort']
    return [i for i in items if i.get('type') in tv_types]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IMDb Watchlist Server for *arr")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--user-id")
    parser.add_argument("--scrape-only", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--search", help="Search for a movie title in the local cache")
    parser.add_argument("--stats", action="store_true", help="Show statistics of the local cache")
    
    args = parser.parse_args()

    if args.stats:
        cache = load_cache()
        if not cache:
            print("Cache is empty.")
        else:
            print(f"{'User ID':<40} {'Items':<10} {'Last Updated'}")
            print("-" * 80)
            for uid, data in cache.items():
                last_updated = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(data['timestamp']))
                print(f"{uid:<40} {len(data['items']):<10} {last_updated}")
        sys.exit(0)

    if args.search:
        cache = load_cache()
        query = args.search.lower()
        found = False
        print(f"Searching for '{args.search}' in cache...")
        print("-" * 80)
        for uid, data in cache.items():
            for item in data['items']:
                if query in item['title'].lower():
                    year_str = f" ({item['year']})" if item.get('year') else ""
                    type_str = f" [{item.get('type', 'unknown')}]"
                    print(f"[{uid}] {item['imdb']}\t{item['title']}{year_str}{type_str}")
                    found = True
        if not found:
            print("No matches found in cache.")
        sys.exit(0)
    
    if args.user_id:
        uid = get_user_id(args.user_id)
        if args.list:
            items = scrape_imdb_watchlist(uid)
            for item in items or []:
                print(f"{item['imdb']}\t{item['title']} ({item['year']})")
        else:
            scrape_imdb_watchlist(uid)
            if args.scrape_only:
                sys.exit(0)

    uvicorn.run(app, host=args.host, port=args.port)
