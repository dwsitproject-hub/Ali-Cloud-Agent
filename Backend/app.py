"""Cloud Agent Monitoring - Flask app factory.

Endpoints (UI + JSON API blueprint ``main``)
  GET  /              dashboard UI
  GET  /api/status    latest scan + alert + history (JSON)
  POST /api/scan      run Agent 1 now (optional ?auto_alert=true)
  POST /api/send      run Agent 2 now against the latest scan (the "Send" button)
  GET  /api/health    liveness probe

``create_app()`` builds the Flask app; ``_bootstrap(app)`` starts the scheduler,
which runs the first scan immediately in a background thread so the server
begins serving right away. Keep the scheduler to a SINGLE process.
"""
from __future__ import annotations

import atexit
import logging
from pathlib import Path

from flask import Blueprint, Flask, jsonify, render_template, request
from flask_migrate import Migrate

import config
import scheduler
import seed
import state
from agents import agent2_alerter
from db import db

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_BASE_DIR = Path(__file__).resolve().parent
# Templates + static assets live in the sibling Frontend/ folder (one level up
# from Backend/), not under the Python package.
_FRONTEND = _BASE_DIR.parent / "Frontend"

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def dashboard():
    return render_template("dashboard.html",
                           interval=config.SCAN_INTERVAL_MINUTES,
                           mock=config.MOCK_MODE or not config.credentials_present(),
                           auto_alert=config.AUTO_ALERT_ON_BREACH,
                           recipients=config.ALERT_RECIPIENTS)


@main_bp.route("/api/health")
def health():
    return jsonify(ok=True)


@main_bp.route("/api/status")
def status():
    snap = state.snapshot()
    snap["config"] = {
        "mode": "mock" if (config.MOCK_MODE or not config.credentials_present()) else "live",
        "interval_minutes": config.SCAN_INTERVAL_MINUTES,
        "auto_alert_on_breach": config.AUTO_ALERT_ON_BREACH,
        "recipients": config.ALERT_RECIPIENTS,
    }
    return jsonify(snap)


@main_bp.route("/api/scan", methods=["POST"])
def scan_now():
    auto = request.args.get("auto_alert", "false").lower() in ("1", "true", "yes")
    scan = scheduler.run_scan_job(auto_alert=auto)
    return jsonify(scan)


@main_bp.route("/api/send", methods=["POST"])
def send_now():
    """The Send button - fire Agent 2 against the most recent scan."""
    snap = state.snapshot()
    scan = snap.get("last_scan")
    if not scan:
        scan = scheduler.run_scan_job(auto_alert=False)  # nothing scanned yet
    alert = agent2_alerter.send_alert(scan)
    alert["trigger"] = "manual"
    state.set_last_alert(alert)
    return jsonify(alert)


def create_app() -> Flask:
    """Build and configure the Flask application."""
    app = Flask(__name__,
                template_folder=str(_FRONTEND / "templates"),
                static_folder=str(_FRONTEND / "static"))
    app.config.update(
        SECRET_KEY=config.SECRET_KEY,
        SQLALCHEMY_DATABASE_URI=config.DATABASE_URL,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )

    db.init_app(app)
    # Import models so Flask-Migrate's autogenerate sees every table.
    import models  # noqa: F401
    Migrate(app, db, directory=str(_BASE_DIR / "migrations"))
    seed.register_cli(app)

    from instances_api import instances_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(instances_bp)
    return app


def _bootstrap(app: Flask) -> None:
    # Start the scheduler; it runs the first scan immediately in a background
    # thread (so the server starts serving right away instead of blocking on
    # ~27 live API calls). The dashboard shows "loading" until the first scan
    # lands, then auto-refreshes. The scheduler captures ``app`` so its
    # background jobs run inside an application context.
    scheduler.start(app)
    atexit.register(scheduler.shutdown)


if __name__ == "__main__":
    _app = create_app()
    _bootstrap(_app)
    # use_reloader=False so the scheduler isn't started twice
    _app.run(host=config.FLASK_HOST, port=config.FLASK_PORT,
             debug=False, use_reloader=False)
