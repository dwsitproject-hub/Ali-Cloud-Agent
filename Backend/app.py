"""Cloud Agent Monitoring - Flask app factory (API-only backend).

This is the BACKEND: a JSON API + auth. The UI is a separate static frontend
(``Frontend/``, served by its own nginx) that calls this API cross-origin.

Endpoints (JSON API blueprint ``main``)
  GET  /api/status    latest scan + alert + history (JSON)
  POST /api/scan      run Agent 1 now (optional ?auto_alert=true)
  POST /api/send      run Agent 2 now against the latest scan (the "Send" button)
  GET  /api/csrf      current CSRF token (for the SPA's state-changing calls)
  GET  /api/health    liveness probe (public)
  GET  /api/ready     readiness: DB + scan freshness (public)

``create_app()`` builds the Flask app; ``_bootstrap(app)`` starts the scheduler,
which runs the first scan immediately in a background thread so the server
begins serving right away. Keep the scheduler to a SINGLE process.
"""
from __future__ import annotations

import atexit
import logging
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, Flask, jsonify, redirect, request, url_for
from flask_cors import CORS
from flask_login import LoginManager, current_user
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect, generate_csrf

import config
import scheduler
import seed
import state
from agents import agent2_alerter
from db import db

# Endpoints reachable without a login session.
_PUBLIC_ENDPOINTS = {
    "static", "main.health", "main.ready",
    "auth.login", "auth.hub", "auth.dev_login", "auth.logout", "auth.info",
}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_BASE_DIR = Path(__file__).resolve().parent  # for the Alembic migrations dir

main_bp = Blueprint("main", __name__)


@main_bp.route("/api/csrf")
def csrf():
    """Hand the SPA a CSRF token for its POST/PATCH/DELETE calls.

    Login-required (so it rides the authenticated session); the token is bound
    to that session and echoed back in the ``X-CSRFToken`` header."""
    return jsonify(csrf_token=generate_csrf())


@main_bp.route("/api/health")
def health():
    """Liveness: the process is up and serving. Always 200 (public)."""
    return jsonify(ok=True)


@main_bp.route("/api/ready")
def ready():
    """Readiness: dependencies are healthy (public).

    Verifies DB connectivity and reports how stale the last scan is (a proxy
    for scheduler liveness). Returns 503 if the DB is unreachable so an
    orchestrator / uptime check can react; scan staleness is reported but is
    non-fatal (a fresh boot legitimately has no scan yet)."""
    from sqlalchemy import text

    checks = {}
    ok = True
    try:
        db.session.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as exc:  # pragma: no cover - exercised only when DB is down
        checks["db"] = f"error: {type(exc).__name__}"
        ok = False

    age = None
    stale = None
    try:
        snap = state.snapshot()
        last = snap.get("last_scan")
        if last and last.get("scanned_at"):
            scanned = datetime.fromisoformat(last["scanned_at"])
            if scanned.tzinfo is None:
                scanned = scanned.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - scanned).total_seconds()
            # Stale if we've missed ~3 scan intervals.
            stale = age > config.SCAN_INTERVAL_MINUTES * 60 * 3
    except Exception:  # DB error already reflected above
        pass
    checks["last_scan_age_seconds"] = age
    checks["scan_stale"] = stale

    return jsonify(ok=ok, checks=checks), (200 if ok else 503)


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
    # Fail fast on an unsafe/incomplete production configuration before we bind
    # to the DB or start serving (see config.validate_startup).
    errors, warnings = config.validate_startup()
    for w in warnings:
        logging.getLogger("config").warning("config: %s", w)
    if errors:
        raise RuntimeError(
            "Refusing to start — invalid production configuration:\n  - "
            + "\n  - ".join(errors))

    # No template/static folders: this backend serves JSON only. The UI is the
    # separate static frontend in Frontend/ (its own nginx).
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=config.SECRET_KEY,
        SQLALCHEMY_DATABASE_URI=config.DATABASE_URL,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        # Recover from stale/closed pooled connections (managed Postgres drops
        # idle ones); pre_ping validates before use, recycle caps connection age.
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True, "pool_recycle": 280},
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE=config.SESSION_COOKIE_SAMESITE,
        SESSION_COOKIE_SECURE=config.SESSION_COOKIE_SECURE,
    )

    # Allow the separate frontend origin to call the API with credentials
    # (session cookie). Never "*" with credentials; pin to CORS_ORIGINS.
    CORS(app, resources={r"/api/*": {"origins": config.CORS_ORIGINS},
                         r"/auth/*": {"origins": config.CORS_ORIGINS}},
         supports_credentials=True,
         allow_headers=["Content-Type", "X-CSRFToken"],
         methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"])

    db.init_app(app)
    # Import models so Flask-Migrate's autogenerate sees every table.
    import models  # noqa: F401
    Migrate(app, db, directory=str(_BASE_DIR / "migrations"))
    seed.register_cli(app)

    # --- Auth: CSRF + login session ---
    csrf = CSRFProtect(app)

    login_manager = LoginManager(app)
    login_manager.login_view = "auth.login"

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(models.User, user_id)

    @login_manager.unauthorized_handler
    def _unauthorized():
        # JSON for API callers (the dashboard polls /api/*); redirect for pages.
        if request.path.startswith("/api/"):
            return jsonify(error="authentication required"), 401
        return redirect(url_for("auth.login"))

    @app.before_request
    def _require_login():
        # Let CORS preflights through untouched (they carry no auth).
        if request.method == "OPTIONS":
            return None
        if request.endpoint in _PUBLIC_ENDPOINTS:
            return None
        if current_user.is_authenticated:
            return None
        return _unauthorized()

    from auth.sso import auth_bp
    from instances_api import instances_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(instances_bp)
    app.register_blueprint(auth_bp)
    # The Hub's cross-site POST carries no CSRF token; the JWT signature is the
    # authenticity check, so exempt just that endpoint.
    csrf.exempt(app.view_functions["auth.hub"])
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
