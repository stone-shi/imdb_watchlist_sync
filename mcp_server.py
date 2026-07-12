import logging
import time
from typing import Optional

from mcp.server.fastmcp import FastMCP

import embedding

# FastMCP defaults to host="127.0.0.1", which auto-enables DNS-rebinding
# protection that only allows Host headers of 127.0.0.1/localhost/::1. This
# server is mounted behind a reverse proxy that rewrites the Host header, so
# host="0.0.0.0" is required to avoid that restriction rejecting every
# proxied request with a 421.
mcp = FastMCP("imdb-watchlist", host="0.0.0.0")

logger = logging.getLogger("imdb-server.mcp")

# Same type groupings as imdb_server.py's /radarr and /sonarr filters.
MOVIE_TYPES = ["movie", "tvMovie", "video"]
TV_TYPES = ["tvSeries", "tvMiniSeries", "tvSpecial", "tvShort"]

# Max number of semantic-only matches (i.e. not already found lexically) to add.
SEMANTIC_TOP_K = 5


@mcp.tool()
def search_watchlist(query: str) -> list:
    """Search cached watchlists across all users for titles matching query. Uses case-insensitive
    substring matching, blended with semantic similarity search when an embedding API is configured
    via the EMBEDDING_API_URL/EMBEDDING_API_KEY/EMBEDDING_MODEL environment variables."""
    from imdb_server import load_cache

    cache = load_cache()
    q = query.lower()

    all_items = []
    for uid, data in cache.items():
        for item in data["items"]:
            entry = item.copy()
            entry["source_user_id"] = uid
            all_items.append(entry)

    results = [item for item in all_items if q in item["title"].lower()]
    matched_ids = {item["imdb"] for item in results}
    for item in results:
        item["match_type"] = "lexical"
        item["score"] = 1.0

    if embedding.is_configured():
        try:
            embeddings = embedding.get_embeddings(all_items)
            query_vector = embedding.embed_texts([query])[0]
            scored = sorted(
                (
                    (item, embedding.cosine_similarity(query_vector, embeddings[item["imdb"]]))
                    for item in all_items
                    if item["imdb"] not in matched_ids and item["imdb"] in embeddings
                ),
                key=lambda pair: pair[1],
                reverse=True,
            )
            for item, score in scored[:SEMANTIC_TOP_K]:
                item["match_type"] = "semantic"
                item["score"] = score
                results.append(item)
        except Exception:
            logger.warning("Semantic search failed, returning lexical-only results", exc_info=True)

    return results


@mcp.tool()
def get_stats() -> list:
    """Get cache statistics: movie count, tv count, and last-updated time for every cached user."""
    from imdb_server import load_cache

    cache = load_cache()
    stats = []
    for uid, data in cache.items():
        movie_count = sum(1 for i in data["items"] if i.get("type") in MOVIE_TYPES or i.get("type") is None)
        tv_count = sum(1 for i in data["items"] if i.get("type") in TV_TYPES)
        stats.append({
            "user_id": uid,
            "movie_count": movie_count,
            "tv_count": tv_count,
            "last_updated": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(data["timestamp"])),
        })
    return stats


@mcp.tool()
def list_watchlist(user_id: Optional[str] = None, page: int = 1, page_size: int = 20, filter: str = "all") -> dict:
    """Paginated view of one user's cached watchlist. If user_id is omitted, defaults to the only cached user (error if the cache has zero or more than one). filter can be 'all', 'movie', or 'tv' to restrict the results by title type (invalid values are treated as 'all'). Triggers a scrape if uncached (blocking) or refreshes in the background if stale."""
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

    if filter == "movie":
        items = [i for i in items if i.get("type") in MOVIE_TYPES or i.get("type") is None]
    elif filter == "tv":
        items = [i for i in items if i.get("type") in TV_TYPES]

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
