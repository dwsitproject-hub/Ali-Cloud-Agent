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


# --- login guard ------------------------------------------------------------
def test_pages_require_login(anon_client):
    r = anon_client.get("/")
    assert r.status_code in (302, 303)
    assert "/auth/login" in r.headers["Location"]


def test_api_returns_401_json_when_anonymous(anon_client):
    r = anon_client.get("/api/status")
    assert r.status_code == 401
    assert r.get_json()["error"]


def test_health_is_public(anon_client):
    assert anon_client.get("/api/health").status_code == 200


def test_login_page_renders(anon_client):
    assert anon_client.get("/auth/login").status_code == 200


def test_dev_login_then_dashboard(client):
    # `client` fixture already did dev-login; dashboard should be reachable.
    assert client.get("/").status_code == 200


# --- /auth/hub JWT verification --------------------------------------------
def test_hub_valid_token_logs_in(anon_client):
    r = anon_client.post("/auth/hub", data={"token": _token()})
    assert r.status_code == 303
    assert "/" == r.headers["Location"] or r.headers["Location"].endswith("/")
    # session established -> dashboard now reachable on the same client
    assert anon_client.get("/").status_code == 200


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
