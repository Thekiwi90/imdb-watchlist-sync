import json
import os
import re
import time
import logging
import requests
from bs4 import BeautifulSoup
import schedule

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/data/config.json")

DEFAULT_CONFIG = {
    "imdb_user_id": "",
    "sonarr_enabled": True,
    "sonarr_url": "http://sonarr:8989",
    "sonarr_api_key": "",
    "sonarr_root_folder": "/tv",
    "sonarr_quality_profile_id": 1,
    "radarr_enabled": True,
    "radarr_url": "http://radarr:7878",
    "radarr_api_key": "",
    "radarr_root_folder": "/movies",
    "radarr_quality_profile_id": 1,
    "sync_interval_minutes": 60,
}


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def config_is_valid(cfg: dict) -> bool:
    if not cfg.get("imdb_user_id"):
        return False
    sonarr_on = cfg.get("sonarr_enabled", True)
    radarr_on = cfg.get("radarr_enabled", True)
    if not sonarr_on and not radarr_on:
        return False
    if sonarr_on and not cfg.get("sonarr_api_key"):
        return False
    if radarr_on and not cfg.get("radarr_api_key"):
        return False
    return True


# --- Sync log buffer for web UI ---
sync_log: list[str] = []
MAX_LOG_LINES = 200


class LogCapture(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        sync_log.append(msg)
        if len(sync_log) > MAX_LOG_LINES:
            del sync_log[: len(sync_log) - MAX_LOG_LINES]


log_capture = LogCapture()
log_capture.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger().addHandler(log_capture)


def fetch_imdb_watchlist(cfg: dict) -> list[str]:
    url = f"https://www.imdb.com/user/{cfg['imdb_user_id']}/watchlist/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }

    log.info("Fetching IMDb watchlist for user %s", cfg["imdb_user_id"])
    imdb_ids: list[str] = []
    page = 1

    while True:
        params = {"page": page} if page > 1 else {}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("Failed to fetch IMDb watchlist page %d: %s", page, e)
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        found = re.findall(r"(tt\d{7,})", resp.text)
        unique = list(dict.fromkeys(found))
        imdb_ids.extend(unique)

        next_link = soup.select_one("a.lister-page-next")
        if next_link:
            page += 1
        else:
            break

    imdb_ids = list(dict.fromkeys(imdb_ids))
    log.info("Found %d IMDb IDs on watchlist", len(imdb_ids))
    return imdb_ids


def get_existing_sonarr_tvdb_ids(cfg: dict) -> set[int]:
    try:
        resp = requests.get(
            f"{cfg['sonarr_url'].rstrip('/')}/api/v3/series",
            headers={"X-Api-Key": cfg["sonarr_api_key"]},
            timeout=30,
        )
        resp.raise_for_status()
        return {s["tvdbId"] for s in resp.json() if s.get("tvdbId")}
    except requests.RequestException as e:
        log.error("Failed to fetch existing Sonarr series: %s", e)
        return set()


def get_existing_radarr_tmdb_ids(cfg: dict) -> set[int]:
    try:
        resp = requests.get(
            f"{cfg['radarr_url'].rstrip('/')}/api/v3/movie",
            headers={"X-Api-Key": cfg["radarr_api_key"]},
            timeout=30,
        )
        resp.raise_for_status()
        return {m["tmdbId"] for m in resp.json() if m.get("tmdbId")}
    except requests.RequestException as e:
        log.error("Failed to fetch existing Radarr movies: %s", e)
        return set()


def lookup_sonarr(cfg: dict, imdb_id: str) -> dict | None:
    try:
        resp = requests.get(
            f"{cfg['sonarr_url'].rstrip('/')}/api/v3/series/lookup",
            params={"term": f"imdb:{imdb_id}"},
            headers={"X-Api-Key": cfg["sonarr_api_key"]},
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            return results[0]
    except requests.RequestException as e:
        log.error("Sonarr lookup failed for %s: %s", imdb_id, e)
    return None


def lookup_radarr(cfg: dict, imdb_id: str) -> dict | None:
    try:
        resp = requests.get(
            f"{cfg['radarr_url'].rstrip('/')}/api/v3/movie/lookup",
            params={"term": f"imdb:{imdb_id}"},
            headers={"X-Api-Key": cfg["radarr_api_key"]},
            timeout=30,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            return results[0]
    except requests.RequestException as e:
        log.error("Radarr lookup failed for %s: %s", imdb_id, e)
    return None


def add_to_sonarr(cfg: dict, series: dict) -> bool:
    payload = {
        "title": series.get("title", "Unknown"),
        "tvdbId": series["tvdbId"],
        "qualityProfileId": cfg["sonarr_quality_profile_id"],
        "rootFolderPath": cfg["sonarr_root_folder"],
        "monitored": True,
        "addOptions": {"searchForMissingEpisodes": True},
    }
    try:
        resp = requests.post(
            f"{cfg['sonarr_url'].rstrip('/')}/api/v3/series",
            json=payload,
            headers={"X-Api-Key": cfg["sonarr_api_key"]},
            timeout=30,
        )
        resp.raise_for_status()
        log.info("Added series to Sonarr: %s", payload["title"])
        return True
    except requests.RequestException as e:
        log.error("Failed to add series '%s' to Sonarr: %s", payload["title"], e)
        return False


def add_to_radarr(cfg: dict, movie: dict) -> bool:
    payload = {
        "title": movie.get("title", "Unknown"),
        "tmdbId": movie["tmdbId"],
        "qualityProfileId": cfg["radarr_quality_profile_id"],
        "rootFolderPath": cfg["radarr_root_folder"],
        "monitored": True,
        "addOptions": {"searchForMovie": True},
    }
    try:
        resp = requests.post(
            f"{cfg['radarr_url'].rstrip('/')}/api/v3/movie",
            json=payload,
            headers={"X-Api-Key": cfg["radarr_api_key"]},
            timeout=30,
        )
        resp.raise_for_status()
        log.info("Added movie to Radarr: %s", payload["title"])
        return True
    except requests.RequestException as e:
        log.error("Failed to add movie '%s' to Radarr: %s", payload["title"], e)
        return False


def sync(cfg: dict | None = None):
    if cfg is None:
        cfg = load_config()
    if not config_is_valid(cfg):
        log.warning("Config incomplete, skipping sync.")
        return

    sonarr_on = cfg.get("sonarr_enabled", True)
    radarr_on = cfg.get("radarr_enabled", True)
    log.info("--- Starting sync (Sonarr: %s, Radarr: %s) ---",
             "ON" if sonarr_on else "OFF", "ON" if radarr_on else "OFF")

    imdb_ids = fetch_imdb_watchlist(cfg)
    if not imdb_ids:
        log.info("No IMDb IDs found, nothing to do.")
        return

    existing_tvdb = get_existing_sonarr_tvdb_ids(cfg) if sonarr_on else set()
    existing_tmdb = get_existing_radarr_tmdb_ids(cfg) if radarr_on else set()

    added_series = 0
    added_movies = 0
    skipped = 0

    for imdb_id in imdb_ids:
        if sonarr_on:
            series = lookup_sonarr(cfg, imdb_id)
            if series and series.get("tvdbId"):
                if series["tvdbId"] in existing_tvdb:
                    log.info("Skipping series (already in Sonarr): %s", series.get("title"))
                    skipped += 1
                else:
                    if add_to_sonarr(cfg, series):
                        existing_tvdb.add(series["tvdbId"])
                        added_series += 1
                continue

        if radarr_on:
            movie = lookup_radarr(cfg, imdb_id)
            if movie and movie.get("tmdbId"):
                if movie["tmdbId"] in existing_tmdb:
                    log.info("Skipping movie (already in Radarr): %s", movie.get("title"))
                    skipped += 1
                else:
                    if add_to_radarr(cfg, movie):
                        existing_tmdb.add(movie["tmdbId"])
                        added_movies += 1
                continue

        log.warning("No match found for %s in Sonarr or Radarr", imdb_id)

    log.info(
        "--- Sync complete: %d series added, %d movies added, %d skipped ---",
        added_series, added_movies, skipped,
    )
