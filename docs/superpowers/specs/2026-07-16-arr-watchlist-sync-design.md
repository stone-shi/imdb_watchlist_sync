# Periodic Radarr/Sonarr Watchlist Sync — Design

## Purpose

Periodically check all cached IMDb watchlists against a configured
Radarr/Sonarr pair, and auto-add anything that's missing (and not
import-list-excluded) to the appropriate one. Expose status/log/manual
controls via a small web page, since this runs unattended.

## Scope

- One Radarr instance + one Sonarr instance, shared across all cached IMDb
  users (not per-user).
- Movie vs. TV split reuses the existing `MOVIE_TYPES`/`TV_TYPES` lists from
  `mcp_server.py`.
- Out of scope: removing items from Radarr/Sonarr, syncing *arr state back
  into the IMDb cache, per-user *arr targets, auth on the new endpoints
  (matches existing REST/MCP endpoints, which are also unauthenticated).

## Configuration

New module `arr_sync.py` owns a `data/config.json` (bind-mountable in
Docker, like `data/watchlist_cache.json`). It is **not** committed — added to
`.gitignore` alongside the other `data/*.json` runtime files. A committed
`data/config.example.json` documents the shape:

```json
{
  "poll_interval_seconds": 3600,
  "sync_timeout_seconds": 7200,
  "dry_run": true,
  "radarr": {
    "url": "",
    "api_key": "",
    "quality_profile": "HD-1080p",
    "root_folder_path": null,
    "minimum_availability": "announced",
    "search_on_add": true
  },
  "sonarr": {
    "url": "",
    "api_key": "",
    "quality_profile": "HD-1080p",
    "root_folder_path": null,
    "monitor": "all",
    "series_type": "standard",
    "season_folder": true,
    "search_on_add": true
  }
}
```

- `poll_interval_seconds`, `sync_timeout_seconds`, and each service's
  `url`/`api_key` can be overridden by env vars (loaded via `python-dotenv`,
  same as `embedding.py`): `ARR_SYNC_POLL_INTERVAL_SECONDS`,
  `ARR_SYNC_TIMEOUT_SECONDS`, `SONARR_URL`, `SONARR_API_KEY`, `RADARR_URL`,
  `RADARR_API_KEY`. Env wins over the config file when both are set.
- A service with a blank `url`/`api_key` (the shipped default) is skipped
  every cycle — an unconfigured deployment is a safe no-op even though the
  scheduler itself always runs.
- `root_folder_path: null` auto-selects the sole entry from that server's
  `GET /rootfolder` if there's exactly one; if there are zero or several and
  none is configured, that cycle logs an error and skips that service.
- `quality_profile` is resolved **by name** against `GET /qualityprofile` at
  sync time (not a hardcoded id), so it survives differing profile ids
  across instances.
- Defaults reflect the confirmed choices: `search_on_add: true` (Radarr
  `addOptions.searchForMovie` / Sonarr `addOptions.searchForMissingEpisodes`),
  Sonarr `monitor: "all"` (whole series, not just future episodes), and
  `dry_run: true` in the shipped example so a fresh deployment logs its
  intended actions instead of writing to *arr until the operator reviews
  and flips it off.

## Scheduler

An `asyncio` loop, started alongside the existing `mcp.session_manager.run()`
in `imdb_server.py`'s `lifespan` (and cancelled on shutdown):

```python
while True:
    try_start_sync(source="periodic")
    await asyncio.sleep(poll_interval_seconds)
```

No new dependency (APScheduler would be overkill for one fixed interval).
`imdb_server.py` also gets a `--sync-once` CLI flag (mirrors
`--scrape-only`) that calls the same sync entry point once and exits, for
manual/cron-driven use.

## Concurrency, guard, and timeout

The sync itself (`sync_all()`) makes synchronous `requests` calls to
Radarr/Sonarr — like the rest of this codebase (`embedding.py`, the IMDb
scraper) — so it runs in a `threading.Thread`, following the existing
`threading.Thread(target=scrape_imdb_watchlist, ...)` pattern already used in
`mcp_server.py`'s `list_watchlist`. Module state in `arr_sync.py`:

- `_current_thread: threading.Thread | None`
- `_stop_event: threading.Event`
- `_status: dict` (current state, guarded by a `threading.Lock`)
- `_log: collections.deque(maxlen=500)`, fed by a `logging.Handler` attached
  to a dedicated `imdb-server.arr_sync` logger (reuses existing logging
  infra rather than hand-building strings).

`try_start_sync(source)` is the single entry point called by the periodic
loop, the manual trigger endpoint, and `--sync-once`:

```
with _lock:
    if _current_thread is alive:
        elapsed = now - _status["started_at"]
        if elapsed > sync_timeout_seconds and not _stop_event.is_set():
            log("sync running Xs, exceeds timeout Ys; requesting stop")
            _stop_event.set()
            _status["state"] = "stopping (timed out)"
        else:
            log("sync already running; skipping this trigger")
        return False   # never starts a second thread while one is alive
    _stop_event.clear()
    _status = {state: "running", started_at: now, source, dry_run, ...}
    _current_thread = threading.Thread(target=_run_sync, args=(_stop_event,), daemon=True)
    _current_thread.start()
    return True
```

**"Kill" is cooperative, not a hard OS-level kill** — Python threads can't be
forcibly terminated safely, and true preemption would require
multiprocessing, which is unwarranted here. Instead:

- Every Radarr/Sonarr HTTP call passes a request `timeout` (~30s), bounding
  how long any single call can block.
- `_run_sync` checks `_stop_event` between each watchlist item (and between
  the Radarr and Sonarr phases) and exits early when set.
- Worst-case delay between a stop signal and the thread actually exiting is
  therefore roughly one HTTP call's timeout, not indefinite — and no second
  sync thread is ever started while the old one is still alive, so
  `_stop_event` being set is always resolved before a fresh run begins.

A manual **Stop** button unconditionally sets `_stop_event` (regardless of
elapsed time) via `POST /sync/stop`, giving an on-demand abort in addition
to the timeout-triggered one. If nothing is running, it's a no-op response.

## Sync logic

`RadarrClient` and `SonarrClient` are kept as separate small classes rather
than a forced shared base — the APIs diverge enough (`tmdbId` vs `tvdbId`,
`/exclusions` vs `/importlistexclusion`, movie vs. series add payloads) that
sharing would cost more clarity than it saves.

Per cycle, per configured service:

1. `GET /movie` or `GET /series` once → set of existing `imdbId`s already in
   the library (both APIs expose `imdbId` directly on library items, so this
   needs no extra lookup call).
2. `GET /exclusions` or `GET /importlistexclusion` once → set of excluded
   `tmdbId`/`tvdbId`s (these are **not** keyed by `imdbId`, so a candidate
   not yet in the library still needs a metadata lookup before it can be
   checked against this set).
3. For each watchlist item of the matching type not already in the library:
   - Resolve metadata: `GET /movie/lookup/imdb?imdbId=...` (Radarr) or
     `GET /series/lookup?term=imdb:...` (Sonarr). No match / lookup failure →
     log and count as failed, continue to the next item.
   - If the resolved `tmdbId`/`tvdbId` is in the excluded set → log and count
     as excluded, continue.
   - If `dry_run` → log "would add `<title>`", count as would-add, continue
     (no write call).
   - Otherwise `POST /movie` / `POST /series` with the resolved metadata,
     configured quality profile id, root folder, and:
     - Radarr: `minimumAvailability`, `monitored: true`,
       `addOptions: {searchForMovie: <search_on_add>}`
     - Sonarr: `seriesType`, `seasonFolder`, `monitored: true`,
       `addOptions: {monitor: <monitor>, searchForMissingEpisodes: <search_on_add>}`
   - Check `_stop_event` before moving to the next item; exit the loop early
     if set.
4. Each item is wrapped in its own try/except so one bad lookup/add doesn't
   abort the rest of the cycle.
5. End-of-cycle (or early-exit) status update: counts of
   added/skipped-existing/skipped-excluded/failed/would-add per service,
   final `state` (`success` / `stopped` / `error`), `finished_at`.

## Web page and status

Three new routes on the existing FastAPI `app` (via an `APIRouter` defined in
`arr_sync.py`, included from `imdb_server.py` — no new dependency, no
template engine; the page is small enough for a hand-rolled `HTMLResponse`):

- `GET /sync` — HTML status page: current state (idle / running with
  elapsed time / success / stopped / error, each with timestamps), per-service
  counts from the last completed run, the recent log (`_log` rendered in a
  `<pre>` block), and a **Trigger** button (`<form>` POSTing to
  `/sync/trigger`) plus a **Stop** button (`<form>` POSTing to
  `/sync/stop`). Auto-refreshes every 5s via
  `<meta http-equiv="refresh" content="5">` — no JS needed.
- `GET /sync/status` — the same status as JSON, for scripting/tests.
- `POST /sync/trigger` — calls `try_start_sync(source="manual")`; responds
  with whether a new run started or one was already in progress (and, per
  the timeout logic above, may itself be the trigger that signals a stuck
  run to stop).
- `POST /sync/stop` — sets `_stop_event` unconditionally if a run is active.

Status/log state is in-memory only (resets on server restart) — this is
operational visibility, not data that needs to survive a restart, and the
next scheduled tick re-populates it regardless.

## Error handling

- Per-item failures (bad lookup, add rejected by *arr) are logged and
  counted, not raised — one bad item never aborts a cycle.
- A service with no root folder resolvable, or a `quality_profile` name that
  doesn't match any profile on that server, logs an error and skips that
  service for the cycle (the other configured service still runs).
- Unhandled exceptions in `_run_sync` are caught at the top level, logged,
  and set `state: "error"` with the exception message — the thread always
  exits and clears `_current_thread`, so a bug in the sync logic can't wedge
  the guard permanently.

## Testing

Unit tests in `tests/` mock `requests` calls (same `patch`-based style as
existing `patch_cache`/`patch_watchlist` helpers) covering: already-in-library
skip, excluded skip, dry-run no-op, add-call payload construction, the
duplicate-trigger guard (second trigger while one is "running" is rejected),
and the timeout-then-stop transition (simulate elapsed time exceeding
`sync_timeout_seconds`, confirm `_stop_event` gets set and the run reports
`stopped`).

No automated test hits the real Radarr/Sonarr instances given for manual
verification — those are used only for read-only checks and `dry_run`
verification during development; an actual live `POST` add against them
requires explicit confirmation at the time, separate from this spec.

## Docker / docs

- `arr_sync.py` is added to the `Dockerfile`'s `COPY` line alongside the
  other three modules (the existing gotcha this repo's `CLAUDE.md` already
  flags for new top-level modules).
- `data/config.json` added to `.gitignore`; `data/config.example.json`
  committed as the template.
- `.env` (already git-ignored) gains `SONARR_URL`/`SONARR_API_KEY`/
  `RADARR_URL`/`RADARR_API_KEY` for local testing against the real instances
  provided for this work.
- `CLAUDE.md` gets a new section documenting `arr_sync.py`'s role, the
  config file, and the `/sync*` routes, matching how the other three modules
  are already documented there.

## Out of scope (future ideas)

- Per-user *arr targets (currently one shared Radarr/Sonarr pair for all
  cached IMDb users).
- Removing items from *arr when they leave a watchlist.
- Persisting sync history across restarts.
- Auth on `/sync*` routes.
