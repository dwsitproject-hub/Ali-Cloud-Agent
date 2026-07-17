"""Authentication: DWS Hub SSO via OpenID Connect (Authorization Code + PKCE)
plus a local dev-login bypass.

The DWS Hub is an OAuth2/OIDC provider and this app is a **public client**
(PKCE, no client secret). Flow:

  GET /auth/oidc/login    -> redirect to the Hub's authorize endpoint (with PKCE)
  GET /auth/oidc/callback -> exchange the code (+verifier) for tokens, verify the
                             id_token via the Hub's JWKS, establish a session
  GET /auth/login         -> send unauthenticated visitors to the frontend login page
  GET /auth/info          -> public: which sign-in options are available
  GET/POST /auth/dev-login-> local bypass (gated by DEV_LOGIN_ENABLED)
  GET /auth/logout

Authlib handles discovery (``server_metadata_url``), the PKCE code_verifier/
challenge, ``state``/``nonce``, the token exchange at the Hub's token endpoint,
and id_token signature/claims validation against the Hub's JWKS.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, abort, jsonify, redirect
from flask_login import login_user, logout_user

import config
import seed
from db import db
from models import User

log = logging.getLogger("auth")

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

oauth = OAuth()
_HUB = "dwshub"  # Authlib client registration name


def init_oauth(app) -> None:
    """Register the DWS Hub OIDC client. Called from app.create_app().

    Registration is lazy about the network — Authlib fetches the discovery
    document + JWKS on first use, not here, so the app boots even if the Hub is
    briefly unreachable. Skipped entirely when OIDC isn't configured (e.g. local
    dev running on the dev-login bypass)."""
    oauth.init_app(app)
    if config.oidc_configured():
        oauth.register(
            name=_HUB,
            client_id=config.OIDC_CLIENT_ID,
            server_metadata_url=config.OIDC_DISCOVERY_URL,
            client_kwargs={
                "scope": config.OIDC_SCOPES,
                "code_challenge_method": "S256",   # PKCE
            },
            token_endpoint_auth_method="none",     # public client — no secret
        )


def _now():
    return datetime.now(timezone.utc)


def _login_existing_or_new(user_id: str, email: str) -> None:
    """Upsert a user by stable id (OIDC ``sub``) and start a session."""
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
    # The login UI lives on the frontend; send unauthenticated visitors there.
    return redirect(config.FRONTEND_URL + "/login.html")


@auth_bp.route("/info")
def info():
    """Public: lets the frontend login page show the right sign-in option(s)."""
    return jsonify(
        oidc_enabled=config.oidc_configured(),
        dev_login_enabled=config.DEV_LOGIN_ENABLED,
    )


@auth_bp.route("/oidc/login")
def oidc_login():
    """Begin the OIDC Authorization Code + PKCE flow (redirect to the Hub)."""
    if not config.oidc_configured():
        abort(404)
    client = oauth.create_client(_HUB)
    return client.authorize_redirect(config.OIDC_REDIRECT_URI)


@auth_bp.route("/oidc/callback")
def oidc_callback():
    """Handle the Hub redirect: exchange code, verify id_token, log in."""
    if not config.oidc_configured():
        abort(404)
    client = oauth.create_client(_HUB)
    # Validates state, exchanges the code (+PKCE verifier), and verifies the
    # id_token's signature (JWKS), issuer, audience, expiry and nonce.
    token = client.authorize_access_token()
    claims = token.get("userinfo") or {}
    if not claims.get("sub"):
        # Fall back to the userinfo endpoint if the id_token carried no claims.
        try:
            claims = client.userinfo(token=token)
        except Exception:  # pragma: no cover - depends on the live provider
            claims = {}

    sub = claims.get("sub")
    email = (claims.get("email") or "").strip().lower()
    if not sub:
        log.warning("OIDC callback: no 'sub' in id_token/userinfo")
        return "Invalid token payload (no subject)", 400

    _login_existing_or_new(str(sub), email)
    log.info("OIDC login: %s", email or sub)
    # 303 so the browser issues a top-level GET to the frontend with the cookie.
    return redirect(config.FRONTEND_URL, code=303)


@auth_bp.route("/dev-login", methods=["GET", "POST"])
def dev_login():
    if not config.DEV_LOGIN_ENABLED:
        abort(404)
    _login_existing_or_new(seed.DEV_USER_ID, config.DEV_LOGIN_EMAIL)
    log.info("dev login: %s", config.DEV_LOGIN_EMAIL)
    return redirect(config.FRONTEND_URL, code=303)


@auth_bp.route("/logout")
def logout():
    logout_user()
    return redirect(config.FRONTEND_URL + "/login.html")
