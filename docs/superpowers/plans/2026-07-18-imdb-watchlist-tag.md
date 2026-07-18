# Apply 'imdb_watchlist' Tag on Radarr/Sonarr — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every movie/series `arr_sync.py` adds to Radarr/Sonarr — and every already-in-library item that matches a cached IMDb watchlist — gets tagged `imdb_watchlist` in that Radarr/Sonarr instance.

**Architecture:** `RadarrClient`/`SonarrClient` gain three methods each (`get_or_create_tag_id`, a richer `get_library_by_imdb` replacing `get_library_imdb_ids`, and `update_movie`/`update_series` via a new `_put` helper). `_sync_movies`/`_sync_tv` resolve the tag once per cycle, include it on new-item `POST` payloads, and PUT-update any already-in-library item that's missing it. Tag resolution failure degrades to "skip tagging this cycle," never blocks adds.

**Tech Stack:** Python, `requests`, `pytest` + `unittest.mock` (existing patterns in `tests/test_arr_sync.py`).

**Design doc:** `docs/superpowers/specs/2026-07-18-imdb-watchlist-tag-design.md`

---

### Task 1: RadarrClient — tag resolution, richer library lookup, update support

**Files:**
- Modify: `arr_sync.py:102-153` (`RadarrClient` class)
- Test: `tests/test_arr_sync.py:116-198` (Radarr client tests)

- [ ] **Step 1: Replace the `get_library_imdb_ids` test and add new client tests**

Delete this test (lines 116-124):

```python
def test_radarr_get_library_imdb_ids():
    client = arr_sync.RadarrClient("https://radarr.example.com", "key")
    movies = [{"imdbId": "tt1"}, {"imdbId": "tt2"}, {"title": "no imdb id"}]
    with patch("arr_sync.requests.get", return_value=_fake_response(movies)) as mock_get:
        result = client.get_library_imdb_ids()
    assert result == {"tt1", "tt2"}
    mock_get.assert_called_once_with(
        "https://radarr.example.com/api/v3/movie", headers={"X-Api-Key": "key"}, timeout=30)
```

Replace it with:

```python
def test_radarr_get_library_by_imdb():
    client = arr_sync.RadarrClient("https://radarr.example.com", "key")
    movies = [{"id": 1, "imdbId": "tt1", "tags": []}, {"id": 2, "imdbId": "tt2", "tags": [5]},
              {"id": 3, "title": "no imdb id"}]
    with patch("arr_sync.requests.get", return_value=_fake_response(movies)) as mock_get:
        result = client.get_library_by_imdb()
    assert result == {"tt1": movies[0], "tt2": movies[1]}
    mock_get.assert_called_once_with(
        "https://radarr.example.com/api/v3/movie", headers={"X-Api-Key": "key"}, timeout=30)


def test_radarr_get_or_create_tag_id_returns_existing_tag():
    client = arr_sync.RadarrClient("https://radarr.example.com", "key")
    tags = [{"id": 1, "label": "other"}, {"id": 7, "label": "IMDB_Watchlist"}]
    with patch("arr_sync.requests.get", return_value=_fake_response(tags)):
        with patch("arr_sync.requests.post") as mock_post:
            result = client.get_or_create_tag_id("imdb_watchlist")
    assert result == 7
    mock_post.assert_not_called()


def test_radarr_get_or_create_tag_id_creates_missing_tag():
    client = arr_sync.RadarrClient("https://radarr.example.com", "key")
    with patch("arr_sync.requests.get", return_value=_fake_response([])):
        with patch("arr_sync.requests.post",
                    return_value=_fake_response({"id": 9, "label": "imdb_watchlist"})) as mock_post:
            result = client.get_or_create_tag_id("imdb_watchlist")
    assert result == 9
    mock_post.assert_called_once_with(
        "https://radarr.example.com/api/v3/tag", headers={"X-Api-Key": "key"},
        json={"label": "imdb_watchlist"}, timeout=30)


def test_radarr_update_movie_puts_full_payload():
    client = arr_sync.RadarrClient("https://radarr.example.com", "key")
    movie = {"id": 501, "title": "Already Have It", "tags": [7]}
    with patch("arr_sync.requests.put", return_value=_fake_response(movie)) as mock_put:
        result = client.update_movie(movie)
    assert result == movie
    mock_put.assert_called_once_with(
        "https://radarr.example.com/api/v3/movie/501", headers={"X-Api-Key": "key"},
        json=movie, timeout=30)
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `./test.sh -k "test_radarr_get_library_by_imdb or test_radarr_get_or_create_tag_id or test_radarr_update_movie"`
Expected: FAIL — `AttributeError` / `RadarrClient` has no attribute `get_library_by_imdb` (etc.), since these methods don't exist yet.

- [ ] **Step 3: Implement the new/changed `RadarrClient` methods**

In `arr_sync.py`, add a `_put` helper right after `_post` (currently lines 112-115):

```python
    def _put(self, path: str, payload: dict):
        resp = requests.put(f"{self.base_url}/api/v3{path}", headers=self.headers, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()
```

Replace `get_library_imdb_ids` (currently lines 117-118):

```python
    def get_library_imdb_ids(self) -> set:
        return {m["imdbId"] for m in self._get("/movie") if m.get("imdbId")}
```

with:

```python
    def get_library_by_imdb(self) -> dict:
        return {m["imdbId"]: m for m in self._get("/movie") if m.get("imdbId")}

    def get_or_create_tag_id(self, label: str) -> Optional[int]:
        for tag in self._get("/tag"):
            if tag.get("label", "").lower() == label.lower():
                return tag["id"]
        created = self._post("/tag", {"label": label})
        return created["id"]
```

Add `update_movie` right after `add_movie` (end of the class, currently ending at line 153):

```python
    def update_movie(self, movie: dict) -> dict:
        return self._put(f"/movie/{movie['id']}", movie)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./test.sh -k "test_radarr"`
Expected: PASS (all Radarr client tests, including the untouched pre-existing ones).

- [ ] **Step 5: Commit**

```bash
git add arr_sync.py tests/test_arr_sync.py
git commit -m "feat: add tag resolution and update support to RadarrClient"
```

---

### Task 2: SonarrClient — tag resolution, richer library lookup, update support

**Files:**
- Modify: `arr_sync.py:156-204` (`SonarrClient` class)
- Test: `tests/test_arr_sync.py:200-256` (Sonarr client tests)

- [ ] **Step 1: Replace the `get_library_imdb_ids` test and add new client tests**

Delete this test (lines 200-204):

```python
def test_sonarr_get_library_imdb_ids():
    client = arr_sync.SonarrClient("https://sonarr.example.com", "key")
    series = [{"imdbId": "tt1"}, {"imdbId": "tt2"}, {"title": "no imdb id"}]
    with patch("arr_sync.requests.get", return_value=_fake_response(series)):
        assert client.get_library_imdb_ids() == {"tt1", "tt2"}
```

Replace it with:

```python
def test_sonarr_get_library_by_imdb():
    client = arr_sync.SonarrClient("https://sonarr.example.com", "key")
    series = [{"id": 1, "imdbId": "tt1", "tags": []}, {"id": 2, "imdbId": "tt2", "tags": [5]},
              {"id": 3, "title": "no imdb id"}]
    with patch("arr_sync.requests.get", return_value=_fake_response(series)):
        assert client.get_library_by_imdb() == {"tt1": series[0], "tt2": series[1]}


def test_sonarr_get_or_create_tag_id_returns_existing_tag():
    client = arr_sync.SonarrClient("https://sonarr.example.com", "key")
    tags = [{"id": 1, "label": "other"}, {"id": 7, "label": "IMDB_Watchlist"}]
    with patch("arr_sync.requests.get", return_value=_fake_response(tags)):
        with patch("arr_sync.requests.post") as mock_post:
            result = client.get_or_create_tag_id("imdb_watchlist")
    assert result == 7
    mock_post.assert_not_called()


def test_sonarr_get_or_create_tag_id_creates_missing_tag():
    client = arr_sync.SonarrClient("https://sonarr.example.com", "key")
    with patch("arr_sync.requests.get", return_value=_fake_response([])):
        with patch("arr_sync.requests.post",
                    return_value=_fake_response({"id": 9, "label": "imdb_watchlist"})) as mock_post:
            result = client.get_or_create_tag_id("imdb_watchlist")
    assert result == 9
    mock_post.assert_called_once_with(
        "https://sonarr.example.com/api/v3/tag", headers={"X-Api-Key": "key"},
        json={"label": "imdb_watchlist"}, timeout=30)


def test_sonarr_update_series_puts_full_payload():
    client = arr_sync.SonarrClient("https://sonarr.example.com", "key")
    series = {"id": 501, "title": "Breaking Bad", "tags": [7]}
    with patch("arr_sync.requests.put", return_value=_fake_response(series)) as mock_put:
        result = client.update_series(series)
    assert result == series
    mock_put.assert_called_once_with(
        "https://sonarr.example.com/api/v3/series/501", headers={"X-Api-Key": "key"},
        json=series, timeout=30)
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `./test.sh -k "test_sonarr_get_library_by_imdb or test_sonarr_get_or_create_tag_id or test_sonarr_update_series"`
Expected: FAIL — `AttributeError` on the missing methods.

- [ ] **Step 3: Implement the new/changed `SonarrClient` methods**

Add a `_put` helper right after `_post` (currently lines 166-169):

```python
    def _put(self, path: str, payload: dict):
        resp = requests.put(f"{self.base_url}/api/v3{path}", headers=self.headers, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()
```

Replace `get_library_imdb_ids` (currently lines 171-172):

```python
    def get_library_imdb_ids(self) -> set:
        return {s["imdbId"] for s in self._get("/series") if s.get("imdbId")}
```

with:

```python
    def get_library_by_imdb(self) -> dict:
        return {s["imdbId"]: s for s in self._get("/series") if s.get("imdbId")}

    def get_or_create_tag_id(self, label: str) -> Optional[int]:
        for tag in self._get("/tag"):
            if tag.get("label", "").lower() == label.lower():
                return tag["id"]
        created = self._post("/tag", {"label": label})
        return created["id"]
```

Add `update_series` right after `add_series` (end of the class, currently ending at line 204):

```python
    def update_series(self, series: dict) -> dict:
        return self._put(f"/series/{series['id']}", series)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./test.sh -k "test_sonarr"`
Expected: PASS (all Sonarr client tests, including the untouched pre-existing ones).

- [ ] **Step 5: Commit**

```bash
git add arr_sync.py tests/test_arr_sync.py
git commit -m "feat: add tag resolution and update support to SonarrClient"
```

---

### Task 3: `_sync_movies` — apply the tag to new and existing movies

**Files:**
- Modify: `arr_sync.py:207-270` (`_sync_movies`)
- Test: `tests/test_arr_sync.py` (movie sync tests, listed below)

- [ ] **Step 1: Update existing `_sync_movies` tests for the new client API and counts shape, and add new tagging tests**

Replace `test_sync_movies_skips_items_already_in_library` with:

```python
def test_sync_movies_skips_items_already_in_library():
    client = MagicMock()
    client.get_library_by_imdb.return_value = {"tt1": {"id": 501, "title": "Already Have It", "tags": []}}
    client.get_excluded_tmdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/Movies"
    client.get_or_create_tag_id.return_value = None
    with patch.object(arr_sync, "RadarrClient", lambda url, key: client):
        counts = arr_sync._sync_movies(_base_config(), [{"imdb": "tt1", "title": "Already Have It"}],
                                         threading.Event())

    assert counts["skipped_existing"] == 1
    assert counts["added"] == 0
    client.lookup_by_imdb.assert_not_called()
```

Replace `test_sync_movies_skips_excluded_items`:

```python
def test_sync_movies_skips_excluded_items():
    client = MagicMock()
    client.get_library_by_imdb.return_value = {}
    client.get_excluded_tmdb_ids.return_value = {603}
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/Movies"
    client.get_or_create_tag_id.return_value = None
    client.lookup_by_imdb.return_value = {"title": "The Matrix", "tmdbId": 603}
    with patch.object(arr_sync, "RadarrClient", lambda url, key: client):
        counts = arr_sync._sync_movies(_base_config(), [{"imdb": "tt0133093", "title": "The Matrix"}],
                                         threading.Event())

    assert counts["skipped_excluded"] == 1
    assert counts["added"] == 0
    client.add_movie.assert_not_called()
```

Replace `test_sync_movies_dry_run_does_not_call_add`:

```python
def test_sync_movies_dry_run_does_not_call_add():
    client = MagicMock()
    client.get_library_by_imdb.return_value = {}
    client.get_excluded_tmdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/Movies"
    client.get_or_create_tag_id.return_value = None
    client.lookup_by_imdb.return_value = {"title": "The Matrix", "tmdbId": 603}
    with patch.object(arr_sync, "RadarrClient", lambda url, key: client):
        counts = arr_sync._sync_movies(_base_config(dry_run=True),
                                         [{"imdb": "tt0133093", "title": "The Matrix"}], threading.Event())

    assert counts["would_add"] == 1
    client.add_movie.assert_not_called()
```

Replace `test_sync_movies_adds_new_item`:

```python
def test_sync_movies_adds_new_item():
    client = MagicMock()
    client.get_library_by_imdb.return_value = {}
    client.get_excluded_tmdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/Movies"
    client.get_or_create_tag_id.return_value = None
    client.lookup_by_imdb.return_value = {"title": "The Matrix", "tmdbId": 603}
    client.add_movie.return_value = {"id": 42, "title": "The Matrix", "tmdbId": 603, "tags": []}
    with patch.object(arr_sync, "RadarrClient", lambda url, key: client):
        counts = arr_sync._sync_movies(_base_config(), [{"imdb": "tt0133093", "title": "The Matrix"}],
                                         threading.Event())

    assert counts["added"] == 1
    client.add_movie.assert_called_once()
```

Replace `test_sync_movies_dedupes_same_imdb_id_across_two_cached_users`:

```python
def test_sync_movies_dedupes_same_imdb_id_across_two_cached_users():
    client = MagicMock()
    client.get_library_by_imdb.return_value = {}
    client.get_excluded_tmdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/Movies"
    client.get_or_create_tag_id.return_value = None
    client.lookup_by_imdb.return_value = {"title": "The Matrix", "tmdbId": 603}
    client.add_movie.return_value = {"id": 42, "title": "The Matrix", "tmdbId": 603, "tags": []}
    items = [
        {"imdb": "tt0133093", "title": "The Matrix"},
        {"imdb": "tt0133093", "title": "The Matrix"},
    ]
    with patch.object(arr_sync, "RadarrClient", lambda url, key: client):
        counts = arr_sync._sync_movies(_base_config(), items, threading.Event())

    assert counts["added"] == 1
    assert counts["skipped_existing"] == 1
    assert counts["failed"] == 0
    assert client.add_movie.call_count == 1
```

Replace `test_sync_movies_counts_failed_lookup`:

```python
def test_sync_movies_counts_failed_lookup():
    client = MagicMock()
    client.get_library_by_imdb.return_value = {}
    client.get_excluded_tmdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/Movies"
    client.get_or_create_tag_id.return_value = None
    client.lookup_by_imdb.return_value = None
    with patch.object(arr_sync, "RadarrClient", lambda url, key: client):
        counts = arr_sync._sync_movies(_base_config(), [{"imdb": "tt9999999", "title": "Unknown"}],
                                         threading.Event())

    assert counts["failed"] == 1
```

Replace `test_sync_movies_stops_early_when_stop_event_set`:

```python
def test_sync_movies_stops_early_when_stop_event_set():
    client = MagicMock()
    client.get_library_by_imdb.return_value = {}
    client.get_excluded_tmdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/Movies"
    client.get_or_create_tag_id.return_value = None
    stop_event = threading.Event()
    stop_event.set()
    with patch.object(arr_sync, "RadarrClient", lambda url, key: client):
        counts = arr_sync._sync_movies(_base_config(), [{"imdb": "tt1", "title": "X"}], stop_event)

    assert counts == {"added": 0, "would_add": 0, "skipped_existing": 0, "skipped_excluded": 0,
                       "failed": 0, "tagged": 0, "would_tag": 0}
    client.lookup_by_imdb.assert_not_called()
```

`test_sync_movies_skips_when_radarr_not_configured` needs no change (it returns before touching the client).

Now add five new tests, placed after `test_sync_movies_skips_when_radarr_not_configured`:

```python
def test_sync_movies_tags_new_item_on_add():
    client = MagicMock()
    client.get_library_by_imdb.return_value = {}
    client.get_excluded_tmdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/Movies"
    client.get_or_create_tag_id.return_value = 99
    client.lookup_by_imdb.return_value = {"title": "The Matrix", "tmdbId": 603, "tags": []}
    client.add_movie.return_value = {"id": 42, "title": "The Matrix", "tmdbId": 603, "tags": [99]}
    with patch.object(arr_sync, "RadarrClient", lambda url, key: client):
        counts = arr_sync._sync_movies(_base_config(), [{"imdb": "tt0133093", "title": "The Matrix"}],
                                         threading.Event())

    assert counts["added"] == 1
    sent_movie = client.add_movie.call_args.args[0]
    assert sent_movie["tags"] == [99]


def test_sync_movies_tags_existing_untagged_item():
    client = MagicMock()
    client.get_library_by_imdb.return_value = {"tt1": {"id": 501, "title": "Already Have It", "tags": []}}
    client.get_excluded_tmdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/Movies"
    client.get_or_create_tag_id.return_value = 99
    with patch.object(arr_sync, "RadarrClient", lambda url, key: client):
        counts = arr_sync._sync_movies(_base_config(), [{"imdb": "tt1", "title": "Already Have It"}],
                                         threading.Event())

    assert counts["skipped_existing"] == 1
    assert counts["tagged"] == 1
    client.update_movie.assert_called_once_with({"id": 501, "title": "Already Have It", "tags": [99]})


def test_sync_movies_does_not_retag_already_tagged_item():
    client = MagicMock()
    client.get_library_by_imdb.return_value = {"tt1": {"id": 501, "title": "Already Have It", "tags": [99]}}
    client.get_excluded_tmdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/Movies"
    client.get_or_create_tag_id.return_value = 99
    with patch.object(arr_sync, "RadarrClient", lambda url, key: client):
        counts = arr_sync._sync_movies(_base_config(), [{"imdb": "tt1", "title": "Already Have It"}],
                                         threading.Event())

    assert counts["skipped_existing"] == 1
    assert counts["tagged"] == 0
    client.update_movie.assert_not_called()


def test_sync_movies_dry_run_would_tag_existing_item():
    client = MagicMock()
    client.get_library_by_imdb.return_value = {"tt1": {"id": 501, "title": "Already Have It", "tags": []}}
    client.get_excluded_tmdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/Movies"
    client.get_or_create_tag_id.return_value = 99
    with patch.object(arr_sync, "RadarrClient", lambda url, key: client):
        counts = arr_sync._sync_movies(_base_config(dry_run=True),
                                         [{"imdb": "tt1", "title": "Already Have It"}], threading.Event())

    assert counts["would_tag"] == 1
    client.update_movie.assert_not_called()


def test_sync_movies_tag_put_failure_does_not_abort_cycle():
    client = MagicMock()
    client.get_library_by_imdb.return_value = {"tt1": {"id": 501, "title": "Already Have It", "tags": []}}
    client.get_excluded_tmdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/Movies"
    client.get_or_create_tag_id.return_value = 99
    client.update_movie.side_effect = RuntimeError("boom")
    with patch.object(arr_sync, "RadarrClient", lambda url, key: client):
        counts = arr_sync._sync_movies(_base_config(), [{"imdb": "tt1", "title": "Already Have It"}],
                                         threading.Event())

    assert counts["skipped_existing"] == 1
    assert counts["tagged"] == 0


def test_sync_movies_tag_resolution_failure_still_allows_add():
    client = MagicMock()
    client.get_library_by_imdb.return_value = {}
    client.get_excluded_tmdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/Movies"
    client.get_or_create_tag_id.side_effect = RuntimeError("tag api down")
    client.lookup_by_imdb.return_value = {"title": "The Matrix", "tmdbId": 603, "tags": []}
    client.add_movie.return_value = {"id": 42, "title": "The Matrix", "tmdbId": 603, "tags": []}
    with patch.object(arr_sync, "RadarrClient", lambda url, key: client):
        counts = arr_sync._sync_movies(_base_config(), [{"imdb": "tt0133093", "title": "The Matrix"}],
                                         threading.Event())

    assert counts["added"] == 1
    sent_movie = client.add_movie.call_args.args[0]
    assert sent_movie["tags"] == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./test.sh -k test_sync_movies`
Expected: FAIL — `_sync_movies` still calls the removed `get_library_imdb_ids`/never touches tags, so the updated and new tests fail (`AttributeError`-driven wrong branches, or missing `tagged`/`would_tag` keys).

- [ ] **Step 3: Rewrite `_sync_movies`**

Replace the whole function (currently lines 207-270) with:

```python
def _sync_movies(config: dict, items: list, stop_event) -> dict:
    counts = {"added": 0, "would_add": 0, "skipped_existing": 0, "skipped_excluded": 0,
              "failed": 0, "tagged": 0, "would_tag": 0}
    radarr_config = config["radarr"]
    if not radarr_config.get("url") or not radarr_config.get("api_key"):
        logger.info("Radarr not configured, skipping movies")
        return counts

    client = RadarrClient(radarr_config["url"], radarr_config["api_key"])
    try:
        existing_library = client.get_library_by_imdb()
        excluded_tmdb_ids = client.get_excluded_tmdb_ids()
        quality_profile_id = client.resolve_quality_profile_id(radarr_config["quality_profile"])
        root_folder_path = client.resolve_root_folder_path(radarr_config["root_folder_path"])
    except Exception:
        logger.error("Failed to fetch Radarr library/exclusions/profile/root folder", exc_info=True)
        return counts

    if quality_profile_id is None:
        logger.error("Radarr quality profile '%s' not found, skipping movies",
                      radarr_config["quality_profile"])
        return counts
    if root_folder_path is None:
        logger.error("Radarr root folder is ambiguous or unset, skipping movies")
        return counts

    try:
        tag_id = client.get_or_create_tag_id("imdb_watchlist")
    except Exception:
        logger.warning("Failed to resolve imdb_watchlist tag on Radarr, continuing without tagging",
                        exc_info=True)
        tag_id = None

    for item in items:
        if stop_event.is_set():
            logger.info("Stop requested, halting Radarr sync early")
            break

        imdb_id = item.get("imdb") or item.get("imdbId") or item.get("imdb_id")
        existing_movie = existing_library.get(imdb_id) if imdb_id else None
        if not imdb_id or existing_movie is not None:
            if (existing_movie is not None and tag_id is not None
                    and tag_id not in existing_movie.get("tags", [])):
                if config["dry_run"]:
                    logger.info("[dry run] would tag existing movie: %s", existing_movie.get("title"))
                    counts["would_tag"] += 1
                else:
                    try:
                        updated_tags = list(existing_movie.get("tags", [])) + [tag_id]
                        client.update_movie({**existing_movie, "tags": updated_tags})
                        existing_movie["tags"] = updated_tags
                        logger.info("Tagged existing movie: %s", existing_movie.get("title"))
                        counts["tagged"] += 1
                    except Exception:
                        logger.error("Failed to tag existing movie %s (%s)",
                                      existing_movie.get("title"), imdb_id, exc_info=True)
            counts["skipped_existing"] += 1
            continue

        try:
            movie = client.lookup_by_imdb(imdb_id)
            if not movie:
                logger.warning("No Radarr lookup match for %s (%s)", item.get("title"), imdb_id)
                counts["failed"] += 1
                continue
            if movie.get("tmdbId") in excluded_tmdb_ids:
                logger.info("Skipping excluded movie: %s", movie.get("title"))
                counts["skipped_excluded"] += 1
                continue
            if config["dry_run"]:
                logger.info("[dry run] would add movie: %s", movie.get("title"))
                counts["would_add"] += 1
                continue
            if tag_id is not None:
                movie["tags"] = [tag_id]
            added = client.add_movie(
                movie,
                quality_profile_id=quality_profile_id,
                root_folder_path=root_folder_path,
                minimum_availability=radarr_config["minimum_availability"],
                search_on_add=radarr_config["search_on_add"],
            )
            logger.info("Added movie: %s", movie.get("title"))
            counts["added"] += 1
            existing_library[imdb_id] = added
        except Exception:
            logger.error("Failed to add movie %s (%s)", item.get("title"), imdb_id, exc_info=True)
            counts["failed"] += 1

    return counts
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./test.sh -k test_sync_movies`
Expected: PASS (all listed tests, old and new).

- [ ] **Step 5: Commit**

```bash
git add arr_sync.py tests/test_arr_sync.py
git commit -m "feat: tag new and existing movies with imdb_watchlist in Radarr"
```

---

### Task 4: `_sync_tv` — apply the tag to new and existing series

**Files:**
- Modify: `arr_sync.py:273-338` (`_sync_tv`)
- Test: `tests/test_arr_sync.py` (new tv sync tests — there is no pre-existing granular `_sync_tv` coverage to update, only `test_run_sync_splits_movie_and_tv_items`, which mocks `_sync_tv` entirely and needs no change)

- [ ] **Step 1: Add tv sync tests (mirroring the movie ones from Task 3)**

Add these tests to `tests/test_arr_sync.py`, placed after `test_run_sync_splits_movie_and_tv_items`:

```python
def test_sync_tv_tags_new_item_on_add():
    client = MagicMock()
    client.get_library_by_imdb.return_value = {}
    client.get_excluded_tvdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/TVs"
    client.get_or_create_tag_id.return_value = 99
    client.lookup_by_imdb.return_value = {"title": "Breaking Bad", "tvdbId": 81189, "tags": []}
    client.add_series.return_value = {"id": 7, "title": "Breaking Bad", "tvdbId": 81189, "tags": [99]}
    with patch.object(arr_sync, "SonarrClient", lambda url, key: client):
        counts = arr_sync._sync_tv(_base_config(), [{"imdb": "tt0903747", "title": "Breaking Bad"}],
                                     threading.Event())

    assert counts["added"] == 1
    sent_series = client.add_series.call_args.args[0]
    assert sent_series["tags"] == [99]


def test_sync_tv_tags_existing_untagged_item():
    client = MagicMock()
    client.get_library_by_imdb.return_value = {"tt1": {"id": 501, "title": "Breaking Bad", "tags": []}}
    client.get_excluded_tvdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/TVs"
    client.get_or_create_tag_id.return_value = 99
    with patch.object(arr_sync, "SonarrClient", lambda url, key: client):
        counts = arr_sync._sync_tv(_base_config(), [{"imdb": "tt1", "title": "Breaking Bad"}],
                                     threading.Event())

    assert counts["skipped_existing"] == 1
    assert counts["tagged"] == 1
    client.update_series.assert_called_once_with({"id": 501, "title": "Breaking Bad", "tags": [99]})


def test_sync_tv_does_not_retag_already_tagged_item():
    client = MagicMock()
    client.get_library_by_imdb.return_value = {"tt1": {"id": 501, "title": "Breaking Bad", "tags": [99]}}
    client.get_excluded_tvdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/TVs"
    client.get_or_create_tag_id.return_value = 99
    with patch.object(arr_sync, "SonarrClient", lambda url, key: client):
        counts = arr_sync._sync_tv(_base_config(), [{"imdb": "tt1", "title": "Breaking Bad"}],
                                     threading.Event())

    assert counts["skipped_existing"] == 1
    assert counts["tagged"] == 0
    client.update_series.assert_not_called()


def test_sync_tv_dry_run_would_tag_existing_item():
    client = MagicMock()
    client.get_library_by_imdb.return_value = {"tt1": {"id": 501, "title": "Breaking Bad", "tags": []}}
    client.get_excluded_tvdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/TVs"
    client.get_or_create_tag_id.return_value = 99
    with patch.object(arr_sync, "SonarrClient", lambda url, key: client):
        counts = arr_sync._sync_tv(_base_config(dry_run=True),
                                     [{"imdb": "tt1", "title": "Breaking Bad"}], threading.Event())

    assert counts["would_tag"] == 1
    client.update_series.assert_not_called()


def test_sync_tv_tag_put_failure_does_not_abort_cycle():
    client = MagicMock()
    client.get_library_by_imdb.return_value = {"tt1": {"id": 501, "title": "Breaking Bad", "tags": []}}
    client.get_excluded_tvdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/TVs"
    client.get_or_create_tag_id.return_value = 99
    client.update_series.side_effect = RuntimeError("boom")
    with patch.object(arr_sync, "SonarrClient", lambda url, key: client):
        counts = arr_sync._sync_tv(_base_config(), [{"imdb": "tt1", "title": "Breaking Bad"}],
                                     threading.Event())

    assert counts["skipped_existing"] == 1
    assert counts["tagged"] == 0


def test_sync_tv_tag_resolution_failure_still_allows_add():
    client = MagicMock()
    client.get_library_by_imdb.return_value = {}
    client.get_excluded_tvdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/TVs"
    client.get_or_create_tag_id.side_effect = RuntimeError("tag api down")
    client.lookup_by_imdb.return_value = {"title": "Breaking Bad", "tvdbId": 81189, "tags": []}
    client.add_series.return_value = {"id": 7, "title": "Breaking Bad", "tvdbId": 81189, "tags": []}
    with patch.object(arr_sync, "SonarrClient", lambda url, key: client):
        counts = arr_sync._sync_tv(_base_config(), [{"imdb": "tt0903747", "title": "Breaking Bad"}],
                                     threading.Event())

    assert counts["added"] == 1
    sent_series = client.add_series.call_args.args[0]
    assert sent_series["tags"] == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./test.sh -k test_sync_tv`
Expected: FAIL — `_sync_tv` doesn't know about tags yet (`get_library_by_imdb` doesn't exist on the real code path, counts dict lacks `tagged`/`would_tag`).

- [ ] **Step 3: Rewrite `_sync_tv`**

Replace the whole function (currently lines 273-338) with:

```python
def _sync_tv(config: dict, items: list, stop_event) -> dict:
    counts = {"added": 0, "would_add": 0, "skipped_existing": 0, "skipped_excluded": 0,
              "failed": 0, "tagged": 0, "would_tag": 0}
    sonarr_config = config["sonarr"]
    if not sonarr_config.get("url") or not sonarr_config.get("api_key"):
        logger.info("Sonarr not configured, skipping tv shows")
        return counts

    client = SonarrClient(sonarr_config["url"], sonarr_config["api_key"])
    try:
        existing_library = client.get_library_by_imdb()
        excluded_tvdb_ids = client.get_excluded_tvdb_ids()
        quality_profile_id = client.resolve_quality_profile_id(sonarr_config["quality_profile"])
        root_folder_path = client.resolve_root_folder_path(sonarr_config["root_folder_path"])
    except Exception:
        logger.error("Failed to fetch Sonarr library/exclusions/profile/root folder", exc_info=True)
        return counts

    if quality_profile_id is None:
        logger.error("Sonarr quality profile '%s' not found, skipping tv shows",
                      sonarr_config["quality_profile"])
        return counts
    if root_folder_path is None:
        logger.error("Sonarr root folder is ambiguous or unset, skipping tv shows")
        return counts

    try:
        tag_id = client.get_or_create_tag_id("imdb_watchlist")
    except Exception:
        logger.warning("Failed to resolve imdb_watchlist tag on Sonarr, continuing without tagging",
                        exc_info=True)
        tag_id = None

    for item in items:
        if stop_event.is_set():
            logger.info("Stop requested, halting Sonarr sync early")
            break

        imdb_id = item.get("imdb") or item.get("imdbId") or item.get("imdb_id")
        existing_series = existing_library.get(imdb_id) if imdb_id else None
        if not imdb_id or existing_series is not None:
            if (existing_series is not None and tag_id is not None
                    and tag_id not in existing_series.get("tags", [])):
                if config["dry_run"]:
                    logger.info("[dry run] would tag existing series: %s", existing_series.get("title"))
                    counts["would_tag"] += 1
                else:
                    try:
                        updated_tags = list(existing_series.get("tags", [])) + [tag_id]
                        client.update_series({**existing_series, "tags": updated_tags})
                        existing_series["tags"] = updated_tags
                        logger.info("Tagged existing series: %s", existing_series.get("title"))
                        counts["tagged"] += 1
                    except Exception:
                        logger.error("Failed to tag existing series %s (%s)",
                                      existing_series.get("title"), imdb_id, exc_info=True)
            counts["skipped_existing"] += 1
            continue

        try:
            series = client.lookup_by_imdb(imdb_id)
            if not series:
                logger.warning("No Sonarr lookup match for %s (%s)", item.get("title"), imdb_id)
                counts["failed"] += 1
                continue
            if series.get("tvdbId") in excluded_tvdb_ids:
                logger.info("Skipping excluded series: %s", series.get("title"))
                counts["skipped_excluded"] += 1
                continue
            if config["dry_run"]:
                logger.info("[dry run] would add series: %s", series.get("title"))
                counts["would_add"] += 1
                continue
            if tag_id is not None:
                series["tags"] = [tag_id]
            added = client.add_series(
                series,
                quality_profile_id=quality_profile_id,
                root_folder_path=root_folder_path,
                series_type=sonarr_config["series_type"],
                season_folder=sonarr_config["season_folder"],
                monitor=sonarr_config["monitor"],
                search_on_add=sonarr_config["search_on_add"],
            )
            logger.info("Added series: %s", series.get("title"))
            counts["added"] += 1
            existing_library[imdb_id] = added
        except Exception:
            logger.error("Failed to add series %s (%s)", item.get("title"), imdb_id, exc_info=True)
            counts["failed"] += 1

    return counts
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./test.sh -k test_sync_tv`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add arr_sync.py tests/test_arr_sync.py
git commit -m "feat: tag new and existing series with imdb_watchlist in Sonarr"
```

---

### Task 5: Status page — show Tagged / Would tag counts

**Files:**
- Modify: `arr_sync.py:464-529` (`_render_sync_page`)
- Test: `tests/test_arr_sync.py:620-625` (`test_sync_page_renders_html_with_buttons`)

- [ ] **Step 1: Add a test asserting the new columns render**

Add this test right after `test_sync_page_renders_html_with_buttons`:

```python
def test_sync_page_renders_tagged_and_would_tag_columns():
    response = sync_client.get("/sync")
    assert response.status_code == 200
    assert "<th>Tagged</th>" in response.text
    assert "<th>Would tag" in response.text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./test.sh -k test_sync_page_renders_tagged_and_would_tag_columns`
Expected: FAIL — the table has no `Tagged`/`Would tag` columns yet.

- [ ] **Step 3: Update `counts_row` and the table header**

In `_render_sync_page`, replace the `counts_row` helper (currently lines 484-492):

```python
    def counts_row(service, counts):
        return (
            f"<tr><td>{service}</td>"
            f"<td>{counts.get('added', '-')}</td>"
            f"<td>{counts.get('would_add', '-')}</td>"
            f"<td>{counts.get('skipped_existing', '-')}</td>"
            f"<td>{counts.get('skipped_excluded', '-')}</td>"
            f"<td>{counts.get('failed', '-')}</td></tr>"
        )
```

with:

```python
    def counts_row(service, counts):
        return (
            f"<tr><td>{service}</td>"
            f"<td>{counts.get('added', '-')}</td>"
            f"<td>{counts.get('would_add', '-')}</td>"
            f"<td>{counts.get('skipped_existing', '-')}</td>"
            f"<td>{counts.get('skipped_excluded', '-')}</td>"
            f"<td>{counts.get('failed', '-')}</td>"
            f"<td>{counts.get('tagged', '-')}</td>"
            f"<td>{counts.get('would_tag', '-')}</td></tr>"
        )
```

Then, in the returned HTML template (currently line 515), replace:

```html
    <tr><th>Service</th><th>Added</th><th>Would add (dry run)</th><th>Skipped (existing)</th><th>Skipped (excluded)</th><th>Failed</th></tr>
```

with:

```html
    <tr><th>Service</th><th>Added</th><th>Would add (dry run)</th><th>Skipped (existing)</th><th>Skipped (excluded)</th><th>Failed</th><th>Tagged</th><th>Would tag (dry run)</th></tr>
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `./test.sh -k "test_sync_page"`
Expected: PASS (including the pre-existing `test_sync_page_renders_html_with_buttons`, `test_sync_page_escapes_log_content`, `test_sync_page_escapes_error_content`).

- [ ] **Step 5: Commit**

```bash
git add arr_sync.py tests/test_arr_sync.py
git commit -m "feat: show tagged/would-tag counts on the sync status page"
```

---

### Task 6: Document the tagging behavior in CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add a paragraph to the "Arr sync" section**

In `CLAUDE.md`, find this paragraph (in the `### Arr sync` section):

```
Exclusion lists on Radarr/Sonarr are keyed by `tmdbId`/`tvdbId`, not `imdbId` — so checking a candidate against them requires the `/movie/lookup/imdb` or `/series/lookup?term=imdb:...` metadata call first. Checking whether an item is *already in the library* does not need this lookup, since both APIs expose `imdbId` directly on library items.
```

and add this paragraph immediately after it:

```
Every item added to Radarr/Sonarr by this module — and, on every cycle, any watchlist-matched item already in the library — gets tagged `imdb_watchlist` there. The tag name is a hardcoded constant, not configurable. It's resolved (or created if missing) once per service per cycle via `get_or_create_tag_id`; if that call fails, the cycle logs a warning and proceeds without tagging rather than blocking adds — tagging is additive, never a gate on the add/skip/exclude logic. A single item's tag update failing is likewise logged and skipped, not raised.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document imdb_watchlist tagging behavior in arr_sync"
```

---

### Task 7: Full regression run

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `./test.sh`
Expected: PASS — every test in `tests/test_imdb_server.py` and `tests/test_arr_sync.py`, including all tests touched or added in Tasks 1-6.

- [ ] **Step 2: Spot-check no stray references to the removed `get_library_imdb_ids` name remain**

Run: `grep -rn "get_library_imdb_ids" --include="*.py" .`
Expected: no output (method fully renamed to `get_library_by_imdb` everywhere in source and tests — this deliberately excludes `docs/`, since the plan and spec markdown files quote the old name in their "replace this" instructions).
