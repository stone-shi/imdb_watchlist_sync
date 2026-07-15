# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A FastAPI server that scrapes public IMDb watchlists (via SeleniumBase in undetected mode, to bypass IMDb's AWS WAF challenge) and serves them as JSON compatible with Radarr/Sonarr custom import lists. It also exposes an MCP interface (mounted in the same process) with read-only tools for querying the cache. Multiple users' watchlists can be cached simultaneously, keyed by IMDb user ID.

## Commands

```bash
./test.sh                          # create/reuse venv, install deps, run full pytest suite
./test.sh -k test_name              # run a single test (args are forwarded to pytest)
./build.sh                          # build & push Docker image to registry.shifamily.com (writes version.txt from git rev)

python imdb_server.py --port 8080                                  # run the server
python imdb_server.py --user-id <ID> --list                        # scrape and print a watchlist without starting the server
python imdb_server.py --user-id <ID> --scrape-only                  # scrape once, populate cache, exit
python imdb_server.py --stats                                       # print cache stats from the CLI
python imdb_server.py --search "title"                              # search the local cache from the CLI
```

There is no lint config in this repo; don't invent one.

## Architecture

Three Python modules make up the whole app, loaded together into one FastAPI process:

- **`imdb_server.py`** — scraper, REST API, and CLI entrypoint. Owns `load_cache()`/`save_cache()` (JSON file at `data/watchlist_cache.json`, keyed by user ID) and `scrape_imdb_watchlist()`. `SCRAPE_LOCKS` is a module-level set used to dedupe concurrent scrapes of the same user. REST endpoints (`/watchlist`, `/radarr`, `/sonarr`, `/stats`, `/search`) read/write this cache directly.
- **`mcp_server.py`** — defines the `mcp` FastMCP instance and its three tools (`search_watchlist`, `get_stats`, `list_watchlist`). These tools `import` from `imdb_server` *inside each function body* (not at module top) specifically to avoid a circular import, since `imdb_server.py` itself imports `mcp` from this module. Tool logic mirrors the REST endpoints' filtering/caching behavior but is not a thin wrapper around them — keep the two in sync manually when changing cache shape or type-filtering rules.
- **`embedding.py`** — optional OpenAI-compatible embeddings client used only by `search_watchlist`. Config comes from `EMBEDDING_API_URL`/`EMBEDDING_API_KEY`/`EMBEDDING_MODEL` env vars (`.env`, git-ignored). `is_configured()` gates all semantic-search code paths; when unset, search is lexical-only substring matching. Embeddings are cached per-title in `data/embedding_index.json` and invalidated per-item when the title or model name changes.

### Mounting MCP inside FastAPI

`imdb_server.py` mounts `mcp.streamable_http_app()` at `/mcp` and `mcp.sse_app()` at `/sse`. Two non-obvious details, both commented in the source — don't undo them without understanding why:
- `mcp.settings.streamable_http_path`/`sse_path` are overridden to `"/"` before mounting, because FastMCP's sub-apps already prefix their routes and would otherwise double up (`/mcp/mcp` instead of `/mcp`).
- FastAPI's `lifespan` is overridden to also run `mcp.session_manager.run()`, because Starlette does not propagate lifespan events into mounted sub-apps automatically — omitting this breaks the MCP session manager.
- `mcp = FastMCP(..., host="0.0.0.0")` in `mcp_server.py` is required (not `127.0.0.1`, FastMCP's default) because this server sits behind a reverse proxy that rewrites the Host header, and FastMCP's DNS-rebinding protection would otherwise reject every proxied request with a 421.

### Cache and data model

Cache is a single JSON file: `{user_id: {"timestamp": float, "items": [...]}}`. Considered stale after 3600s. Each item has `title`, `imdb`/`imdbId`/`imdb_id` (redundant aliases for the same IMDb ID, kept for compatibility with different consumers), `tmdbId` (always `None` — not populated by the scraper), `year`, `type` (IMDb's raw type string, e.g. `movie`, `tvSeries`, `tvMiniSeries`). Movie vs. TV filtering (used by `/radarr`, `/sonarr`, and the MCP tools) is done by checking `type` against the `MOVIE_TYPES`/`TV_TYPES` lists in `mcp_server.py` — movies also match when `type is None` (unknown type defaults to movie-eligible).

### Scraping

`scrape_imdb_watchlist()` drives a headless undetected Chrome via SeleniumBase, opens the user's watchlist URL, and detects/retries once on IMDb's challenge page. It parses the `__NEXT_DATA__` script tag's JSON and walks several possible shapes of IMDb's GraphQL response (`prefilteredTitleList`, `watchlistData`, `predefinedList`, or a fallback scan for any key containing `edges`) since IMDb's page data shape has changed over time. If scraping succeeds and other users are cached, only the scraped user's cache entry is updated (the on-disk cache for all other users is preserved).

## Testing

Tests live in `tests/test_imdb_server.py` and use FastAPI's `TestClient` against the real `app` object, patching `imdb_server.load_cache`/`get_watchlist` (see the `patch_cache`/`patch_watchlist` helpers in that file) rather than hitting IMDb or spinning up a browser. There is no test coverage for `mcp_server.py` or `embedding.py` yet.

## Docker

`Dockerfile` installs Google Chrome + `sbase install chromedriver` for SeleniumBase, then copies `imdb_server.py mcp_server.py embedding.py version.txt*` into the image — when adding a new top-level module, add it to that `COPY` line too or it won't ship. `data/` is a volume mount (see `docker-compose.yml`) so the watchlist and embedding caches persist across container restarts.
