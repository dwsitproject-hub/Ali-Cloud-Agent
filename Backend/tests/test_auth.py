"""SSO + login-guard behavior."""
import time
import uuid

import jwt

import config


def _token(secret=None, exp_delta=60, user_id=None, email="user@example.com",
           alg="HS256"):
    now = int(time.time())
    payload = {
        "user_id": user_id or str(uuid.uuid4()),
        "email": email,
        "iat": now,
        "exp": now + exp_delta,
    }
    return jwt.encode(payload, secret or config.SSO_TOKEN_SECRET, algorithm=alg)


# --- login guard (API-only backend) ----------------------------------------
def test_api_returns_401_json_when_anonymous(anon_client):
    r = anon_client.get("/api/status")
    assert r.status_code == 401
    assert r.get_json()["error"]


def test_health_is_public(anon_client):
    assert anon_client.get("/api/health").status_code == 200


def test_ready_is_public_and_reports_db(anon_client):
    r = anon_client.get("/api/ready")
    assert r.status_code == 200          # DB is up in the test environment
    body = r.get_json()
    assert body["ok"] is True
    assert body["checks"]["db"] == "ok"
    assert "last_scan_age_seconds" in body["checks"]


def test_login_redirects_to_frontend(anon_client):
    r = anon_client.get("/auth/login")
    assert r.status_code in (301, 302, 303)
    assert r.headers["Location"].startswith(config.FRONTEND_URL)
    assert r.headers["Location"].endswith("/login.html")


def test_auth_info_is_public(anon_client):
    r = anon_client.get("/auth/info")
    assert r.status_code == 200
    assert "dev_login_enabled" in r.get_json()


def test_dev_login_then_api_reachable(client):
    # `client` fixture already did dev-login; the API should now be reachable.
    assert client.get("/api/status").status_code == 200


def test_csrf_endpoint_requires_login_and_returns_token(anon_client, client):
    assert anon_client.get("/api/csrf").status_code == 401
    body = client.get("/api/csrf").get_json()
    assert body["csrf_token"]


# --- /auth/hub JWT verification --------------------------------------------
def test_hub_valid_token_logs_in(anon_client):
    r = anon_client.post("/auth/hub", data={"token": _token()})
    assert r.status_code == 303
    # logs in, then redirects to the standalone frontend
    assert r.headers["Location"].startswith(config.FRONTEND_URL)
    # session established -> API now reachable on the same client
    assert anon_client.get("/api/status").status_code == 200


def test_hub_missing_token(anon_client):
    assert anon_client.post("/auth/hub", data={}).status_code == 400


def test_hub_expired_token(anon_client):
    r = anon_client.post("/auth/hub", data={"token": _token(exp_delta=-60)})
    assert r.status_code == 401


def test_hub_bad_signature(anon_client):
    r = anon_client.post("/auth/hub", data={"token": _token(secret="wrong-secret")})
    assert r.status_code == 401


def test_hub_non_uuid_user_id(anon_client):
    r = anon_client.post("/auth/hub", data={"token": _token(user_id="not-a-uuid")})
    assert r.status_code == 400
