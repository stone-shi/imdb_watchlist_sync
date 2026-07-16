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
