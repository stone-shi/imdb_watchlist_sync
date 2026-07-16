# Periodic Radarr/Sonarr Watchlist Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Periodically check all cached IMDb watchlists against a configured Radarr/Sonarr pair, auto-add anything missing (and not import-list-excluded), and expose status/log/manual trigger/stop via a web page.

**Architecture:** A new module `arr_sync.py` owns config loading (`data/config.json` + env overrides), two small HTTP clients (`RadarrClient`, `SonarrClient`), the sync orchestration logic, a thread-based guard/timeout/stop state machine (sync work is synchronous `requests` calls, so it runs in a `threading.Thread` per the existing codebase pattern), an `asyncio` scheduler loop, and a FastAPI `APIRouter` with the status page + JSON/trigger/stop endpoints. `imdb_server.py` wires the scheduler into its `lifespan` and mounts the router.

**Tech Stack:** Python, FastAPI, `requests`, `threading`, `asyncio`, `python-dotenv` (already a dependency via `embedding.py`'s pattern), `pytest` + `unittest.mock`.

Full design reference: `docs/superpowers/specs/2026-07-16-arr-watchlist-sync-design.md`.

---

### Task 1: Config loading

**Files:**
- Create: `arr_sync.py`
- Test: Create `tests/test_arr_sync.py`

- [ ] **Step 1: Write the failing tests**

```python
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

import arr_sync

ENV_VARS = [
    "ARR_SYNC_POLL_INTERVAL_SECONDS", "ARR_SYNC_TIMEOUT_SECONDS",
    "SONARR_URL", "SONARR_API_KEY", "RADARR_URL", "RADARR_API_KEY",
]


@pytest.fixture(autouse=True)
def clear_arr_env(monkeypatch):
    # A real .env (added in Task 11) may set these for local testing against
    # the real servers; every test must start from a clean slate regardless.
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_load_arr_config_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(arr_sync, "CONFIG_FILE", str(tmp_path / "missing.json"))

    config = arr_sync.load_arr_config()

    assert config["poll_interval_seconds"] == 3600
    assert config["sync_timeout_seconds"] == 7200
    assert config["dry_run"] is True
    assert config["radarr"]["url"] == ""
    assert config["radarr"]["quality_profile"] == "HD-1080p"
    assert config["sonarr"]["monitor"] == "all"


def test_load_arr_config_merges_file_over_defaults(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "dry_run": False,
        "radarr": {"url": "https://radarr.example.com", "api_key": "abc"},
    }))
    monkeypatch.setattr(arr_sync, "CONFIG_FILE", str(config_file))

    config = arr_sync.load_arr_config()

    assert config["dry_run"] is False
    assert config["radarr"]["url"] == "https://radarr.example.com"
    assert config["radarr"]["api_key"] == "abc"
    # Untouched radarr keys keep their defaults
    assert config["radarr"]["quality_profile"] == "HD-1080p"
    # sonarr section untouched by the partial file
    assert config["sonarr"]["url"] == ""


def test_load_arr_config_invalid_json_falls_back_to_defaults(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text("not valid json{{{")
    monkeypatch.setattr(arr_sync, "CONFIG_FILE", str(config_file))

    config = arr_sync.load_arr_config()

    assert config["poll_interval_seconds"] == 3600


def test_load_arr_config_env_overrides_file(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "poll_interval_seconds": 100,
        "radarr": {"url": "https://from-file.example.com", "api_key": "file-key"},
    }))
    monkeypatch.setattr(arr_sync, "CONFIG_FILE", str(config_file))
    monkeypatch.setenv("ARR_SYNC_POLL_INTERVAL_SECONDS", "42")
    monkeypatch.setenv("RADARR_URL", "https://from-env.example.com")
    monkeypatch.setenv("RADARR_API_KEY", "env-key")

    config = arr_sync.load_arr_config()

    assert config["poll_interval_seconds"] == 42
    assert config["radarr"]["url"] == "https://from-env.example.com"
    assert config["radarr"]["api_key"] == "env-key"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./test.sh -k test_load_arr_config`
Expected: FAIL/ERROR — `ModuleNotFoundError: No module named 'arr_sync'`

- [ ] **Step 3: Create `arr_sync.py` with config loading**

```python
import json
import logging
import os
from collections import deque

from dotenv import load_dotenv

load_dotenv()

CACHE_DIR = "data"
CONFIG_FILE = os.path.join(CACHE_DIR, "config.json")

DEFAULT_CONFIG = {
    "poll_interval_seconds": 3600,
    "sync_timeout_seconds": 7200,
    "dry_run": True,
    "radarr": {
        "url": "",
        "api_key": "",
        "quality_profile": "HD-1080p",
        "root_folder_path": None,
        "minimum_availability": "announced",
        "search_on_add": True,
    },
    "sonarr": {
        "url": "",
        "api_key": "",
        "quality_profile": "HD-1080p",
        "root_folder_path": None,
        "monitor": "all",
        "series_type": "standard",
        "season_folder": True,
        "search_on_add": True,
    },
}

logger = logging.getLogger("imdb-server.arr_sync")

_log = deque(maxlen=500)


class _DequeLogHandler(logging.Handler):
    def emit(self, record):
        _log.append(self.format(record))


_handler = _DequeLogHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


def load_arr_config() -> dict:
    config = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                file_config = json.load(f)
            config["poll_interval_seconds"] = file_config.get(
                "poll_interval_seconds", config["poll_interval_seconds"])
            config["sync_timeout_seconds"] = file_config.get(
                "sync_timeout_seconds", config["sync_timeout_seconds"])
            config["dry_run"] = file_config.get("dry_run", config["dry_run"])
            for service in ("radarr", "sonarr"):
                config[service].update(file_config.get(service, {}))
        except Exception:
            logger.warning("Failed to load %s, using defaults", CONFIG_FILE, exc_info=True)

    if os.environ.get("ARR_SYNC_POLL_INTERVAL_SECONDS"):
        config["poll_interval_seconds"] = int(os.environ["ARR_SYNC_POLL_INTERVAL_SECONDS"])
    if os.environ.get("ARR_SYNC_TIMEOUT_SECONDS"):
        config["sync_timeout_seconds"] = int(os.environ["ARR_SYNC_TIMEOUT_SECONDS"])
    if os.environ.get("SONARR_URL"):
        config["sonarr"]["url"] = os.environ["SONARR_URL"]
    if os.environ.get("SONARR_API_KEY"):
        config["sonarr"]["api_key"] = os.environ["SONARR_API_KEY"]
    if os.environ.get("RADARR_URL"):
        config["radarr"]["url"] = os.environ["RADARR_URL"]
    if os.environ.get("RADARR_API_KEY"):
        config["radarr"]["api_key"] = os.environ["RADARR_API_KEY"]

    return config
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./test.sh -k test_load_arr_config`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add arr_sync.py tests/test_arr_sync.py
git commit -m "feat: add arr_sync config loading (config.json + env overrides)"
```

---

### Task 2: RadarrClient

**Files:**
- Modify: `arr_sync.py` (append)
- Test: Modify `tests/test_arr_sync.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
from unittest.mock import MagicMock, patch


def _fake_response(json_data, status=200, raise_exc=None):
    resp = MagicMock()
    resp.json.return_value = json_data
    if raise_exc:
        resp.raise_for_status.side_effect = raise_exc
    else:
        resp.raise_for_status.return_value = None
    resp.status_code = status
    return resp


def test_radarr_get_library_imdb_ids():
    client = arr_sync.RadarrClient("https://radarr.example.com", "key")
    movies = [{"imdbId": "tt1"}, {"imdbId": "tt2"}, {"title": "no imdb id"}]
    with patch("arr_sync.requests.get", return_value=_fake_response(movies)) as mock_get:
        result = client.get_library_imdb_ids()
    assert result == {"tt1", "tt2"}
    mock_get.assert_called_once_with(
        "https://radarr.example.com/api/v3/movie", headers={"X-Api-Key": "key"}, timeout=30)


def test_radarr_get_excluded_tmdb_ids():
    client = arr_sync.RadarrClient("https://radarr.example.com", "key")
    exclusions = [{"tmdbId": 111}, {"tmdbId": 222}]
    with patch("arr_sync.requests.get", return_value=_fake_response(exclusions)):
        result = client.get_excluded_tmdb_ids()
    assert result == {111, 222}


def test_radarr_resolve_quality_profile_id_found():
    client = arr_sync.RadarrClient("https://radarr.example.com", "key")
    profiles = [{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}]
    with patch("arr_sync.requests.get", return_value=_fake_response(profiles)):
        assert client.resolve_quality_profile_id("HD-1080p") == 4


def test_radarr_resolve_quality_profile_id_not_found():
    client = arr_sync.RadarrClient("https://radarr.example.com", "key")
    profiles = [{"id": 1, "name": "Any"}]
    with patch("arr_sync.requests.get", return_value=_fake_response(profiles)):
        assert client.resolve_quality_profile_id("Nonexistent") is None


def test_radarr_resolve_root_folder_path_configured_wins():
    client = arr_sync.RadarrClient("https://radarr.example.com", "key")
    assert client.resolve_root_folder_path("/custom/path") == "/custom/path"


def test_radarr_resolve_root_folder_path_auto_selects_sole_folder():
    client = arr_sync.RadarrClient("https://radarr.example.com", "key")
    with patch("arr_sync.requests.get", return_value=_fake_response([{"id": 1, "path": "/media/Movies"}])):
        assert client.resolve_root_folder_path(None) == "/media/Movies"


def test_radarr_resolve_root_folder_path_ambiguous_returns_none():
    client = arr_sync.RadarrClient("https://radarr.example.com", "key")
    folders = [{"id": 1, "path": "/a"}, {"id": 2, "path": "/b"}]
    with patch("arr_sync.requests.get", return_value=_fake_response(folders)):
        assert client.resolve_root_folder_path(None) is None


def test_radarr_lookup_by_imdb_found():
    client = arr_sync.RadarrClient("https://radarr.example.com", "key")
    movie = {"title": "The Matrix", "year": 1999, "imdbId": "tt0133093", "tmdbId": 603}
    with patch("arr_sync.requests.get", return_value=_fake_response(movie)):
        assert client.lookup_by_imdb("tt0133093") == movie


def test_radarr_lookup_by_imdb_not_found_returns_none():
    import requests
    client = arr_sync.RadarrClient("https://radarr.example.com", "key")
    resp = _fake_response({"message": "not found"}, status=500,
                           raise_exc=requests.HTTPError("500 Server Error"))
    with patch("arr_sync.requests.get", return_value=resp):
        assert client.lookup_by_imdb("tt0000000") is None


def test_radarr_add_movie_builds_payload_from_lookup_result():
    client = arr_sync.RadarrClient("https://radarr.example.com", "key")
    movie = {"title": "The Matrix", "year": 1999, "imdbId": "tt0133093", "tmdbId": 603}
    with patch("arr_sync.requests.post", return_value=_fake_response({"id": 99})) as mock_post:
        result = client.add_movie(
            movie, quality_profile_id=4, root_folder_path="/media/Movies",
            minimum_availability="announced", search_on_add=True)
    assert result == {"id": 99}
    sent_payload = mock_post.call_args.kwargs["json"]
    assert sent_payload["title"] == "The Matrix"
    assert sent_payload["tmdbId"] == 603
    assert sent_payload["qualityProfileId"] == 4
    assert sent_payload["rootFolderPath"] == "/media/Movies"
    assert sent_payload["minimumAvailability"] == "announced"
    assert sent_payload["monitored"] is True
    assert sent_payload["addOptions"] == {"searchForMovie": True}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./test.sh -k test_radarr`
Expected: FAIL — `AttributeError: module 'arr_sync' has no attribute 'RadarrClient'`

- [ ] **Step 3: Append `RadarrClient` to `arr_sync.py`**

Add `import requests` and `from typing import Optional` to the top imports, then append:

```python
class RadarrClient:
    def __init__(self, url: str, api_key: str):
        self.base_url = url.rstrip("/")
        self.headers = {"X-Api-Key": api_key}

    def _get(self, path: str):
        resp = requests.get(f"{self.base_url}/api/v3{path}", headers=self.headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict):
        resp = requests.post(f"{self.base_url}/api/v3{path}", headers=self.headers, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_library_imdb_ids(self) -> set:
        return {m["imdbId"] for m in self._get("/movie") if m.get("imdbId")}

    def get_excluded_tmdb_ids(self) -> set:
        return {e["tmdbId"] for e in self._get("/exclusions") if e.get("tmdbId")}

    def resolve_quality_profile_id(self, name: str) -> Optional[int]:
        for profile in self._get("/qualityprofile"):
            if profile["name"] == name:
                return profile["id"]
        return None

    def resolve_root_folder_path(self, configured: Optional[str]) -> Optional[str]:
        if configured:
            return configured
        folders = self._get("/rootfolder")
        if len(folders) == 1:
            return folders[0]["path"]
        return None

    def lookup_by_imdb(self, imdb_id: str) -> Optional[dict]:
        try:
            return self._get(f"/movie/lookup/imdb?imdbId={imdb_id}")
        except requests.HTTPError:
            return None

    def add_movie(self, movie: dict, quality_profile_id: int, root_folder_path: str,
                   minimum_availability: str, search_on_add: bool) -> dict:
        payload = dict(movie)
        payload["qualityProfileId"] = quality_profile_id
        payload["rootFolderPath"] = root_folder_path
        payload["monitored"] = True
        payload["minimumAvailability"] = minimum_availability
        payload["addOptions"] = {"searchForMovie": search_on_add}
        return self._post("/movie", payload)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./test.sh -k test_radarr`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add arr_sync.py tests/test_arr_sync.py
git commit -m "feat: add RadarrClient for library/exclusion/lookup/add calls"
```

---

### Task 3: SonarrClient

**Files:**
- Modify: `arr_sync.py` (append)
- Test: Modify `tests/test_arr_sync.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
def test_sonarr_get_library_imdb_ids():
    client = arr_sync.SonarrClient("https://sonarr.example.com", "key")
    series = [{"imdbId": "tt1"}, {"imdbId": "tt2"}, {"title": "no imdb id"}]
    with patch("arr_sync.requests.get", return_value=_fake_response(series)):
        assert client.get_library_imdb_ids() == {"tt1", "tt2"}


def test_sonarr_get_excluded_tvdb_ids():
    client = arr_sync.SonarrClient("https://sonarr.example.com", "key")
    exclusions = [{"tvdbId": 111}, {"tvdbId": 222}]
    with patch("arr_sync.requests.get", return_value=_fake_response(exclusions)):
        assert client.get_excluded_tvdb_ids() == {111, 222}


def test_sonarr_resolve_quality_profile_id_found():
    client = arr_sync.SonarrClient("https://sonarr.example.com", "key")
    profiles = [{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}]
    with patch("arr_sync.requests.get", return_value=_fake_response(profiles)):
        assert client.resolve_quality_profile_id("HD-1080p") == 4


def test_sonarr_resolve_root_folder_path_auto_selects_sole_folder():
    client = arr_sync.SonarrClient("https://sonarr.example.com", "key")
    with patch("arr_sync.requests.get", return_value=_fake_response([{"id": 2, "path": "/media/TVs"}])):
        assert client.resolve_root_folder_path(None) == "/media/TVs"


def test_sonarr_lookup_by_imdb_found_returns_first_result():
    client = arr_sync.SonarrClient("https://sonarr.example.com", "key")
    series = [{"title": "Breaking Bad", "year": 2008, "tvdbId": 81189, "imdbId": "tt0903747"}]
    with patch("arr_sync.requests.get", return_value=_fake_response(series)):
        assert client.lookup_by_imdb("tt0903747") == series[0]


def test_sonarr_lookup_by_imdb_not_found_returns_none():
    client = arr_sync.SonarrClient("https://sonarr.example.com", "key")
    with patch("arr_sync.requests.get", return_value=_fake_response([])):
        assert client.lookup_by_imdb("tt0000000") is None


def test_sonarr_add_series_builds_payload_from_lookup_result():
    client = arr_sync.SonarrClient("https://sonarr.example.com", "key")
    series = {"title": "Breaking Bad", "year": 2008, "tvdbId": 81189, "imdbId": "tt0903747"}
    with patch("arr_sync.requests.post", return_value=_fake_response({"id": 5})) as mock_post:
        result = client.add_series(
            series, quality_profile_id=4, root_folder_path="/media/TVs",
            series_type="standard", season_folder=True, monitor="all", search_on_add=True)
    assert result == {"id": 5}
    sent_payload = mock_post.call_args.kwargs["json"]
    assert sent_payload["title"] == "Breaking Bad"
    assert sent_payload["tvdbId"] == 81189
    assert sent_payload["qualityProfileId"] == 4
    assert sent_payload["rootFolderPath"] == "/media/TVs"
    assert sent_payload["seriesType"] == "standard"
    assert sent_payload["seasonFolder"] is True
    assert sent_payload["monitored"] is True
    assert sent_payload["addOptions"] == {"monitor": "all", "searchForMissingEpisodes": True}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./test.sh -k test_sonarr`
Expected: FAIL — `AttributeError: module 'arr_sync' has no attribute 'SonarrClient'`

- [ ] **Step 3: Append `SonarrClient` to `arr_sync.py`**

```python
class SonarrClient:
    def __init__(self, url: str, api_key: str):
        self.base_url = url.rstrip("/")
        self.headers = {"X-Api-Key": api_key}

    def _get(self, path: str):
        resp = requests.get(f"{self.base_url}/api/v3{path}", headers=self.headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict):
        resp = requests.post(f"{self.base_url}/api/v3{path}", headers=self.headers, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_library_imdb_ids(self) -> set:
        return {s["imdbId"] for s in self._get("/series") if s.get("imdbId")}

    def get_excluded_tvdb_ids(self) -> set:
        return {e["tvdbId"] for e in self._get("/importlistexclusion") if e.get("tvdbId")}

    def resolve_quality_profile_id(self, name: str) -> Optional[int]:
        for profile in self._get("/qualityprofile"):
            if profile["name"] == name:
                return profile["id"]
        return None

    def resolve_root_folder_path(self, configured: Optional[str]) -> Optional[str]:
        if configured:
            return configured
        folders = self._get("/rootfolder")
        if len(folders) == 1:
            return folders[0]["path"]
        return None

    def lookup_by_imdb(self, imdb_id: str) -> Optional[dict]:
        results = self._get(f"/series/lookup?term=imdb:{imdb_id}")
        return results[0] if results else None

    def add_series(self, series: dict, quality_profile_id: int, root_folder_path: str,
                    series_type: str, season_folder: bool, monitor: str, search_on_add: bool) -> dict:
        payload = dict(series)
        payload["qualityProfileId"] = quality_profile_id
        payload["rootFolderPath"] = root_folder_path
        payload["seriesType"] = series_type
        payload["seasonFolder"] = season_folder
        payload["monitored"] = True
        payload["addOptions"] = {"monitor": monitor, "searchForMissingEpisodes": search_on_add}
        return self._post("/series", payload)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./test.sh -k test_sonarr`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add arr_sync.py tests/test_arr_sync.py
git commit -m "feat: add SonarrClient for library/exclusion/lookup/add calls"
```

---

### Task 4: Sync orchestration

**Files:**
- Modify: `arr_sync.py` (append)
- Test: Modify `tests/test_arr_sync.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
import threading


def _base_config(**overrides):
    config = json.loads(json.dumps(arr_sync.DEFAULT_CONFIG))
    config["dry_run"] = False
    config["radarr"]["url"] = "https://radarr.example.com"
    config["radarr"]["api_key"] = "key"
    config["sonarr"]["url"] = "https://sonarr.example.com"
    config["sonarr"]["api_key"] = "key"
    for k, v in overrides.items():
        config[k] = v
    return config


def test_sync_movies_skips_items_already_in_library(monkeypatch):
    client = MagicMock()
    client.get_library_imdb_ids.return_value = {"tt1"}
    client.get_excluded_tmdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/Movies"
    monkeypatch.setattr(arr_sync, "RadarrClient", lambda url, key: client)

    counts = arr_sync._sync_movies(_base_config(), [{"imdb": "tt1", "title": "Already Have It"}],
                                     threading.Event())

    assert counts["skipped_existing"] == 1
    assert counts["added"] == 0
    client.lookup_by_imdb.assert_not_called()


def test_sync_movies_skips_excluded_items(monkeypatch):
    client = MagicMock()
    client.get_library_imdb_ids.return_value = set()
    client.get_excluded_tmdb_ids.return_value = {603}
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/Movies"
    client.lookup_by_imdb.return_value = {"title": "The Matrix", "tmdbId": 603}
    monkeypatch.setattr(arr_sync, "RadarrClient", lambda url, key: client)

    counts = arr_sync._sync_movies(_base_config(), [{"imdb": "tt0133093", "title": "The Matrix"}],
                                     threading.Event())

    assert counts["skipped_excluded"] == 1
    assert counts["added"] == 0
    client.add_movie.assert_not_called()


def test_sync_movies_dry_run_does_not_call_add(monkeypatch):
    client = MagicMock()
    client.get_library_imdb_ids.return_value = set()
    client.get_excluded_tmdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/Movies"
    client.lookup_by_imdb.return_value = {"title": "The Matrix", "tmdbId": 603}
    monkeypatch.setattr(arr_sync, "RadarrClient", lambda url, key: client)

    counts = arr_sync._sync_movies(_base_config(dry_run=True),
                                     [{"imdb": "tt0133093", "title": "The Matrix"}], threading.Event())

    assert counts["would_add"] == 1
    client.add_movie.assert_not_called()


def test_sync_movies_adds_new_item():
    client = MagicMock()
    client.get_library_imdb_ids.return_value = set()
    client.get_excluded_tmdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/Movies"
    client.lookup_by_imdb.return_value = {"title": "The Matrix", "tmdbId": 603}
    with patch.object(arr_sync, "RadarrClient", lambda url, key: client):
        counts = arr_sync._sync_movies(_base_config(), [{"imdb": "tt0133093", "title": "The Matrix"}],
                                         threading.Event())

    assert counts["added"] == 1
    client.add_movie.assert_called_once()


def test_sync_movies_counts_failed_lookup():
    client = MagicMock()
    client.get_library_imdb_ids.return_value = set()
    client.get_excluded_tmdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/Movies"
    client.lookup_by_imdb.return_value = None
    with patch.object(arr_sync, "RadarrClient", lambda url, key: client):
        counts = arr_sync._sync_movies(_base_config(), [{"imdb": "tt9999999", "title": "Unknown"}],
                                         threading.Event())

    assert counts["failed"] == 1


def test_sync_movies_stops_early_when_stop_event_set():
    client = MagicMock()
    client.get_library_imdb_ids.return_value = set()
    client.get_excluded_tmdb_ids.return_value = set()
    client.resolve_quality_profile_id.return_value = 4
    client.resolve_root_folder_path.return_value = "/media/Movies"
    stop_event = threading.Event()
    stop_event.set()
    with patch.object(arr_sync, "RadarrClient", lambda url, key: client):
        counts = arr_sync._sync_movies(_base_config(), [{"imdb": "tt1", "title": "X"}], stop_event)

    assert counts == {"added": 0, "would_add": 0, "skipped_existing": 0, "skipped_excluded": 0, "failed": 0}
    client.lookup_by_imdb.assert_not_called()


def test_sync_movies_skips_when_radarr_not_configured():
    config = _base_config()
    config["radarr"]["url"] = ""
    counts = arr_sync._sync_movies(config, [{"imdb": "tt1", "title": "X"}], threading.Event())
    assert counts["added"] == 0


def test_run_sync_splits_movie_and_tv_items(monkeypatch):
    cache = {
        "ur1": {"items": [
            {"imdb": "tt1", "title": "A Movie", "type": "movie"},
            {"imdb": "tt2", "title": "A Show", "type": "tvSeries"},
        ]}
    }
    monkeypatch.setattr("imdb_server.load_cache", lambda: cache)
    monkeypatch.setattr(arr_sync, "load_arr_config", lambda: _base_config(dry_run=True))
    movie_calls = []
    tv_calls = []
    monkeypatch.setattr(arr_sync, "_sync_movies",
                         lambda config, items, stop_event: movie_calls.append(items) or {"added": 0})
    monkeypatch.setattr(arr_sync, "_sync_tv",
                         lambda config, items, stop_event: tv_calls.append(items) or {"added": 0})

    result = arr_sync._run_sync("test", threading.Event())

    assert len(movie_calls[0]) == 1 and movie_calls[0][0]["title"] == "A Movie"
    assert len(tv_calls[0]) == 1 and tv_calls[0][0]["title"] == "A Show"
    assert result == {"radarr": {"added": 0}, "sonarr": {"added": 0}, "dry_run": True}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./test.sh -k "test_sync_movies or test_run_sync"`
Expected: FAIL — `AttributeError: module 'arr_sync' has no attribute '_sync_movies'`

- [ ] **Step 3: Append sync orchestration functions to `arr_sync.py`**

```python
def _sync_movies(config: dict, items: list, stop_event) -> dict:
    counts = {"added": 0, "would_add": 0, "skipped_existing": 0, "skipped_excluded": 0, "failed": 0}
    radarr_config = config["radarr"]
    if not radarr_config.get("url") or not radarr_config.get("api_key"):
        logger.info("Radarr not configured, skipping movies")
        return counts

    client = RadarrClient(radarr_config["url"], radarr_config["api_key"])
    try:
        existing_imdb_ids = client.get_library_imdb_ids()
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

    for item in items:
        if stop_event.is_set():
            logger.info("Stop requested, halting Radarr sync early")
            break

        imdb_id = item.get("imdb") or item.get("imdbId") or item.get("imdb_id")
        if not imdb_id or imdb_id in existing_imdb_ids:
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
            client.add_movie(
                movie,
                quality_profile_id=quality_profile_id,
                root_folder_path=root_folder_path,
                minimum_availability=radarr_config["minimum_availability"],
                search_on_add=radarr_config["search_on_add"],
            )
            logger.info("Added movie: %s", movie.get("title"))
            counts["added"] += 1
        except Exception:
            logger.error("Failed to add movie %s (%s)", item.get("title"), imdb_id, exc_info=True)
            counts["failed"] += 1

    return counts


def _sync_tv(config: dict, items: list, stop_event) -> dict:
    counts = {"added": 0, "would_add": 0, "skipped_existing": 0, "skipped_excluded": 0, "failed": 0}
    sonarr_config = config["sonarr"]
    if not sonarr_config.get("url") or not sonarr_config.get("api_key"):
        logger.info("Sonarr not configured, skipping tv shows")
        return counts

    client = SonarrClient(sonarr_config["url"], sonarr_config["api_key"])
    try:
        existing_imdb_ids = client.get_library_imdb_ids()
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

    for item in items:
        if stop_event.is_set():
            logger.info("Stop requested, halting Sonarr sync early")
            break

        imdb_id = item.get("imdb") or item.get("imdbId") or item.get("imdb_id")
        if not imdb_id or imdb_id in existing_imdb_ids:
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
            client.add_series(
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
        except Exception:
            logger.error("Failed to add series %s (%s)", item.get("title"), imdb_id, exc_info=True)
            counts["failed"] += 1

    return counts


def _run_sync(source: str, stop_event) -> dict:
    from imdb_server import load_cache
    from mcp_server import MOVIE_TYPES, TV_TYPES

    config = load_arr_config()
    cache = load_cache()

    movie_items = []
    tv_items = []
    for data in cache.values():
        for item in data.get("items", []):
            item_type = item.get("type")
            if item_type in TV_TYPES:
                tv_items.append(item)
            elif item_type in MOVIE_TYPES or item_type is None:
                movie_items.append(item)

    logger.info("Starting arr sync (source=%s, dry_run=%s): %d movie items, %d tv items",
                source, config["dry_run"], len(movie_items), len(tv_items))

    radarr_counts = _sync_movies(config, movie_items, stop_event)
    sonarr_counts = {"added": 0, "would_add": 0, "skipped_existing": 0, "skipped_excluded": 0, "failed": 0}
    if not stop_event.is_set():
        sonarr_counts = _sync_tv(config, tv_items, stop_event)

    logger.info("Finished arr sync: radarr=%s sonarr=%s", radarr_counts, sonarr_counts)
    return {"radarr": radarr_counts, "sonarr": sonarr_counts, "dry_run": config["dry_run"]}


def run_sync_once(source: str = "cli") -> dict:
    return _run_sync(source, threading.Event())
```

Add `import threading` to the top imports (needed by `run_sync_once`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `./test.sh -k "test_sync_movies or test_run_sync"`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add arr_sync.py tests/test_arr_sync.py
git commit -m "feat: add arr sync orchestration (movie/tv split, per-item lookup+add)"
```

---

### Task 5: Guard, timeout, and stop state machine

**Files:**
- Modify: `arr_sync.py` (append)
- Test: Modify `tests/test_arr_sync.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
import time


@pytest.fixture(autouse=True)
def reset_sync_state():
    arr_sync._current_thread = None
    arr_sync._stop_event = threading.Event()
    arr_sync._status = {
        "state": "idle", "started_at": None, "finished_at": None,
        "source": None, "result": None, "error": None,
    }
    arr_sync._log.clear()
    yield


def test_try_start_sync_starts_when_idle(monkeypatch):
    monkeypatch.setattr(arr_sync, "_run_sync", lambda source, stop_event: {"radarr": {}, "sonarr": {}})

    started = arr_sync.try_start_sync("manual")

    assert started is True
    arr_sync._current_thread.join(timeout=2)
    assert arr_sync.get_status()["state"] == "success"


def test_try_start_sync_rejects_second_trigger_while_running(monkeypatch):
    release = threading.Event()

    def slow_sync(source, stop_event):
        release.wait(timeout=2)
        return {"radarr": {}, "sonarr": {}}

    monkeypatch.setattr(arr_sync, "_run_sync", slow_sync)

    assert arr_sync.try_start_sync("periodic") is True
    time.sleep(0.05)  # let the thread actually start and set state to "running"
    assert arr_sync.try_start_sync("manual") is False
    assert arr_sync.get_status()["state"] == "running"

    release.set()
    arr_sync._current_thread.join(timeout=2)


def test_try_start_sync_signals_stop_after_timeout(monkeypatch):
    stop_seen = threading.Event()

    def slow_sync(source, stop_event):
        stop_event.wait(timeout=2)
        stop_seen.set()
        return {"radarr": {}, "sonarr": {}}

    monkeypatch.setattr(arr_sync, "_run_sync", slow_sync)
    monkeypatch.setattr(arr_sync, "load_arr_config", lambda: {"sync_timeout_seconds": 0.05})

    assert arr_sync.try_start_sync("periodic") is True
    time.sleep(0.2)  # exceed the 0.05s timeout

    assert arr_sync.try_start_sync("periodic") is False
    assert stop_seen.wait(timeout=2)
    arr_sync._current_thread.join(timeout=2)
    assert arr_sync.get_status()["state"] == "stopped"


def test_request_stop_noop_when_idle():
    assert arr_sync.request_stop() is False


def test_request_stop_sets_event_when_running(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(arr_sync, "_run_sync", lambda source, stop_event: (release.wait(timeout=2), {"radarr": {}, "sonarr": {}})[1])

    arr_sync.try_start_sync("manual")
    time.sleep(0.05)

    assert arr_sync.request_stop() is True
    assert arr_sync.get_status()["state"] == "stopping"

    release.set()
    arr_sync._current_thread.join(timeout=2)


def test_run_sync_crash_sets_error_state(monkeypatch):
    def broken_sync(source, stop_event):
        raise RuntimeError("boom")

    monkeypatch.setattr(arr_sync, "_run_sync", broken_sync)

    arr_sync.try_start_sync("manual")
    arr_sync._current_thread.join(timeout=2)

    status = arr_sync.get_status()
    assert status["state"] == "error"
    assert "boom" in status["error"]


def test_get_log_returns_recent_entries():
    arr_sync.logger.info("hello from test")
    assert any("hello from test" in line for line in arr_sync.get_log())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./test.sh -k "test_try_start_sync or test_request_stop or test_run_sync_crash or test_get_log"`
Expected: FAIL — `AttributeError: module 'arr_sync' has no attribute 'try_start_sync'`

- [ ] **Step 3: Append the state machine to `arr_sync.py`**

```python
_lock = threading.Lock()
_current_thread = None
_stop_event = threading.Event()
_status = {
    "state": "idle", "started_at": None, "finished_at": None,
    "source": None, "result": None, "error": None,
}


def _thread_target(source: str, stop_event: threading.Event):
    try:
        result = _run_sync(source, stop_event)
        with _lock:
            _status["state"] = "stopped" if stop_event.is_set() else "success"
            _status["result"] = result
            _status["finished_at"] = time.time()
    except Exception as e:
        logger.error("Sync thread crashed", exc_info=True)
        with _lock:
            _status["state"] = "error"
            _status["error"] = str(e)
            _status["finished_at"] = time.time()


def try_start_sync(source: str) -> bool:
    global _current_thread, _stop_event
    with _lock:
        if _current_thread is not None and _current_thread.is_alive():
            elapsed = time.time() - (_status["started_at"] or time.time())
            config = load_arr_config()
            if elapsed > config["sync_timeout_seconds"] and not _stop_event.is_set():
                logger.warning("Sync running %.0fs, exceeds timeout %ds; requesting stop",
                                elapsed, config["sync_timeout_seconds"])
                _stop_event.set()
                _status["state"] = "stopping (timed out)"
            else:
                logger.info("Sync already running (source=%s); skipping trigger from %s",
                             _status.get("source"), source)
            return False

        _stop_event = threading.Event()
        _status["state"] = "running"
        _status["started_at"] = time.time()
        _status["finished_at"] = None
        _status["source"] = source
        _status["result"] = None
        _status["error"] = None
        _current_thread = threading.Thread(target=_thread_target, args=(source, _stop_event), daemon=True)
        _current_thread.start()
        return True


def request_stop() -> bool:
    with _lock:
        if _current_thread is not None and _current_thread.is_alive():
            _stop_event.set()
            _status["state"] = "stopping"
            return True
        return False


def get_status() -> dict:
    with _lock:
        return dict(_status)


def get_log() -> list:
    return list(_log)
```

Add `import time` to the top imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./test.sh -k "test_try_start_sync or test_request_stop or test_run_sync_crash or test_get_log"`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add arr_sync.py tests/test_arr_sync.py
git commit -m "feat: add duplicate-trigger guard, reactive timeout, and stop support"
```

---

### Task 6: Async scheduler loop

**Files:**
- Modify: `arr_sync.py` (append)
- Test: Modify `tests/test_arr_sync.py` (append)

- [ ] **Step 1: Write the failing test**

```python
import asyncio


def test_scheduler_loop_ticks_periodically(monkeypatch):
    calls = []
    monkeypatch.setattr(arr_sync, "load_arr_config", lambda: {"poll_interval_seconds": 0.05})
    monkeypatch.setattr(arr_sync, "try_start_sync", lambda source: calls.append(source))

    async def run_briefly():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(arr_sync.scheduler_loop(), timeout=0.2)

    asyncio.run(run_briefly())

    assert len(calls) >= 2
    assert all(c == "periodic" for c in calls)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./test.sh -k test_scheduler_loop`
Expected: FAIL — `AttributeError: module 'arr_sync' has no attribute 'scheduler_loop'`

- [ ] **Step 3: Append the scheduler loop to `arr_sync.py`**

```python
async def scheduler_loop():
    while True:
        config = load_arr_config()
        try:
            try_start_sync(source="periodic")
        except Exception:
            logger.error("Error in scheduler tick", exc_info=True)
        await asyncio.sleep(config["poll_interval_seconds"])
```

Add `import asyncio` to the top imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `./test.sh -k test_scheduler_loop`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add arr_sync.py tests/test_arr_sync.py
git commit -m "feat: add asyncio scheduler loop for periodic sync"
```

---

### Task 7: Web page and routes

**Files:**
- Modify: `arr_sync.py` (append)
- Test: Modify `tests/test_arr_sync.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
from fastapi import FastAPI
from fastapi.testclient import TestClient

_test_app = FastAPI()
_test_app.include_router(arr_sync.router)
sync_client = TestClient(_test_app)


def test_sync_status_endpoint_shape():
    response = sync_client.get("/sync/status")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "log" in data
    assert data["status"]["state"] == "idle"


def test_sync_trigger_endpoint_starts_a_run(monkeypatch):
    monkeypatch.setattr(arr_sync, "_run_sync", lambda source, stop_event: {"radarr": {}, "sonarr": {}})

    response = sync_client.post("/sync/trigger")

    assert response.status_code == 200
    assert response.json()["started"] is True
    arr_sync._current_thread.join(timeout=2)


def test_sync_trigger_endpoint_rejects_while_running(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(arr_sync, "_run_sync",
                         lambda source, stop_event: (release.wait(timeout=2), {"radarr": {}, "sonarr": {}})[1])

    sync_client.post("/sync/trigger")
    time.sleep(0.05)
    response = sync_client.post("/sync/trigger")

    assert response.json()["started"] is False

    release.set()
    arr_sync._current_thread.join(timeout=2)


def test_sync_stop_endpoint_noop_when_idle():
    response = sync_client.post("/sync/stop")
    assert response.json()["stop_requested"] is False


def test_sync_page_renders_html_with_buttons():
    response = sync_client.get("/sync")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'action="/sync/trigger"' in response.text
    assert 'action="/sync/stop"' in response.text


def test_sync_page_escapes_log_content(monkeypatch):
    monkeypatch.setattr(arr_sync, "_log", type(arr_sync._log)(["<script>alert(1)</script>"], maxlen=500))

    response = sync_client.get("/sync")

    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./test.sh -k "test_sync_status_endpoint or test_sync_trigger_endpoint or test_sync_stop_endpoint or test_sync_page"`
Expected: FAIL — `AttributeError: module 'arr_sync' has no attribute 'router'`

- [ ] **Step 3: Append the router to `arr_sync.py`**

```python
def _render_sync_page() -> str:
    status = get_status()
    log_lines = get_log()[-200:]
    log_text = html.escape("\n".join(log_lines)) if log_lines else "(no log yet)"
    result = status.get("result") or {}
    radarr_counts = result.get("radarr", {})
    sonarr_counts = result.get("sonarr", {})

    def fmt_time(ts):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"

    elapsed = ""
    if status["state"].startswith("running") or status["state"].startswith("stopping"):
        if status.get("started_at"):
            elapsed = f" (running for {int(time.time() - status['started_at'])}s)"

    error_html = ""
    if status.get("error"):
        error_html = f"<p><strong>Error:</strong> {html.escape(status['error'])}</p>"

    def counts_row(service, counts):
        return (
            f"<tr><td>{service}</td>"
            f"<td>{counts.get('added', '-')}</td>"
            f"<td>{counts.get('would_add', '-')}</td>"
            f"<td>{counts.get('skipped_existing', '-')}</td>"
            f"<td>{counts.get('skipped_excluded', '-')}</td>"
            f"<td>{counts.get('failed', '-')}</td></tr>"
        )

    return f"""<!DOCTYPE html>
<html>
<head>
  <title>Arr Sync Status</title>
  <meta http-equiv="refresh" content="5">
  <style>
    body {{ font-family: monospace; margin: 2em; }}
    pre {{ background: #f0f0f0; padding: 1em; max-height: 400px; overflow-y: auto; }}
    table {{ border-collapse: collapse; margin-bottom: 1em; }}
    td, th {{ border: 1px solid #ccc; padding: 0.3em 0.8em; text-align: left; }}
    button {{ padding: 0.5em 1em; margin-right: 0.5em; }}
  </style>
</head>
<body>
  <h1>Arr Sync Status</h1>
  <p><strong>State:</strong> {html.escape(status['state'])}{elapsed}</p>
  <p><strong>Source:</strong> {html.escape(status.get('source') or '-')}</p>
  <p><strong>Started:</strong> {fmt_time(status.get('started_at'))}</p>
  <p><strong>Finished:</strong> {fmt_time(status.get('finished_at'))}</p>
  {error_html}
  <table>
    <tr><th>Service</th><th>Added</th><th>Would add (dry run)</th><th>Skipped (existing)</th><th>Skipped (excluded)</th><th>Failed</th></tr>
    {counts_row("Radarr", radarr_counts)}
    {counts_row("Sonarr", sonarr_counts)}
  </table>
  <form method="post" action="/sync/trigger" style="display:inline">
    <button type="submit">Trigger Sync</button>
  </form>
  <form method="post" action="/sync/stop" style="display:inline">
    <button type="submit">Stop Sync</button>
  </form>
  <h2>Recent Log</h2>
  <pre>{log_text}</pre>
</body>
</html>
"""


router = APIRouter()


@router.get("/sync/status")
def sync_status():
    return {"status": get_status(), "log": get_log()[-200:]}


@router.post("/sync/trigger")
def sync_trigger():
    started = try_start_sync(source="manual")
    return {"started": started, "status": get_status()}


@router.post("/sync/stop")
def sync_stop():
    stopped = request_stop()
    return {"stop_requested": stopped, "status": get_status()}


@router.get("/sync", response_class=HTMLResponse)
def sync_page():
    return HTMLResponse(_render_sync_page())
```

Add these imports to the top of `arr_sync.py`: `import html`, `from fastapi import APIRouter`, `from fastapi.responses import HTMLResponse`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./test.sh -k "test_sync_status_endpoint or test_sync_trigger_endpoint or test_sync_stop_endpoint or test_sync_page"`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add arr_sync.py tests/test_arr_sync.py
git commit -m "feat: add /sync status page, JSON status, trigger, and stop endpoints"
```

---

### Task 8: Wire into `imdb_server.py`

**Files:**
- Modify: `imdb_server.py:1-16` (imports), `imdb_server.py:34-42` (lifespan/app setup), `imdb_server.py:58-60` (mounting), `imdb_server.py:286-296` (CLI args), `imdb_server.py:326-338` (CLI dispatch)

- [ ] **Step 1: Add imports and wire the router/lifespan**

In `imdb_server.py`, add `import asyncio` to the import block (imdb_server.py:12, alongside the existing `import threading`), and add after `from mcp_server import mcp` (imdb_server.py:15):

```python
from arr_sync import router as arr_sync_router, scheduler_loop, run_sync_once
```

Replace the `lifespan` function (imdb_server.py:37-40):

```python
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler_task = asyncio.create_task(scheduler_loop())
    async with mcp.session_manager.run():
        yield
    scheduler_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass
```

After `app.mount("/sse", mcp.sse_app())` (imdb_server.py:60), add:

```python
app.include_router(arr_sync_router)
```

- [ ] **Step 2: Add the `--sync-once` CLI flag**

In the `argparse` block (after the `--stats` argument, imdb_server.py:294), add:

```python
    parser.add_argument("--sync-once", action="store_true",
                         help="Run one Radarr/Sonarr sync cycle immediately and exit")
```

In the CLI dispatch section, after the `if args.search:` block and before `if args.user_id:` (imdb_server.py:326), add:

```python
    if args.sync_once:
        result = run_sync_once()
        print(json.dumps(result, indent=2))
        sys.exit(0)
```

- [ ] **Step 3: Run the full test suite to confirm nothing broke**

Run: `./test.sh`
Expected: PASS — all existing tests plus the new `test_arr_sync.py` tests (this repo has no automated test for `lifespan`/mounting itself, matching the existing gap already noted for `mcp_server.py` — end-to-end wiring is confirmed manually in Task 13)

- [ ] **Step 4: Commit**

```bash
git add imdb_server.py
git commit -m "feat: wire arr_sync scheduler and routes into the FastAPI app"
```

---

### Task 9: Config template and `.gitignore`

**Files:**
- Create: `data/config.example.json`
- Modify: `.gitignore`

- [ ] **Step 1: Create the committed example config**

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

- [ ] **Step 2: Add `config.json` to `.gitignore`**

Add this line to `.gitignore`, next to the other `data/*.json` runtime files:

```
config.json
```

- [ ] **Step 3: Verify `data/config.json` would be ignored**

Run: `git check-ignore -v data/config.json`
Expected: prints the matching `.gitignore` rule (confirms it's ignored)

- [ ] **Step 4: Commit**

```bash
git add data/config.example.json .gitignore
git commit -m "feat: add config.example.json template, gitignore data/config.json"
```

---

### Task 10: Dockerfile

**Files:**
- Modify: `Dockerfile:41`

- [ ] **Step 1: Add `arr_sync.py` to the `COPY` line**

Change:
```dockerfile
COPY imdb_server.py mcp_server.py embedding.py .
```
to:
```dockerfile
COPY imdb_server.py mcp_server.py embedding.py arr_sync.py .
```

- [ ] **Step 2: Commit**

```bash
git add Dockerfile
git commit -m "fix: copy arr_sync.py into the Docker image"
```

---

### Task 11: Local `.env` for manual verification

**Files:**
- Create: `.env` (already covered by the existing `.gitignore` entry — do not `git add` it)

- [ ] **Step 1: Create `.env` with the real test credentials**

```
SONARR_URL=https://sonarr.local.shifamily.com
SONARR_API_KEY=7affdd24f7444a09b245691d809dbf20
RADARR_URL=https://radarr.local.shifamily.com
RADARR_API_KEY=0ee5aea4565a4a909ed839b4ebb6da77
```

- [ ] **Step 2: Confirm it's ignored by git**

Run: `git status`
Expected: `.env` does not appear (it's already in `.gitignore` from before this feature)

---

### Task 12: `CLAUDE.md` documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Document the new module**

In the "Architecture" section, after the `embedding.py` bullet, add:

```markdown
- **`arr_sync.py`** — periodic Radarr/Sonarr sync. Reads `data/config.json` (not committed; `data/config.example.json` is the template) merged with env var overrides (`ARR_SYNC_POLL_INTERVAL_SECONDS`, `ARR_SYNC_TIMEOUT_SECONDS`, `SONARR_URL`, `SONARR_API_KEY`, `RADARR_URL`, `RADARR_API_KEY`), and checks every cached watchlist against the configured Radarr/Sonarr instance on a fixed interval, adding anything missing (and not import-list-excluded). Runs the sync in a `threading.Thread` (same pattern as `mcp_server.py`'s background scrape refresh) since it makes synchronous `requests` calls; a module-level guard prevents overlapping runs, and a stuck run gets a cooperative stop signal — not a hard kill — either when it exceeds `sync_timeout_seconds` on the next trigger, or immediately via the `/sync` page's Stop button. Exposes `GET /sync` (status/log page), `GET /sync/status` (JSON), `POST /sync/trigger`, `POST /sync/stop`.
```

Add a new subsection after "### Scraping":

```markdown
### Arr sync

`imdb_server.py`'s `lifespan` starts `arr_sync.scheduler_loop()` as a background `asyncio` task alongside the MCP session manager. The scheduler and the manual `/sync/trigger` endpoint share the same guard (`arr_sync.try_start_sync`) — only one sync runs at a time. `python imdb_server.py --sync-once` runs a single cycle synchronously and exits, for cron-driven or manual use outside the built-in scheduler.

Exclusion lists on Radarr/Sonarr are keyed by `tmdbId`/`tvdbId`, not `imdbId` — so checking a candidate against them requires the `/movie/lookup/imdb` or `/series/lookup?term=imdb:...` metadata call first. Checking whether an item is *already in the library* does not need this lookup, since both APIs expose `imdbId` directly on library items.
```

In the "Docker" section, update the `COPY` line reference:

```markdown
`Dockerfile` installs Google Chrome + `sbase install chromedriver` for SeleniumBase, then copies `imdb_server.py mcp_server.py embedding.py arr_sync.py version.txt*` into the image — when adding a new top-level module, add it to that `COPY` line too or it won't ship. `data/` is a volume mount (see `docker-compose.yml`) so the watchlist cache, embedding cache, and `arr_sync` config persist across container restarts.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document arr_sync module in CLAUDE.md"
```

---

### Task 13: Manual verification against the real servers

**No files changed** — this is a verification-only task. Do not flip `dry_run` to `false` or otherwise trigger a live `POST /movie` or `POST /series` against the real Radarr/Sonarr instances without separately confirming with the user which specific title to add.

- [ ] **Step 1: Create a local (gitignored) `data/config.json` for testing**

```json
{
  "poll_interval_seconds": 999999,
  "sync_timeout_seconds": 120,
  "dry_run": true,
  "radarr": {"quality_profile": "HD-1080p", "root_folder_path": null, "minimum_availability": "announced", "search_on_add": true},
  "sonarr": {"quality_profile": "HD-1080p", "root_folder_path": null, "monitor": "all", "series_type": "standard", "season_folder": true, "search_on_add": true}
}
```

`url`/`api_key` are intentionally omitted here — they come from `.env` (Task 11) via the env-override mechanism, so no secrets need to be written into this file.

- [ ] **Step 2: Run one sync cycle from the CLI**

Run: `./venv/bin/python imdb_server.py --sync-once`
Expected: JSON output with `radarr`/`sonarr` count objects and `"dry_run": true`. Check the console log output for `[dry run] would add movie: ...` / `[dry run] would add series: ...` lines and confirm the titles look right (present in the IMDb watchlist cache, absent from the real Radarr/Sonarr library).

- [ ] **Step 3: Start the server and check the web page**

Run: `./venv/bin/python imdb_server.py --port 8080` (in the background), then:
```bash
curl -s http://localhost:8080/sync/status | python3 -m json.tool
```
Expected: `state: "idle"` (no cycle has run yet at this poll interval) or `"success"` if `--sync-once` populated it in the same process — open `http://localhost:8080/sync` in a browser and confirm the page renders, the counts table matches Step 2's output, and both the Trigger and Stop buttons are visible.

- [ ] **Step 4: Exercise the Trigger and Stop endpoints**

```bash
curl -s -X POST http://localhost:8080/sync/trigger | python3 -m json.tool
curl -s -X POST http://localhost:8080/sync/trigger | python3 -m json.tool   # should report started: false while the first is running (movie+tv lookups take a few seconds against ~real library sizes)
curl -s -X POST http://localhost:8080/sync/stop | python3 -m json.tool
```
Expected: second `trigger` call returns `"started": false`; `stop` call returns `"stop_requested": true` while a run is active, and the `/sync` page shows `state: "stopped"` shortly after.

- [ ] **Step 5: Stop the manually-started server**

Stop the background `imdb_server.py` process once verification is complete.
