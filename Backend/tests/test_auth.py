"""Auth + login-guard behavior (OIDC/PKCE + dev-login).

Full OIDC round-trips need a live Hub, so here we cover the login guard, the
public endpoints, the dev-login bypass, and that the OIDC routes are gated when
OIDC isn't configured (the test env runs on dev-login, no Hub)."""
import config


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
    body = r.get_json()
    assert "oidc_enabled" in body and "dev_login_enabled" in body
    # Test env has no OIDC configured, dev-login on.
    assert body["oidc_enabled"] is False
    assert body["dev_login_enabled"] is True


def test_oidc_login_404_when_not_configured(anon_client):
    # No OIDC_DISCOVERY_URL/CLIENT_ID/REDIRECT_URI in the test env.
    assert anon_client.get("/auth/oidc/login").status_code == 404
    assert anon_client.get("/auth/oidc/callback").status_code == 404


def test_dev_login_then_api_reachable(client):
    # `client` fixture already did dev-login; the API should now be reachable.
    assert client.get("/api/status").status_code == 200


def test_csrf_endpoint_requires_login_and_returns_token(anon_client, client):
    assert anon_client.get("/api/csrf").status_code == 401
    body = client.get("/api/csrf").get_json()
    assert body["csrf_token"]
