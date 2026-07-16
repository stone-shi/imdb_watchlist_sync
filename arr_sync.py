import json
import logging
import os
from collections import deque
from typing import Optional

import requests
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
                if service not in file_config:
                    continue
                service_value = file_config[service]
                if isinstance(service_value, dict):
                    config[service].update(service_value)
                else:
                    logger.warning(
                        "Ignoring malformed %r section in %s (expected object, got %s)",
                        service, CONFIG_FILE, type(service_value).__name__)
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
        # Radarr raises HTTP 500 (not 404) when the imdbId has no match, so
        # HTTPError here means "not found," not a genuine server error.
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
