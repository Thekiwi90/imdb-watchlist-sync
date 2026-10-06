# IMDb Watchlist → Sonarr/Radarr Sync

Small Flask + Docker service that periodically reads one or more public IMDb
watchlists and adds new titles to Sonarr (series) and Radarr (movies).

- Live: https://imdbsync.ralfeerens.nl (Coolify on Proxmox VM 192.168.1.152)
- Repo: `git@github.com:Thekiwi90/imdb-watchlist-sync.git`, branch `main`
- Deploy: Coolify auto-deploys on push to `main`

## Files

- `app.py` — Flask web UI + API, background scheduler (`schedule`), runs an initial sync on start
- `sync.py` — config load/save, IMDb fetching, Sonarr/Radarr lookup + add, log buffer and stats
- `templates/index.html` — single-page config UI (watchlists, Sonarr, Radarr, interval, stats, log)
- `Dockerfile` — `python:3.11-slim`, runs `python app.py` on port 5000
- `docker-compose.yml` — local run, exposes `5050:5000`, volume on `/app/data`

## Configuration

Config is stored as JSON at `CONFIG_PATH` (default `/app/data/config.json`) and edited via the web UI.
Key fields:

```json
{
  "imdb_watchlists": [
    {"name": "Ralf", "user_id": "ur36501984", "list_id": "ls008497440"}
  ],
  "sonarr_enabled": true, "sonarr_url": "...", "sonarr_api_key": "...",
  "sonarr_root_folder": "/tv", "sonarr_quality_profile_id": 1,
  "radarr_enabled": true, "radarr_url": "...", "radarr_api_key": "...",
  "radarr_root_folder": "/movies", "radarr_quality_profile_id": 1,
  "sync_interval_minutes": 60
}
```

- A watchlist entry needs a `user_id` or a `list_id`; prefer `list_id` (see IMDb notes below).
- Legacy single-list configs (`imdb_user_id` / `imdb_list_id`) are migrated into `imdb_watchlists`
  on load (`load_config`) and the legacy keys are dropped on save.

Env vars:

- `CONFIG_PATH` — config file location
- `IMDB_COOKIES` — optional `k=v; k2=v2` cookie string from a logged-in IMDb session (bypasses AWS WAF)
- `IMDB_LIST_ID` — only used when migrating a legacy config without a list ID

## Sync flow

1. Fetch every configured watchlist and merge the IMDb IDs (`tt…`), deduplicated
2. Per ID: Sonarr lookup `GET /api/v3/series/lookup?term=imdb:{id}` → series if found
3. Otherwise Radarr lookup `GET /api/v3/movie/lookup?term=imdb:{id}` → movie if found
4. Skip if `tvdbId` / `tmdbId` already exists (pre-fetched via `GET /api/v3/series` / `/movie`);
   "already been added" errors from the add call also count as existing
5. Add via `POST /api/v3/series` / `POST /api/v3/movie` with monitoring + search enabled

## IMDb notes

- Anonymous scraping of `imdb.com/user/<id>/watchlist/` is blocked by AWS WAF (HTTP 202 challenge),
  so list-ID discovery from the user ID usually fails. Set the list ID explicitly
  (open the watchlist in a browser; the URL contains `ls<digits>`).
- Items are fetched via the IMDb GraphQL API (`graphql.imdb.com`, `list(id:).items`, 250 per page).

## Local testing

The container uses Python 3.11 (code uses `X | None` syntax; macOS system Python 3.9 won't import it):

```bash
uv venv -p 3.11 .venv && VIRTUAL_ENV=.venv uv pip install -r requirements.txt
CONFIG_PATH=./data/config.json .venv/bin/python app.py   # http://localhost:5000
```
