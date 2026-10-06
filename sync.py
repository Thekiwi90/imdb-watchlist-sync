import copy
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
    # Each entry: {"name": str, "user_id": "ur...", "list_id": "ls..."}
    "imdb_watchlists": [],
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
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))

    # Migrate legacy single-watchlist config (imdb_user_id / imdb_list_id)
    legacy_user = cfg.pop("imdb_user_id", "") or ""
    legacy_list = cfg.pop("imdb_list_id", "") or os.environ.get("IMDB_LIST_ID", "")
    if not cfg["imdb_watchlists"] and (legacy_user or legacy_list):
        cfg["imdb_watchlists"] = [{"name": "", "user_id": legacy_user, "list_id": legacy_list}]

    cfg["imdb_watchlists"] = [w for w in cfg["imdb_watchlists"] if watchlist_is_valid(w)]
    return cfg


def save_config(cfg: dict):
    cfg = dict(cfg)
    cfg.pop("imdb_user_id", None)
    cfg.pop("imdb_list_id", None)
    cfg["imdb_watchlists"] = [
        {
            "name": (w.get("name") or "").strip(),
            "user_id": (w.get("user_id") or "").strip(),
            "list_id": (w.get("list_id") or "").strip(),
        }
        for w in cfg.get("imdb_watchlists") or []
        if isinstance(w, dict) and watchlist_is_valid(w)
    ]
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def watchlist_is_valid(w: dict) -> bool:
    return bool((w.get("user_id") or "").strip() or (w.get("list_id") or "").strip())


def watchlist_label(w: dict) -> str:
    return w.get("name") or w.get("list_id") or w.get("user_id") or "?"


def config_is_valid(cfg: dict) -> bool:
    if not any(watchlist_is_valid(w) for w in cfg.get("imdb_watchlists", [])):
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


# --- Sync log buffer and stats for web UI ---
sync_log: list[str] = []
MAX_LOG_LINES = 200

sync_stats: dict = {
    "watchlist_count": 0,
    "imdb_total": 0,
    "sonarr_found": 0,
    "sonarr_added": 0,
    "sonarr_existing": 0,
    "radarr_found": 0,
    "radarr_added": 0,
    "radarr_existing": 0,
    "not_found": 0,
    "last_sync": None,
}


class LogCapture(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        sync_log.append(msg)
        if len(sync_log) > MAX_LOG_LINES:
            del sync_log[: len(sync_log) - MAX_LOG_LINES]


log_capture = LogCapture()
log_capture.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.getLogger().addHandler(log_capture)


def fetch_imdb_watchlist(user_id: str, list_id: str = "") -> list[str]:
    """Fetch all IMDb IDs from a public watchlist."""
    user_id = (user_id or "").strip()
    list_id = (list_id or "").strip()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }

    log.info("Fetching IMDb watchlist (user %s, list %s)", user_id or "-", list_id or "-")

    # Use a session for cookies (IMDb anti-bot)
    session = requests.Session()
    session.headers.update(headers)

    # Load auth cookies from env if user has provided a logged-in session.
    # Anonymous scraping is blocked by AWS WAF (HTTP 202 challenge page);
    # passing real cookies bypasses it.
    cookie_str = os.environ.get("IMDB_COOKIES", "").strip()
    if cookie_str:
        loaded = 0
        for kv in cookie_str.split(";"):
            kv = kv.strip()
            if "=" in kv:
                k, _, v = kv.partition("=")
                session.cookies.set(k.strip(), v.strip(), domain=".imdb.com")
                loaded += 1
        log.info("Loaded %d IMDb cookies from IMDB_COOKIES env", loaded)
    else:
        # Step 0 fallback: visit imdb.com to seed any anonymous cookies
        try:
            session.get("https://www.imdb.com/", timeout=15)
        except requests.RequestException:
            pass

    # Step 1: Find watchlist list ID.
    # Prefer the configured value, then fall back to scraping (WAF-blocked).
    if list_id:
        log.info("Using configured list ID: %s", list_id)
    else:
        watchlist_url = f"https://www.imdb.com/user/{user_id}/watchlist/"
        try:
            resp = session.get(watchlist_url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.error("Failed to fetch IMDb watchlist page: %s", e)
            return []

        list_match = re.search(r"(ls\d+)", resp.text)
        if not list_match:
            log.error("Could not find watchlist list ID (AWS WAF likely blocked the page — set the list ID in the config instead)")
            found = re.findall(r"(tt\d{7,})", resp.text)
            return list(dict.fromkeys(found))

        list_id = list_match.group(1)
        log.info("Found watchlist list ID: %s", list_id)

    # Step 2: Use IMDb GraphQL API to fetch all items with pagination
    imdb_ids: list[str] = []
    after = ""
    page_num = 1
    page_size = 250

    while True:
        query = """
        query WatchlistItems($listId: ID!, $first: Int!, $after: ID) {
          list(id: $listId) {
            items(first: $first, after: $after) {
              total
              edges {
                node {
                  item {
                    ... on Title {
                      id
                    }
                  }
                }
              }
              pageInfo {
                hasNextPage
                endCursor
              }
            }
          }
        }
        """
        variables = {"listId": list_id, "first": page_size}
        if after:
            variables["after"] = after

        try:
            resp = session.post(
                "https://graphql.imdb.com/",
                json={"query": query, "variables": variables},
                headers={
                    "Content-Type": "application/json",
                    "Referer": "https://www.imdb.com/",
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            log.error("GraphQL request failed (page %d): %s", page_num, e)
            break
        except Exception as e:
            log.error("Failed to parse GraphQL response (page %d): %s", page_num, e)
            break

        # Extract IDs from response
        try:
            items_data = data["data"]["list"]["items"]
            total = items_data.get("total", "?")
            edges = items_data.get("edges", [])
            page_info = items_data.get("pageInfo", {})

            for edge in edges:
                title_id = edge.get("node", {}).get("item", {}).get("id", "")
                if title_id.startswith("tt"):
                    imdb_ids.append(title_id)

            imdb_ids = list(dict.fromkeys(imdb_ids))
            log.info("Page %d: fetched %d items (%d total on list, %d unique so far)",
                     page_num, len(edges), total, len(imdb_ids))

            if page_info.get("hasNextPage") and page_info.get("endCursor"):
                after = page_info["endCursor"]
                page_num += 1
            else:
                break
        except (KeyError, TypeError) as e:
            log.error("Unexpected GraphQL response structure (page %d): %s", page_num, e)
            # Fallback: extract tt IDs from raw response
            found = re.findall(r"(tt\d{7,})", resp.text)
            imdb_ids.extend(found)
            imdb_ids = list(dict.fromkeys(imdb_ids))
            break

    log.info("Found %d total IMDb IDs on watchlist", len(imdb_ids))
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


def add_to_sonarr(cfg: dict, series: dict):
    """Returns True if added, 'exists' if already exists, False on error."""
    payload = {
        "title": series.get("title", "Unknown"),
        "tvdbId": series["tvdbId"],
        "qualityProfileId": int(cfg["sonarr_quality_profile_id"]),
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
        body = ""
        if hasattr(e, "response") and e.response is not None:
            body = e.response.text[:300]
            if "already been added" in body.lower() or "ExistsValidator" in body:
                log.info("Already in Sonarr: %s", payload["title"])
                return "exists"
        log.error("Failed to add series '%s' to Sonarr: %s %s", payload["title"], e, body)
        return False


def add_to_radarr(cfg: dict, movie: dict):
    """Returns True if added, 'exists' if already exists, False on error."""
    payload = {
        "title": movie.get("title", "Unknown"),
        "tmdbId": movie["tmdbId"],
        "qualityProfileId": int(cfg["radarr_quality_profile_id"]),
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
        body = ""
        if hasattr(e, "response") and e.response is not None:
            body = e.response.text[:300]
            if "already been added" in body.lower() or "ExistsValidator" in body:
                log.info("Already in Radarr: %s", payload["title"])
                return "exists"
        log.error("Failed to add '%s' to Radarr: %s %s", payload["title"], e, body)
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

    watchlists = [w for w in cfg.get("imdb_watchlists", []) if watchlist_is_valid(w)]
    imdb_ids: list[str] = []
    for w in watchlists:
        ids = fetch_imdb_watchlist(w.get("user_id", ""), w.get("list_id", ""))
        log.info("Watchlist '%s': %d items", watchlist_label(w), len(ids))
        imdb_ids.extend(ids)
    imdb_ids = list(dict.fromkeys(imdb_ids))
    if len(watchlists) > 1:
        log.info("Combined %d watchlists into %d unique IMDb IDs", len(watchlists), len(imdb_ids))

    if not imdb_ids:
        log.info("No IMDb IDs found, nothing to do.")
        return

    existing_tvdb = get_existing_sonarr_tvdb_ids(cfg) if sonarr_on else set()
    existing_tmdb = get_existing_radarr_tmdb_ids(cfg) if radarr_on else set()

    added_series = 0
    added_movies = 0
    sonarr_found = 0
    sonarr_existing = 0
    radarr_found = 0
    radarr_existing = 0
    not_found = 0

    for imdb_id in imdb_ids:
        if sonarr_on:
            series = lookup_sonarr(cfg, imdb_id)
            if series and series.get("tvdbId"):
                sonarr_found += 1
                if series["tvdbId"] in existing_tvdb:
                    log.info("Skipping series (already in Sonarr): %s", series.get("title"))
                    sonarr_existing += 1
                else:
                    result = add_to_sonarr(cfg, series)
                    if result == "exists":
                        sonarr_existing += 1
                    elif result:
                        existing_tvdb.add(series["tvdbId"])
                        added_series += 1
                continue

        if radarr_on:
            movie = lookup_radarr(cfg, imdb_id)
            if movie and movie.get("tmdbId"):
                radarr_found += 1
                if movie["tmdbId"] in existing_tmdb:
                    log.info("Skipping movie (already in Radarr): %s", movie.get("title"))
                    radarr_existing += 1
                else:
                    result = add_to_radarr(cfg, movie)
                    if result == "exists":
                        radarr_existing += 1
                    elif result:
                        existing_tmdb.add(movie["tmdbId"])
                        added_movies += 1
                continue

        not_found += 1
        log.warning("No match found for %s in Sonarr or Radarr", imdb_id)

    # Update stats
    from datetime import datetime
    sync_stats["watchlist_count"] = len(watchlists)
    sync_stats["imdb_total"] = len(imdb_ids)
    sync_stats["sonarr_found"] = sonarr_found
    sync_stats["sonarr_added"] = added_series
    sync_stats["sonarr_existing"] = sonarr_existing
    sync_stats["radarr_found"] = radarr_found
    sync_stats["radarr_added"] = added_movies
    sync_stats["radarr_existing"] = radarr_existing
    sync_stats["not_found"] = not_found
    sync_stats["last_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    log.info(
        "--- Sync complete: IMDb: %d | Sonarr: %d found (%d new, %d existing) | Radarr: %d found (%d new, %d existing) | Not matched: %d ---",
        len(imdb_ids), sonarr_found, added_series, sonarr_existing,
        radarr_found, added_movies, radarr_existing, not_found,
    )
