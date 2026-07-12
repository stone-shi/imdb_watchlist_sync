# MCP Server Interface — Design

## Purpose

Add an MCP (Model Context Protocol) interface to the existing IMDb Watchlist Sync
server so agents can query it natively via MCP tools, instead of only through
the REST endpoints + skill file. Both MCP transports — streamable-http and
SSE — must be available as soon as the server starts, with no separate
process or extra CLI flags.

## Scope

Read-only query tools only. `radarr`/`sonarr`-shaped filtering stays REST-only
since those are for *arr apps, not agents. Cache-mutating scrape triggers
(`--scrape-only`, `force=True`) are not exposed via MCP in this iteration.

## Architecture

- New module `mcp_server.py` defines a `FastMCP` instance (`mcp` package) with
  three tools (below). It imports and reuses `load_cache()` and
  `scrape_imdb_watchlist()` from `imdb_server.py` — no duplicated cache logic.
- In `imdb_server.py`, mount the MCP sub-apps onto the existing FastAPI `app`:
  - `app.mount("/mcp", mcp.streamable_http_app())`
  - `app.mount("/sse", mcp.sse_app())`
- Both are mounted at import time (module load), so they come up the instant
  `uvicorn.run(app, host=args.host, port=args.port)` starts serving — same
  port, same process, no new flags.
- `mcp` is added to `requirements.txt`.

## Tools

### 1. `search_watchlist(query: str)`

Same behavior as `GET /search`: case-insensitive substring match on `title`
across all cached users. Each result item is the cached item dict plus
`source_user_id`. Returns `[]` if nothing matches.

### 2. `get_stats()`

Same behavior as `GET /stats`: for every cached user, returns
`{"user_id": ..., "count": ..., "last_updated": "YYYY-MM-DD HH:MM:SS"}`.
Returns `[]` if the cache is empty.

### 3. `list_watchlist(user_id: str, page: int = 1, page_size: int = 20)`

Paginated view of one user's cached watchlist. Mirrors `GET /watchlist`
semantics for freshness:

- **No cache entry for `user_id`**: call `scrape_imdb_watchlist(user_id)`
  synchronously (blocking) to get an initial result, same as `/watchlist`
  does today when there's no cache. If the scrape returns no items, return
  an error-shaped result rather than raising (MCP tools should not crash the
  call on transient scrape failure — see Error Handling).
- **Cache entry exists but stale** (`time.time() - timestamp > 3600`):
  return the current cached page immediately, and kick off
  `threading.Thread(target=scrape_imdb_watchlist, args=(user_id,)).start()`
  to refresh in the background. This replaces the `BackgroundTasks` pattern
  used by the REST endpoint, because MCP tool calls have no FastAPI request
  context to inject `BackgroundTasks` from.
- **Cache entry exists and fresh**: return the current cached page, no
  background action.

Pagination: 1-indexed `page`. `page_size` default 20. Slice cached items as
`items[(page-1)*page_size : page*page_size]`.

Return shape:
```json
{
  "items": [...],
  "page": 1,
  "page_size": 20,
  "total_items": 47,
  "total_pages": 3
}
```

**Out-of-range `page`**: not an error — clamp to an empty `items: []` list
while still returning accurate `total_items`/`total_pages`, so an agent
paginating past the end sees a clean empty page rather than a fault.

## Error Handling

- `list_watchlist` on a user_id whose initial scrape fails (no cache, scrape
  returns no items): return
  `{"error": "Initial scrape failed. Try again in a minute.", "items": [], "page": 1, "page_size": page_size, "total_items": 0, "total_pages": 0}`
  rather than raising, since MCP tool errors surface less gracefully to
  agents than a structured empty/error result.
- `search_watchlist` and `get_stats` have no new failure modes — they read
  from `load_cache()`, which already tolerates a missing/corrupt cache file
  by returning `{}`.

## Testing

This repo has no existing automated test suite (no `pytest`, no `tests/`
directory) — verification is manual, consistent with existing practice:

1. Start the server (`python imdb_server.py --port 8080`).
2. Against `/mcp`: run an MCP `initialize` → `tools/list` → `tools/call`
   sequence (e.g. via `npx @modelcontextprotocol/inspector` or raw
   JSON-RPC) and confirm all three tools appear and return expected shapes.
3. Against `/sse`: confirm the endpoint opens an SSE event stream.
4. Exercise `list_watchlist` pagination manually: request page 1 and page 2
   of a cached user with more than `page_size` items, and an out-of-range
   page, confirming the empty-page behavior above.

## Out of Scope (future ideas)

- Exposing scrape-trigger / force-refresh via MCP.
- MCP tools for `radarr`/`sonarr`-filtered lists.
- Auth on `/mcp` or `/sse` (matches current REST endpoints, which are also
  unauthenticated).
