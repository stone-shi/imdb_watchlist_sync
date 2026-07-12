import time
from typing import Optional

from mcp.server.fastmcp import FastMCP

# FastMCP defaults to host="127.0.0.1", which auto-enables DNS-rebinding
# protection that only allows Host headers of 127.0.0.1/localhost/::1. This
# server is mounted behind a reverse proxy that rewrites the Host header, so
# host="0.0.0.0" is required to avoid that restriction rejecting every
# proxied request with a 421.
mcp = FastMCP("imdb-watchlist", host="0.0.0.0")


@mcp.tool()
def search_watchlist(query: str) -> list:
    """Search cached watchlists across all users for titles matching query (case-insensitive substring match)."""
    from imdb_server import load_cache

    cache = load_cache()
    q = query.lower()
    results = []
    for uid, data in cache.items():
        for item in data["items"]:
            if q in item["title"].lower():
                result_item = item.copy()
                result_item["source_user_id"] = uid
                results.append(result_item)
    return results


@mcp.tool()
def get_stats() -> list:
    """Get cache statistics: item count and last-updated time for every cached user."""
    from imdb_server import load_cache

    cache = load_cache()
    stats = []
    for uid, data in cache.items():
        stats.append({
            "user_id": uid,
            "count": len(data["items"]),
            "last_updated": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(data["timestamp"])),
        })
    return stats


@mcp.tool()
def list_watchlist(user_id: Optional[str] = None, page: int = 1, page_size: int = 20) -> dict:
    """Paginated view of one user's cached watchlist. If user_id is omitted, defaults to the only cached user (error if the cache has zero or more than one). Triggers a scrape if uncached (blocking) or refreshes in the background if stale."""
    import threading

    from imdb_server import get_user_id, load_cache, scrape_imdb_watchlist

    cache = load_cache()

    if user_id is None:
        cached_uids = list(cache.keys())
        if len(cached_uids) != 1:
            error = (
                "No cached users. Provide a user_id."
                if not cached_uids
                else "Multiple cached users; user_id is required. Call get_stats to see them."
            )
            return {
                "error": error,
                "items": [],
                "page": page,
                "page_size": page_size,
                "total_items": 0,
                "total_pages": 0,
            }
        uid = cached_uids[0]
    else:
        uid = get_user_id(user_id)

    cached_entry = cache.get(uid)

    if not cached_entry:
        items = scrape_imdb_watchlist(uid)
        if not items:
            return {
                "error": "Initial scrape failed. Try again in a minute.",
                "items": [],
                "page": page,
                "page_size": page_size,
                "total_items": 0,
                "total_pages": 0,
            }
    else:
        items = cached_entry["items"]
        is_stale = time.time() - cached_entry.get("timestamp", 0) > 3600
        if is_stale:
            threading.Thread(target=scrape_imdb_watchlist, args=(uid,)).start()

    total_items = len(items)
    total_pages = (total_items + page_size - 1) // page_size if page_size > 0 else 0
    start = (page - 1) * page_size
    page_items = items[start:start + page_size] if start < total_items else []

    return {
        "items": page_items,
        "page": page,
        "page_size": page_size,
        "total_items": total_items,
        "total_pages": total_pages,
    }
