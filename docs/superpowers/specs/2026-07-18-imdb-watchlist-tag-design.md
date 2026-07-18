# Apply 'imdb_watchlist' Tag on Radarr/Sonarr — Design

## Purpose

Every movie/series that `arr_sync.py` places in Radarr or Sonarr because it's
on a cached IMDb watchlist should carry a Radarr/Sonarr tag named
`imdb_watchlist`. This makes it possible to filter/report on "things this
tool added" from within Radarr/Sonarr's own UI, independent of this app's
`/sync` status page. This extends the existing sync flow documented in
[2026-07-16-arr-watchlist-sync-design.md](2026-07-16-arr-watchlist-sync-design.md);
read that first for the surrounding sync/scheduler/guard architecture, which
is unchanged by this work.

## Scope

- Tag name is the constant `"imdb_watchlist"` — not configurable, no new
  `config.json` key, no per-service override. If a rename is ever needed,
  that's a code change.
- Applies to **both** newly-added items and items already present in the
  library that match something in the watchlist cache (backfill) — every
  sync cycle re-checks and tags any matching library item that isn't tagged
  yet, not just items added by this tool going forward.
- "Matches the watchlist cache" reuses the exact same per-cycle item lists
  `_sync_movies`/`_sync_tv` already build from `load_cache()` — no new
  matching logic, no separate DB of "items this tool has ever added."
- Out of scope: removing the tag, tagging items that are in the library but
  *not* on any cached watchlist, a UI toggle to disable tagging, per-service
  tag names.

## Radarr/Sonarr API surface used

Both APIs expose tags the same way:

- `GET /api/v3/tag` → `[{"id": int, "label": str}, ...]`
- `POST /api/v3/tag` with `{"label": str}` → creates and returns
  `{"id": int, "label": str}`.
- Movie/series resources carry a `"tags": [int, ...]` field (list of tag
  ids). `PUT /api/v3/movie/{id}` / `PUT /api/v3/series/{id}` require the
  **full** resource body (not a partial patch) to update it — so tagging an
  existing item means taking the object as returned by `GET`, adding the tag
  id to its `tags` list, and PUT-ing the whole thing back.

## Client changes (`RadarrClient`/`SonarrClient`)

- New method `get_or_create_tag_id(label: str) -> Optional[int]`: fetch
  `GET /tag`, match `label` case-insensitively against existing tags; if no
  match, `POST /tag` to create it and return the new id. Raises on transport
  errors (caller decides how to degrade — see Error handling).
- `get_library_imdb_ids()` is replaced by `get_library_by_imdb() ->
  dict[str, dict]` mapping `imdbId -> full library item` (previously this
  only returned a `set` of ids). Callers that only need membership can use
  `.keys()` / `in`; the richer value is needed here to read/update `tags`.
- New method `update_movie(movie: dict) -> dict` / `update_series(series:
  dict) -> dict`: `PUT /movie/{id}` / `PUT /series/{id}` sending the given
  dict as-is (caller is responsible for having mutated `tags` beforehand).

## Sync loop changes (`_sync_movies`/`_sync_tv`)

1. After resolving quality profile / root folder (existing code), resolve
   `tag_id = client.get_or_create_tag_id("imdb_watchlist")`. If this raises,
   log a warning ("failed to resolve imdb_watchlist tag, continuing without
   tagging") and set `tag_id = None` — this must not block the add/skip
   logic that already works today.
2. Replace the `existing_imdb_ids` set with `existing_library =
   client.get_library_by_imdb()` (dict). Every place that did
   `imdb_id in existing_imdb_ids` becomes `imdb_id in existing_library`.
3. **Item already in the library** (the existing `skipped_existing` branch):
   if `tag_id is not None`, look at `existing_library[imdb_id]["tags"]`.
   - If `tag_id` already present → do nothing (already tagged from a prior
     cycle or a prior add in this same cycle).
   - If absent and `dry_run` → log `"[dry run] would tag existing movie:
     <title>"`, increment `would_tag`.
   - If absent and not `dry_run` → append `tag_id` to a **copy** of the
     item's `tags` list, call `update_movie`/`update_series` with the
     mutated item, increment `tagged`, and update `existing_library[imdb_id]`
     in place so a duplicate `imdb_id` later in the same cycle (from a
     second cached user's watchlist) sees it's already tagged and skips the
     PUT.
   - A PUT failure here is caught per-item, logged as an error with the
     title/imdb_id, and does not raise — the loop continues. It does not
     increment any counter (see Error handling below for why).
4. **New item being added**: when `tag_id is not None`, set
   `payload["tags"] = [tag_id]` on the dict passed into `add_movie`/
   `add_series` (the lookup result normally carries `"tags": []`, so this is
   a plain overwrite, not a merge). No extra API call — the tag is included
   in the same `POST`. `tag_id is None` → payload's tags field is left as
   whatever the lookup returned (empty), i.e. today's behavior.
5. All other branches (excluded, dry-run add, failed lookup, stop-event
   early exit) are unchanged.

## Counts

`counts` dict gains two keys: `tagged`, `would_tag`. Final shape:
`{"added", "would_add", "skipped_existing", "skipped_excluded", "failed",
"tagged", "would_tag"}`.

- `tagged`/`would_tag` count **only backfill tagging of pre-existing library
  items**. New items tagged at creation time are already reflected in
  `added`/`would_add` — they are not double-counted here.
- No new counter for tag-PUT failures. This is a best-effort secondary
  operation on an item that's already correctly in the library either way;
  a log line is sufficient, and inventing a counter for a rare failure mode
  adds a rendering column and a test case for a scenario nobody will act on
  differently based on a number. If this proves wrong in practice, add the
  counter later.

## Status page (`_render_sync_page`)

The counts table gains two columns, `Tagged` and `Would tag`, alongside the
existing five (`counts_row` helper is extended accordingly). `/sync/status`
JSON picks these up automatically since it just serializes the `counts` dict.

## Error handling

- Tag resolution (`get_or_create_tag_id`) failing degrades the *entire*
  service's tagging for that cycle (`tag_id = None`) but never blocks
  add/skip/exclude logic — tagging is additive, not a gate.
- A single item's tag PUT failing is isolated to that item (existing
  per-item try/except already wraps this region) and logged; the cycle
  continues.
- Tag creation racing across two services (Radarr vs Sonarr) is a non-issue
  since Radarr and Sonarr each have their own independent tag namespace —
  no shared state, no lock needed beyond what already serializes
  `_sync_movies` before `_sync_tv` in `_run_sync`.

## Testing

Extend `tests/test_arr_sync.py` (existing file, same `MagicMock`-client and
`patch("arr_sync.requests.get"/"post")` style already used there):

- `get_or_create_tag_id` returns an existing tag's id without POSTing when
  the label already exists (case-insensitive match).
- `get_or_create_tag_id` POSTs to create the tag when absent, returns the
  new id.
- New movie/series add includes `"tags": [tag_id]` in the POST payload.
- Existing library item missing the tag triggers a PUT with the tag id
  appended to its existing `tags`; one that already carries the tag id
  results in no PUT call.
- `dry_run=True` logs "would tag" for an untagged existing item and issues
  no PUT.
- Tag resolution raising an exception still allows adds/skips to proceed
  normally, just without any tags set.
- Update `test_sync_movies_stops_early_when_stop_event_set` (currently
  asserts `counts == {...}` with the five old keys) to include `tagged: 0,
  would_tag: 0` in the expected dict — a mechanical follow-on from the
  counts shape change, not new behavior.

## Out of scope (future ideas)

- Configurable tag name/toggle.
- Untagging items removed from a watchlist.
- A dedicated failure counter for tag-PUT errors.
