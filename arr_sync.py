import json
import logging
import os
import threading
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
