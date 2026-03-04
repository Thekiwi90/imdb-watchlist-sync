# IMDb Watchlist Sync

Automatically sync your public IMDb watchlist with Sonarr and Radarr. TV series get added to Sonarr, movies to Radarr.

Runs as a lightweight Docker container with a built-in web UI for configuration.

## Features

- Scrapes public IMDb watchlists for new items
- Automatically identifies TV series vs movies
- Adds series to Sonarr with episode search enabled
- Adds movies to Radarr with movie search enabled
- Skips duplicates already in your library
- Sonarr and Radarr can be enabled/disabled independently
- Configurable sync interval
- Root folders and quality profiles are loaded directly from Sonarr/Radarr
- Live log viewer in the web UI

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/imdb-watchlist-sync.git
cd imdb-watchlist-sync
```

### 2. Build and run

```bash
docker build -t imdb-sync .
docker-compose up -d
```

### 3. Configure

Open `http://YOUR_HOST:5050` in your browser and fill in:

- **IMDb User ID** — found in your IMDb profile URL (e.g. `ur12345678`)
- **Sonarr/Radarr URL** — the address of your Sonarr/Radarr instance
- **Sonarr/Radarr API Key** — found in Settings → General in Sonarr/Radarr

Click **"Connect & load options"** to automatically load root folders and quality profiles from your Sonarr/Radarr instance.

### 4. Save and sync

Click **Save Settings**, then **Sync Now** to run immediately — or wait for the scheduled interval.

## Configuration

All settings are managed through the web UI and stored in a Docker volume. No `.env` file needed.

| Setting | Description | Default |
|---|---|---|
| IMDb User ID | Your IMDb user ID | — |
| Sonarr URL | Sonarr instance URL | `http://sonarr:8989` |
| Sonarr API Key | Sonarr API key | — |
| Radarr URL | Radarr instance URL | `http://radarr:7878` |
| Radarr API Key | Radarr API key | — |
| Sync Interval | Minutes between syncs | `60` |

## Docker Compose

```yaml
services:
  imdb-sync:
    image: imdb-sync
    container_name: imdb_sync
    restart: unless-stopped
    ports:
      - "5050:5000"
    volumes:
      - imdb-sync-data:/app/data

volumes:
  imdb-sync-data:
```

## Requirements

- Docker
- A public IMDb watchlist
- Sonarr and/or Radarr instance

## Notes

- Your IMDb watchlist must be set to **public** for scraping to work
- The container uses port 5050 by default — change it in `docker-compose.yml` if needed
- Sonarr/Radarr URLs must be reachable from the container (use your host IP, not `localhost`)

## License

MIT
