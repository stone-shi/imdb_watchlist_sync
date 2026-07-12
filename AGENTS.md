# IMDb Watchlist Sync - Development Guide

## Project Overview
This project is a FastAPI-based server designed to scrape IMDb watchlists (supporting the modern `p.` and `ur` ID formats) and serve them as JSON lists compatible with Radarr and Sonarr.

## Core Technologies
- **Python 3.12+**
- **FastAPI**: Web framework for the API endpoints.
- **SeleniumBase (Undetected Mode)**: Primary scraping engine used to bypass IMDb's AWS WAF challenges.
- **BeautifulSoup4**: HTML parsing of the `__NEXT_DATA__` script tag.
- **Docker**: For consistent deployment with all browser dependencies.

## Key Files
- `imdb_server.py`: The main application containing the scraper, API, and CLI logic.
- `Dockerfile`: Container definition including Google Chrome and Selenium drivers.
- `requirements.txt`: Python dependencies.
- `watchlist_cache.json`: Local cache file generated at runtime.

## Development Workflows
- **Testing Scraper**: Use `./venv/bin/python imdb_server.py --user-id <ID> --list` to test scraping logic without starting the server.
- **WAF Bypass**: The scraper uses `driver.uc_open_with_reconnect(url, 6)` which is the most reliable way to handle the background AWS WAF challenges.
- **Caching**: The default cache duration is 1 hour (3600 seconds).

## Future Development Ideas
- **Multi-user support**: Improve the `WATCHLIST_CACHE` handling for multiple concurrent users.
- **Pagination**: Handle watchlists with more than 100 items (requires simulating scroll or GraphQL pagination).
- **RSS Support**: Add an `/rss` endpoint for legacy tools that don't support JSON custom lists.
