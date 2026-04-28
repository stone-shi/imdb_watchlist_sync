---
name: imdb-watchlist-manager
description: Interface for managing and querying IMDb watchlists via the local sync server. Supports fetching lists for Radarr/Sonarr, searching cache, and viewing stats.
version: 1.0.0
requirements:
  - name: IMDB_SERVER_URL
    description: The base URL of your IMDb Watchlist Sync server (e.g., http://localhost:8080).
---

# Instructions
Use this skill to interact with the IMDb Watchlist Sync server. You can fetch watchlists, search for movies, or check cache status.

## Endpoints

### 1. Fetch Watchlist
- **URL**: `${IMDB_SERVER_URL}/watchlist?user_id={USER_ID}`
- **Method**: GET
- **Description**: Returns the full watchlist for a user.
- **Formats**: Also supports `${IMDB_SERVER_URL}/radarr` and `${IMDB_SERVER_URL}/sonarr` for specific app formats.

### 2. Search Cache
- **URL**: `${IMDB_SERVER_URL}/search?q={QUERY}`
- **Method**: GET
- **Description**: Searches for a movie title across all cached watchlists.

### 3. Cache Statistics
- **URL**: `${IMDB_SERVER_URL}/stats`
- **Method**: GET
- **Description**: Shows all users in the cache, item counts, and last update timestamps.

## Workflows

### How to fetch a user's list
1. Identify the IMDb User ID (e.g., `ur12345` or `p.username`).
2. Construct the GET request to `/watchlist`.
3. If the response is `504`, inform the user that a background scrape has started and they should try again in a minute.

### How to search for a movie
1. Take the movie title from the user.
2. Query `/search?q=title`.
3. Present results including the `source_user_id` so the user knows which watchlist contains the movie.
