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


def test_sync_movies_skips_when_radarr_not_configured():
    config = _base_config()
    config["radarr"]["url"] = ""
    counts = arr_sync._sync_movies(config, [{"imdb": "tt1", "title": "X"}], threading.Event())
    assert counts["added"] == 0


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


def test_try_start_sync_survives_config_load_error_while_running(monkeypatch):
    release = threading.Event()

    def slow_sync(source, stop_event):
        release.wait(timeout=2)
        return {"radarr": {}, "sonarr": {}}

    monkeypatch.setattr(arr_sync, "_run_sync", slow_sync)

    assert arr_sync.try_start_sync("periodic") is True
    time.sleep(0.05)  # let the thread actually start and set state to "running"

    def broken_config():
        raise ValueError("bad env var")

    monkeypatch.setattr(arr_sync, "load_arr_config", broken_config)

    assert arr_sync.try_start_sync("manual") is False
    # Original sync is left alone, not incorrectly stopped.
    assert arr_sync.get_status()["state"] == "running"

    release.set()
    arr_sync._current_thread.join(timeout=2)


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


def test_scheduler_loop_survives_malformed_poll_interval(monkeypatch):
    monkeypatch.setattr(
        arr_sync, "load_arr_config", lambda: {"poll_interval_seconds": "not-a-number"}
    )
    monkeypatch.setattr(arr_sync, "try_start_sync", lambda source: None)

    async def run_briefly():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(arr_sync.scheduler_loop(), timeout=0.2)

    asyncio.run(run_briefly())


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


def test_sync_page_renders_tagged_and_would_tag_columns():
    response = sync_client.get("/sync")
    assert response.status_code == 200
    assert "<th>Tagged</th>" in response.text
    assert "<th>Would tag" in response.text


def test_sync_page_escapes_log_content(monkeypatch):
    monkeypatch.setattr(arr_sync, "_log", type(arr_sync._log)(["<script>alert(1)</script>"], maxlen=500))

    response = sync_client.get("/sync")

    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_sync_page_escapes_error_content():
    arr_sync._status["error"] = "<script>alert(2)</script>"

    response = sync_client.get("/sync")

    assert "<script>alert(2)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
