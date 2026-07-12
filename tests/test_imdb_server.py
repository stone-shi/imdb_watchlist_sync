import json as json_module
import sys
import os
import time
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
import imdb_server
from fastapi.testclient import TestClient
from imdb_server import (
    app, get_user_id, scrape_imdb_watchlist, load_cache, save_cache,
)

client = TestClient(app)


class patch_cache:
    """Context manager to replace load_cache with a fixed dict."""
    def __init__(self, cache_dict):
        self.cache_dict = cache_dict

    def __enter__(self):
        self.patcher = patch.object(imdb_server, 'load_cache', return_value=self.cache_dict)
        self.mock = self.patcher.start()
        return self

    def __exit__(self, *args):
        self.patcher.stop()


class patch_watchlist:
    """Context manager to fixate what get_watchlist returns."""
    def __init__(self, items):
        self.items = items

    def __enter__(self):
        self.patcher = patch.object(imdb_server, 'get_watchlist', return_value=self.items)
        self.mock = self.patcher.start()
        return self

    def __exit__(self, *args):
        self.patcher.stop()


def test_get_user_id_from_url():
    result = get_user_id("https://www.imdb.com/user/ur12345678/lists")
    assert result == "ur12345678"


def test_get_user_id_from_url_with_trailing_slash():
    result = get_user_id("https://www.imdb.com/user/ur99999999/")
    assert result == "ur99999999"


def test_get_user_id_returns_id_as_is():
    result = get_user_id("ur12345678")
    assert result == "ur12345678"


def test_get_user_id_from_url_with_query_params():
    result = get_user_id("https://www.imdb.com/user/ur11111111/lists?ref_=tt_rvv_rw_avog")
    assert result == "ur11111111"


def test_load_cache_empty(tmp_path):
    cache_file = tmp_path / "empty.json"

    original_cache_file = imdb_server.CACHE_FILE
    try:
        imdb_server.CACHE_FILE = str(cache_file)

        cache = load_cache()
        assert isinstance(cache, dict)
        assert len(cache) == 0
    finally:
        imdb_server.CACHE_FILE = original_cache_file


def test_load_save_cache(tmp_path):
    cache_file = tmp_path / "test_cache.json"

    original_cache_file = imdb_server.CACHE_FILE
    try:
        imdb_server.CACHE_FILE = str(cache_file)

        save_cache({"test_user": {"timestamp": 12345, "items": []}})

        with open(cache_file, "r") as f:
            loaded = json_module.load(f)

        assert loaded == {"test_user": {"timestamp": 12345, "items": []}}

        reloaded = load_cache()
        assert reloaded["test_user"]["timestamp"] == 12345
        assert reloaded["test_user"]["items"] == []
    finally:
        imdb_server.CACHE_FILE = original_cache_file


def test_load_cache_invalid_json(tmp_path):
    cache_file = tmp_path / "bad_cache.json"

    original_cache_file = imdb_server.CACHE_FILE
    try:
        with open(cache_file, "w") as f:
            f.write("not valid json{{{")

        imdb_server.CACHE_FILE = str(cache_file)

        cache = load_cache()
        assert isinstance(cache, dict)
        assert len(cache) == 0
    finally:
        imdb_server.CACHE_FILE = original_cache_file


def test_app_root_endpoint():
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "online"
    assert "version" in data
    endpoints_keys = list(data["endpoints"].keys())
    assert any("/radarr" in k for k in endpoints_keys)
    assert any("/sonarr" in k for k in endpoints_keys)


def test_app_stats_endpoint_empty():
    with patch_cache({}):
        response = client.get("/stats")
        assert response.status_code == 200
        assert response.json() == []


def test_app_stats_endpoint_with_data():
    sample_cache = {
        "ur123": {"timestamp": time.time(), "items": [{"title": "Test Movie", "imdbId": "tt123"}]},
        "ur456": {"timestamp": time.time() - 7200, "items": []},
    }

    with patch_cache(sample_cache):
        response = client.get("/stats")
        assert response.status_code == 200
        stats = response.json()
        assert len(stats) == 2
        user_ids = [s["user_id"] for s in stats]
        assert "ur123" in user_ids
        assert "ur456" in user_ids


def test_app_search_endpoint_empty():
    with patch_cache({}):
        response = client.get("/search?q=matrix")
        assert response.status_code == 200
        assert response.json() == []


def test_app_search_endpoint_with_matches():
    sample_cache = {
        "ur123": {
            "timestamp": time.time(),
            "items": [
                {"title": "The Matrix", "imdbId": "tt0133097", "year": "1999"},
                {"title": "Matrix Reloaded", "imdbId": "tt0234215", "year": "2003"},
            ],
        },
        "ur456": {
            "timestamp": time.time(),
            "items": [{"title": "John Wick", "imdbId": "tt2911896", "year": "2014"}],
        },
    }

    with patch_cache(sample_cache):
        response = client.get("/search?q=matrix")
        assert response.status_code == 200
        results = response.json()
        assert len(results) == 2
        titles = [r["title"] for r in results]
        assert "The Matrix" in titles
        assert "Matrix Reloaded" in titles


def test_app_search_case_insensitive():
    sample_cache = {
        "ur123": {
            "timestamp": time.time(),
            "items": [{"title": "The Matrix", "imdbId": "tt0133097"}],
        }
    }

    with patch_cache(sample_cache):
        response = client.get("/search?q=MaTrIx")
        assert response.status_code == 200
        results = response.json()
        assert len(results) == 1
        assert results[0]["title"] == "The Matrix"


def test_app_search_returns_source_user_id():
    sample_cache = {
        "ur789": {
            "timestamp": time.time(),
            "items": [{"title": "Inception", "imdbId": "tt1375666"}],
        }
    }

    with patch_cache(sample_cache):
        response = client.get("/search?q=inception")
        assert response.status_code == 200
        results = response.json()
        assert len(results) == 1
        assert results[0]["source_user_id"] == "ur789"


def test_app_radarr_endpoint_filters_movies():
    sample_items = [
        {"title": "Movie 1", "type": "movie", "imdbId": "tt111"},
        {"title": "TV Show 1", "type": "tvSeries", "imdbId": "tt222"},
        {"title": "Video", "type": "video", "imdbId": "tt333"},
        {"title": "Unknown Type", "type": None, "imdbId": "tt444"},
        {"title": "TV Mini", "type": "tvMiniSeries", "imdbId": "tt555"},
    ]

    with patch_watchlist(sample_items):
        response = client.get("/radarr?user_id=ur123")
        assert response.status_code == 200
        results = response.json()
        titles = [r["title"] for r in results]
        assert "Movie 1" in titles
        assert "Video" in titles
        assert "Unknown Type" in titles
        assert "TV Show 1" not in titles
        assert "TV Mini" not in titles


def test_app_sonarr_endpoint_filters_tv():
    sample_items = [
        {"title": "Movie 1", "type": "movie", "imdbId": "tt111"},
        {"title": "TV Show 1", "type": "tvSeries", "imdbId": "tt222"},
        {"title": "TV Mini", "type": "tvMiniSeries", "imdbId": "tt333"},
        {"title": "TV Special", "type": "tvSpecial", "imdbId": "tt444"},
    ]

    with patch_watchlist(sample_items):
        response = client.get("/sonarr?user_id=ur123")
        assert response.status_code == 200
        results = response.json()
        titles = [r["title"] for r in results]
        assert "TV Show 1" in titles
        assert "TV Mini" in titles
        assert "TV Special" in titles
        assert "Movie 1" not in titles


def test_scrape_lock_prevents_duplicate():
    existing = imdb_server.SCRAPE_LOCKS.copy()
    try:
        imdb_server.SCRAPE_LOCKS.add("ur999")
        result = scrape_imdb_watchlist("ur999")
        assert result is None

        result2 = scrape_imdb_watchlist("https://www.imdb.com/user/ur999/lists")
        assert result2 is None
    finally:
        imdb_server.SCRAPE_LOCKS.discard("ur999")


def test_get_user_id_empty_string():
    result = get_user_id("")
    assert result == ""


def test_get_user_id_partial_url():
    result = get_user_id("some text imdb.com/user/ur555/lists more text")
    assert result == "ur555"
