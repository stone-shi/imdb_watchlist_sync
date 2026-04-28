import requests
from bs4 import BeautifulSoup
import json
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from typing import List, Optional, Dict
import argparse
import sys
import os
import time
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("imdb-server")

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

def get_user_id(input_str: str) -> str:
    """Extracts user ID from URL or returns the ID as is."""
    if "imdb.com/user/" in input_str:
        # Extract from https://www.imdb.com/user/ur12345/watchlist or https://www.imdb.com/user/p.xxxx/watchlist
        parts = input_str.split("imdb.com/user/")[1].split("/")
        return parts[0]
    return input_str

def scrape_imdb_watchlist(user_id: str):
    user_id = get_user_id(user_id)
    url = f"https://www.imdb.com/user/{user_id}/watchlist"
    
    logger.info(f"Starting scrape for {url} using Undetected Mode...")
    
    from seleniumbase import Driver
    import logging as sbase_logging
    
    # Suppress seleniumbase logging further
    sbase_logging.getLogger('seleniumbase').setLevel(sbase_logging.WARNING)
    
    driver = None
    try:
        # Initialize Driver in Undetected Mode
        driver = Driver(uc=True, headless=True)
        # Use uc_open_with_reconnect to bypass AWS WAF challenges
        driver.uc_open_with_reconnect(url, 6)
        
        # Check if we are still on the challenge page
        if "challenge" in driver.page_source.lower() or len(driver.page_source) < 5000:
            logger.info("Challenge page detected, retrying...")
            time.sleep(2)
            driver.uc_open_with_reconnect(url, 6)
            
        page_source = driver.page_source
        soup = BeautifulSoup(page_source, 'html.parser')
    except Exception as e:
        logger.error(f"Error during selenium scraping: {e}")
        return None
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass

    script_tag = soup.find('script', id='__NEXT_DATA__')
    
    if not script_tag:
        logger.error("Could not find __NEXT_DATA__ script tag in the page source.")
        return None

    try:
        data = json.loads(script_tag.string)
        items = []
        
        # Try to find edges in common locations
        props = data.get('props', {})
        pageProps = props.get('pageProps', {})
        mainColumnData = pageProps.get('mainColumnData', {})
        
        edges = []
        # Check known paths
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
            # Fallback search for any edges list in mainColumnData
            for val in mainColumnData.values():
                if isinstance(val, dict) and 'edges' in val:
                    edges = val['edges']
                    break
        
        if not edges:
            logger.warning("Could not find 'edges' list in the JSON structure.")

        for item in edges:
            node = item.get('node', {})
            # If no node, try listItem (new structure for some lists)
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
                
            if imdb_id:
                items.append({
                    "title": title,
                    "imdb_id": imdb_id,
                    "year": year
                })
        
        logger.info(f"Successfully scraped {len(items)} items from IMDb.")
        return items if items else None
    except Exception as e:
        logger.error(f"Unexpected error parsing script tag: {e}")
        return None

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
        "endpoints": {
            "/radarr?user_id=...": "Radarr compatible JSON list",
            "/sonarr?user_id=...": "Sonarr compatible JSON list",
            "/watchlist?user_id=...": "General watchlist data"
        },
        "info": "Use your IMDb User ID (urXXXX) or Profile ID (p.XXXX) in the query string."
    }

@app.get("/watchlist")
def get_watchlist(user_id: str = Query(..., description="IMDb User ID"), force: bool = False):
    user_id = get_user_id(user_id)
    cache = load_cache()
    
    # Check cache (1 hour expiry)
    if not force and user_id in cache:
        cached_data = cache[user_id]
        if time.time() - cached_data.get('timestamp', 0) < 3600:
            logger.info(f"Cache hit for user {user_id}")
            return cached_data.get('items')

    items = scrape_imdb_watchlist(user_id)
    if items is None:
        if user_id in cache:
            logger.warning(f"Scrape failed for {user_id}, returning stale cache.")
            return cache[user_id].get('items')
        logger.error(f"Scrape failed for {user_id} and no cache available.")
        raise HTTPException(status_code=500, detail="Failed to scrape watchlist and no cache available.")
    
    # Update cache
    logger.info(f"Updating cache for user {user_id}")
    cache[user_id] = {
        "timestamp": time.time(),
        "items": items
    }
    save_cache(cache)
    return items

@app.get("/radarr")
def get_radarr_list(user_id: str = Query(..., description="IMDb User ID")):
    items = get_watchlist(user_id)
    # Radarr just needs a JSON array of objects with imdb_id
    return items

@app.get("/sonarr")
def get_sonarr_list(user_id: str = Query(..., description="IMDb User ID")):
    items = get_watchlist(user_id)
    # Sonarr usually wants TVDb, but we can only give IMDb IDs here.
    # Some Sonarr versions or setups might handle it.
    return items

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IMDb Watchlist Server for *arr")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind to")
    parser.add_argument("--user-id", help="Default IMDb User ID or URL to test or pre-cache")
    parser.add_argument("--scrape-only", action="store_true", help="Scrape and exit without starting server")
    parser.add_argument("--list", action="store_true", help="Print the watchlist items to the console and exit")
    
    args = parser.parse_args()
    
    if args.user_id:
        uid = get_user_id(args.user_id)
        if not args.list:
            print(f"Target User ID: {uid}")
        items = scrape_imdb_watchlist(uid)
        if items:
            if args.list:
                for item in items:
                    year_str = f" ({item['year']})" if item.get('year') else ""
                    print(f"{item['imdb_id']}\t{item['title']}{year_str}")
            else:
                print(f"Successfully retrieved {len(items)} items.")
                cache = load_cache()
                cache[uid] = {"timestamp": time.time(), "items": items}
                save_cache(cache)
        else:
            print("Scrape failed. Ensure the watchlist is public and try again.")
            if args.scrape_only or args.list:
                sys.exit(1)

    if not args.scrape_only and not args.list:
        print(f"Starting server on {args.host}:{args.port}...")
        uvicorn.run(app, host=args.host, port=args.port)
