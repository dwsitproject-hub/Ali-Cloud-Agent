"""SSO authentication (Downstream Hub) + local dev login.

The Hub auto-POSTs a single-use HS256 JWT to POST /auth/hub (see
Docs/SSO-TARGET-APP-INTEGRATION.md). We verify it with the shared secret,
upsert the user, and establish a Flask-Login session.

Routes
  GET  /auth/login       login landing page
  POST /auth/hub         Hub SSO entry (CSRF-exempt; verified by JWT signature)
  GET  /auth/dev-login   local dev bypass (gated by DEV_LOGIN_ENABLED)
  POST /auth/dev-login
  GET  /auth/logout
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import jwt
from flask import (Blueprint, abort, current_app, redirect, render_template,
                   request, url_for)
from flask_login import login_user, logout_user

import config
import seed
from db import db
from models import User

log = logging.getLogger("auth")

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


def _now():
    return datetime.now(timezone.utc)


def _login_existing_or_new(user_id: str, email: str) -> None:
    """Upsert a user by Hub user_id and start a session."""
    user = db.session.get(User, user_id)
    if user is None:
        user = User(id=user_id, email=email)
        db.session.add(user)
    else:
        user.email = email
    user.last_login_at = _now()
    db.session.commit()
    login_user(user)


@auth_bp.route("/login")
def login():
    return render_template("login.html", dev_login=config.DEV_LOGIN_ENABLED)


@auth_bp.route("/hub", methods=["POST"])
def hub():
    """Receive and verify the Hub's SSO token, then log the user in.

    CSRF-exempt by design (cross-site POST from the Hub); the JWT signature is
    the authenticity check. Exemption is registered in app.create_app().
    """
    token = request.form.get("token")
    if not token:
        return "Missing token", 400
    if not config.SSO_TOKEN_SECRET:
        log.error("SSO_TOKEN_SECRET is not configured")
        return "SSO not configured on this server", 500

    try:
        payload = jwt.decode(
            token, config.SSO_TOKEN_SECRET,
            algorithms=["HS256"], leeway=config.SSO_TOKEN_LEEWAY,
        )
    except jwt.ExpiredSignatureError:
        return "Token expired", 401
    except jwt.InvalidTokenError:
        return "Invalid token", 401

    user_id = payload.get("user_id")
    email = (payload.get("email") or "").strip().lower()
    if not user_id or not email:
        return "Invalid token payload", 400
    try:
        uuid.UUID(str(user_id))  # Hub user_id is a UUID
    except ValueError:
        return "Invalid user_id", 400

    _login_existing_or_new(str(user_id), email)
    log.info("SSO login: %s", email)
    # 303 so the browser issues a top-level GET (carries the new session cookie).
    return redirect(url_for("main.dashboard"), code=303)


@auth_bp.route("/dev-login", methods=["GET", "POST"])
def dev_login():
    if not config.DEV_LOGIN_ENABLED:
        abort(404)
    _login_existing_or_new(seed.DEV_USER_ID, config.DEV_LOGIN_EMAIL)
    log.info("dev login: %s", config.DEV_LOGIN_EMAIL)
    return redirect(url_for("main.dashboard"), code=303)


@auth_bp.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
