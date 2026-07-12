# MCP Server Interface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an MCP interface (`search_watchlist`, `get_stats`, `list_watchlist` tools) to the existing IMDb Watchlist Sync server, exposed over both streamable-http (`/mcp`) and SSE (`/sse`) on the same FastAPI app and port.

**Architecture:** A new `mcp_server.py` module defines a `FastMCP` instance with the three tools. Tool bodies use function-local imports of `imdb_server` (`load_cache`, `scrape_imdb_watchlist`, `get_user_id`) to avoid a circular import, since `imdb_server.py` in turn imports the `mcp` object from `mcp_server.py` at module level to mount it. No automated test suite exists in this repo (spec: `docs/superpowers/specs/2026-07-12-mcp-server-interface-design.md`), so each task is verified manually with throwaway scripts under `/tmp` that mock `imdb_server.load_cache` / `imdb_server.scrape_imdb_watchlist` — this repo's real cache file at `data/watchlist_cache.json` contains live user data and must never be overwritten by verification steps.

**Tech Stack:** Python 3, FastAPI, `mcp` (official Python MCP SDK, `FastMCP`), existing `imdb_server.py`.

---

## Task 1: Add `mcp` dependency and verify FastMCP API contract

**Files:**
- Modify: `requirements.txt`

The venv's `pip` module is missing (`python -m pip` fails with `No module named pip`), so it must be bootstrapped with `ensurepip` first.

- [ ] **Step 1: Bootstrap pip in the venv**

Run: `/data/homes/stoneshi/src/imdb-cli/venv/bin/python -m ensurepip --upgrade`
Expected: prints something like `Successfully installed pip-25.1.1` (or confirms it's already present) with no error.

- [ ] **Step 2: Install the `mcp` package**

Run: `/data/homes/stoneshi/src/imdb-cli/venv/bin/python -m pip install mcp`
Expected: exits 0, ends with `Successfully installed mcp-<version> ...`.

- [ ] **Step 3: Verify the `FastMCP` API this plan depends on**

Run:
```bash
/data/homes/stoneshi/src/imdb-cli/venv/bin/python -c "
from mcp.server.fastmcp import FastMCP
m = FastMCP('test')

@m.tool()
def f(x: int) -> int:
    return x + 1

print('decorated fn still callable:', f(2))
print('has streamable_http_app:', hasattr(m, 'streamable_http_app'))
print('has sse_app:', hasattr(m, 'sse_app'))
"
```
Expected output:
```
decorated fn still callable: 3
has streamable_http_app: True
has sse_app: True
```
If any of these are `False` or the call raises, STOP — the rest of this plan assumes `@mcp.tool()` returns the original function unchanged (so tools stay directly callable for manual verification) and that `FastMCP` exposes both `streamable_http_app()` and `sse_app()`. Check the installed `mcp` version's docs/changelog and adjust Tasks 2-4 to match the actual API before proceeding.

- [ ] **Step 4: Add `mcp` to `requirements.txt`**

Modify `requirements.txt` (currently 5 lines: `fastapi`, `uvicorn`, `requests`, `beautifulsoup4`, `seleniumbase`) to add a new line:
```
mcp
```

- [ ] **Step 5: Commit**

```bash
git add requirements.txt
git commit -m "build: add mcp SDK dependency for MCP server interface"
```

---

## Task 2: Create `mcp_server.py` with `search_watchlist` and `get_stats` tools

**Files:**
- Create: `mcp_server.py`

- [ ] **Step 1: Write `mcp_server.py`**

```python
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
```

- [ ] **Step 2: Verify `search_watchlist` and `get_stats` manually**

This uses `unittest.mock.patch` on `imdb_server.load_cache` so the real `data/watchlist_cache.json` (which has live user data) is never touched.

Run, from `/data/homes/stoneshi/src/imdb-cli`:
```bash
venv/bin/python -c "
from unittest.mock import patch
import time

fixture_cache = {
    'ur999': {
        'timestamp': time.time(),
        'items': [
            {'title': 'Dune', 'imdb': 'tt1160419', 'imdb_id': 'tt1160419', 'imdbId': 'tt1160419', 'tmdbId': None, 'year': 2021, 'type': 'movie'},
            {'title': 'Dune: Part Two', 'imdb': 'tt15239678', 'imdb_id': 'tt15239678', 'imdbId': 'tt15239678', 'tmdbId': None, 'year': 2024, 'type': 'movie'},
        ]
    }
}

with patch('imdb_server.load_cache', return_value=fixture_cache):
    import mcp_server
    results = mcp_server.search_watchlist('dune')
    print('search count:', len(results))
    print('source_user_id set:', all(r['source_user_id'] == 'ur999' for r in results))
    stats = mcp_server.get_stats()
    print('stats:', stats)
"
```
Expected output:
```
search count: 2
source_user_id set: True
stats: [{'user_id': 'ur999', 'count': 2, 'last_updated': '<today's date/time>'}]
```

- [ ] **Step 3: Commit**

```bash
git add mcp_server.py
git commit -m "feat: add search_watchlist and get_stats MCP tools"
```

---

## Task 3: Add `list_watchlist` tool with pagination to `mcp_server.py`

**Files:**
- Modify: `mcp_server.py`

- [ ] **Step 1: Add the `list_watchlist` tool**

Append to `mcp_server.py`:
```python
@mcp.tool()
def list_watchlist(user_id: str, page: int = 1, page_size: int = 20) -> dict:
    """Paginated view of one user's cached watchlist. Triggers a scrape if uncached (blocking) or refreshes in the background if stale."""
    import threading

    from imdb_server import get_user_id, load_cache, scrape_imdb_watchlist

    uid = get_user_id(user_id)
    cache = load_cache()
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
```

- [ ] **Step 2: Verify all five `list_watchlist` scenarios manually**

Run, from `/data/homes/stoneshi/src/imdb-cli`:
```bash
venv/bin/python -c "
from unittest.mock import patch
import time

import mcp_server

items = [
    {'title': f'Movie {i}', 'imdb': f'tt{i}', 'imdb_id': f'tt{i}', 'imdbId': f'tt{i}', 'tmdbId': None, 'year': 2020, 'type': 'movie'}
    for i in range(25)
]
fresh_cache = {'ur1': {'timestamp': time.time(), 'items': items}}
stale_cache = {'ur1': {'timestamp': time.time() - 7200, 'items': items}}

with patch('imdb_server.load_cache', return_value=fresh_cache), \
     patch('imdb_server.scrape_imdb_watchlist') as mock_scrape:
    r = mcp_server.list_watchlist('ur1', page=1, page_size=20)
    print('Case 1 (fresh, page1):', r['page'], r['page_size'], r['total_items'], r['total_pages'], len(r['items']), 'scrape_called=', mock_scrape.called)

with patch('imdb_server.load_cache', return_value=fresh_cache), \
     patch('imdb_server.scrape_imdb_watchlist') as mock_scrape:
    r = mcp_server.list_watchlist('ur1', page=2, page_size=20)
    print('Case 2 (fresh, page2):', len(r['items']), r['total_pages'])

with patch('imdb_server.load_cache', return_value=fresh_cache), \
     patch('imdb_server.scrape_imdb_watchlist') as mock_scrape:
    r = mcp_server.list_watchlist('ur1', page=5, page_size=20)
    print('Case 3 (out-of-range page):', r['items'], r['total_pages'])

with patch('imdb_server.load_cache', return_value=stale_cache), \
     patch('imdb_server.scrape_imdb_watchlist') as mock_scrape:
    r = mcp_server.list_watchlist('ur1', page=1, page_size=20)
    time.sleep(0.2)
    print('Case 4 (stale, background refresh):', len(r['items']), 'scrape_called=', mock_scrape.called)

with patch('imdb_server.load_cache', return_value={}), \
     patch('imdb_server.scrape_imdb_watchlist', return_value=items[:3]):
    r = mcp_server.list_watchlist('ur2', page=1, page_size=20)
    print('Case 5 (no cache, scrape succeeds):', r['total_items'], len(r['items']))

with patch('imdb_server.load_cache', return_value={}), \
     patch('imdb_server.scrape_imdb_watchlist', return_value=[]):
    r = mcp_server.list_watchlist('ur3', page=1, page_size=20)
    print('Case 6 (no cache, scrape fails):', r.get('error'), r['items'], r['total_items'])
"
```
Expected output:
```
Case 1 (fresh, page1): 1 20 25 2 20 scrape_called= False
Case 2 (fresh, page2): 5 2
Case 3 (out-of-range page): [] 2
Case 4 (stale, background refresh): 20 scrape_called= True
Case 5 (no cache, scrape succeeds): 3 3
Case 6 (no cache, scrape fails): Initial scrape failed. Try again in a minute. [] 0
```

- [ ] **Step 3: Commit**

```bash
git add mcp_server.py
git commit -m "feat: add list_watchlist MCP tool with pagination"
```

---

## Task 4: Mount `/mcp` and `/sse` onto the FastAPI app

**Files:**
- Modify: `imdb_server.py:12` (imports)
- Modify: `imdb_server.py:24-30` (after middleware, add mounts)
- Modify: `imdb_server.py:177-188` (root endpoint listing)
- Modify: `README.md` (document new endpoints)

- [ ] **Step 1: Import the `mcp` object**

In `imdb_server.py`, change line 12 from:
```python
import threading
```
to:
```python
import threading

from mcp_server import mcp
```

- [ ] **Step 2: Mount the MCP sub-apps**

In `imdb_server.py`, after the `log_requests` middleware function (currently ends at line 30 with `return response`), add:
```python
app.mount("/mcp", mcp.streamable_http_app())
app.mount("/sse", mcp.sse_app())
```

- [ ] **Step 3: Update the root endpoint listing**

In `imdb_server.py`, change the `read_root` function (lines 177-188) from:
```python
@app.get("/")
def read_root():
    return {
        "status": "online", 
        "info": "IMDb Watchlist Server for *arr",
        "endpoints": {
            "/radarr?user_id=...": "Radarr compatible JSON list",
            "/sonarr?user_id=...": "Sonarr compatible JSON list",
            "/stats": "Get cache statistics",
            "/search?q=...": "Search across all cached watchlists"
        }
    }
```
to:
```python
@app.get("/")
def read_root():
    return {
        "status": "online", 
        "info": "IMDb Watchlist Server for *arr",
        "endpoints": {
            "/radarr?user_id=...": "Radarr compatible JSON list",
            "/sonarr?user_id=...": "Sonarr compatible JSON list",
            "/stats": "Get cache statistics",
            "/search?q=...": "Search across all cached watchlists",
            "/mcp": "MCP streamable-http endpoint (search_watchlist, get_stats, list_watchlist tools)",
            "/sse": "MCP SSE endpoint (same tools as /mcp)"
        }
    }
```

- [ ] **Step 4: Start the server and verify `/mcp` end-to-end**

Run: `venv/bin/python imdb_server.py --port 8099 &` (background it, note the PID)

Then run:
```bash
curl -s -i -X POST http://localhost:8099/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify","version":"0.0.1"}}}'
```
Expected: HTTP response (200, or a 307 redirect to `/mcp/` — if you see a 307, retry with `curl -L` following the redirect, and the plan's mount path stays as-is since `/mcp/tool-name-style` client configs typically follow redirects transparently) containing a JSON-RPC result with server capabilities, not an error.

If that succeeds, list the tools:
```bash
curl -s -L -X POST http://localhost:8099/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```
Expected: JSON-RPC result listing exactly three tools named `search_watchlist`, `get_stats`, `list_watchlist`.

- [ ] **Step 5: Verify `/sse` opens a stream**

Run: `curl -s -i -N --max-time 3 http://localhost:8099/sse`
Expected: response headers include `content-type: text/event-stream`; the command hangs on an open stream until the 3-second `--max-time` cuts it off (that's success — it means the stream stayed open rather than erroring immediately).

Stop the server: `kill %1` (or `kill <PID>` from the earlier background run).

- [ ] **Step 6: Update README with the new endpoints**

In `README.md`, after the "Search and Statistics" section (ends around line 51 with the `--search "Frankenstein"` example), add:
```markdown

## MCP Integration

The server also exposes an MCP interface with read-only tools (`search_watchlist`, `get_stats`, `list_watchlist`) for agents:

- **Streamable HTTP**: `http://your-server-ip:8080/mcp`
- **SSE**: `http://your-server-ip:8080/sse`

Both come up automatically alongside the REST API — no separate process or port.
```

- [ ] **Step 7: Commit**

```bash
git add imdb_server.py README.md
git commit -m "feat: mount MCP streamable-http and SSE endpoints on the FastAPI app"
```

---

## Post-implementation check

After Task 4, confirm the full spec is covered:
- `search_watchlist`, `get_stats`, `list_watchlist` tools exist and match REST parity — Tasks 2-3.
- Both `/mcp` and `/sse` live on the same app/port, active at server start — Task 4.
- `mcp` dependency recorded — Task 1.
- No REST/radarr/sonarr/auth changes — none made, matches "Out of Scope" in the spec.
