import threading
import time
import logging
import requests
from flask import Flask, render_template, request, jsonify
import schedule

from sync import load_config, save_config, config_is_valid, sync, sync_log, sync_stats

log = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
app = Flask(__name__)

# --- Background scheduler ---
scheduler_thread = None


def run_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(10)


def restart_scheduler():
    global scheduler_thread
    schedule.clear()
    cfg = load_config()
    interval = cfg.get("sync_interval_minutes", 30)
    schedule.every(interval).minutes.do(sync)
    log.info("Scheduler set: sync every %d minutes", interval)
    if scheduler_thread is None or not scheduler_thread.is_alive():
        scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
        scheduler_thread.start()


# --- Routes ---
@app.route("/")
def index():
    return render_template("index.html", config=load_config())


@app.route("/api/config", methods=["POST"])
def api_save_config():
    cfg = request.get_json()
    save_config(cfg)
    restart_scheduler()
    return jsonify({"status": "ok"})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    cfg = load_config()
    if not config_is_valid(cfg):
        return jsonify({"message": "Config incomplete — fill in all required fields."}), 400
    threading.Thread(target=sync, args=(cfg,), daemon=True).start()
    return jsonify({"message": "Sync started in background."})


@app.route("/api/fetch-options", methods=["POST"])
def api_fetch_options():
    """Fetch root folders and quality profiles from Sonarr/Radarr."""
    data = request.get_json()
    url = data.get("url", "").rstrip("/")
    api_key = data.get("api_key", "")
    if not url or not api_key:
        return jsonify({"error": "URL and API key required"}), 400
    headers = {"X-Api-Key": api_key}
    result = {}
    try:
        resp = requests.get(f"{url}/api/v3/rootfolder", headers=headers, timeout=10)
        resp.raise_for_status()
        result["root_folders"] = [{"path": rf["path"], "id": rf.get("id")} for rf in resp.json()]
    except Exception as e:
        result["root_folders_error"] = str(e)
    try:
        resp = requests.get(f"{url}/api/v3/qualityprofile", headers=headers, timeout=10)
        resp.raise_for_status()
        result["quality_profiles"] = [{"id": qp["id"], "name": qp["name"]} for qp in resp.json()]
    except Exception as e:
        result["quality_profiles_error"] = str(e)
    return jsonify(result)


@app.route("/api/logs")
def api_logs():
    return jsonify({"logs": list(sync_log)})


@app.route("/api/stats")
def api_stats():
    return jsonify(sync_stats)


if __name__ == "__main__":
    restart_scheduler()
    # Run initial sync if config is valid
    cfg = load_config()
    if config_is_valid(cfg):
        threading.Thread(target=sync, args=(cfg,), daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
