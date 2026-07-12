import time

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("imdb-watchlist")


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
