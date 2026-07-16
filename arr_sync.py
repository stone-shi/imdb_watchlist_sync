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
