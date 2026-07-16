import json
import os
import sys
from unittest.mock import MagicMock, patch

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
        "sync_timeout_seconds": 1800,
        "radarr": {"url": "https://radarr.example.com", "api_key": "abc"},
    }))
    monkeypatch.setattr(arr_sync, "CONFIG_FILE", str(config_file))

    config = arr_sync.load_arr_config()

    assert config["dry_run"] is False
    assert config["sync_timeout_seconds"] == 1800
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


def test_load_arr_config_malformed_service_section_falls_back_to_defaults(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "dry_run": False,
        "radarr": "oops",
    }))
    monkeypatch.setattr(arr_sync, "CONFIG_FILE", str(config_file))

    config = arr_sync.load_arr_config()

    # Malformed radarr section is ignored, not allowed to crash the load...
    assert config["radarr"]["quality_profile"] == "HD-1080p"
    assert config["radarr"]["url"] == ""
    # ...and other top-level keys from the same file are still applied.
    assert config["dry_run"] is False


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
