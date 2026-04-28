# IMDb Watchlist Server for *arr

This CLI tool scrapes your public IMDb watchlist and serves it in a JSON format compatible with Radarr and Sonarr import lists.

## Setup

### Local Setup
1. **Install Dependencies**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Run the Server**:
   ```bash
   python imdb_server.py --port 8080
   ```

### Docker Compose Setup
1. **Create `docker-compose.yml`**:
   ```yaml
   services:
     imdb-server:
       image: imdb-watchlist-server:latest
       container_name: imdb-server
       ports:
         - "8080:8080"
       volumes:
         - ./data:/app/data
       restart: unless-stopped
   ```

2. **Start the Stack**:
   ```bash
   docker compose up -d
   ```

3. **Pre-cache your watchlist** (optional):
   ```bash
   python imdb_server.py --user-id "https://www.imdb.com/user/p.xxxxxxx/watchlist" --scrape-only
   ```

## Integration with Radarr

1. Go to **Settings > Lists**.
2. Click **+** to add a new list.
3. Select **Custom List** (or **Steven Lu** in some versions).
4. **Name**: IMDb Watchlist
5. **List URL**: `http://your-server-ip:8080/radarr?user_id=p.xxxxxxxxx`
   - Replace `your-server-ip` with the IP of the machine running this script.
   - Replace the `user_id` with your actual IMDb ID or URL.
6. Click **Test** and then **Save**.

## Integration with Sonarr

1. Go to **Settings > Import Lists**.
2. Click **+** and select **Advanced List > Custom List**.
3. **List URL**: `http://your-server-ip:8080/sonarr?user_id=p.xxxxxxxxx`
4. Note: Sonarr primarily uses TVDb IDs. Since IMDb only provides IMDb IDs, Sonarr might struggle unless it has a built-in mapper for the titles.

## Troubleshooting

- **AWS WAF Challenge**: IMDb blocks many cloud/datacenter IPs. If you see "IMDb is challenging the request", run the script from a residential IP (e.g., your home computer or NAS).
- **Public Visibility**: Ensure your IMDb watchlist is set to **Public** in IMDb settings (Manage List -> Settings -> Visibility).
- **New URL Format**: This tool supports the new `p.` style IDs and `ur` style IDs automatically.
