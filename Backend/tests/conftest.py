"""Shared pytest fixtures.

Forces MOCK_MODE before the app is imported so tests never touch Alibaba Cloud
or send real mail. ``load_dotenv`` does not override existing env vars, so
setting them here wins over any local .env.
"""
import os
import sys
from pathlib import Path

# Make the flat Backend/ modules importable (`import app`, `import config`, ...)
# regardless of pytest's launch directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Must be set BEFORE config/app are imported anywhere.
os.environ.setdefault("MOCK_MODE", "true")
os.environ.setdefault("HEALTHCHECK_ENABLED", "true")
os.environ.setdefault("AUTO_ALERT_ON_BREACH", "false")  # keep scans side-effect free
os.environ.setdefault("ALERT_RECIPIENTS", "test@example.com")
os.environ.setdefault("DEV_LOGIN_ENABLED", "true")
os.environ.setdefault("SSO_TOKEN_SECRET", "test-sso-secret")

import pytest  # noqa: E402


def make_app():
    """Build a fresh app configured for testing (CSRF off)."""
    import importlib
    app_mod = importlib.import_module("app")
    flask_app = app_mod.create_app()
    flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return flask_app


@pytest.fixture
def anon_client():
    """Test client with no login session."""
    return make_app().test_client()


@pytest.fixture
def client():
    """Authenticated test client (logged in via the dev-login bypass)."""
    c = make_app().test_client()
    resp = c.post("/auth/dev-login")
    assert resp.status_code in (200, 302, 303), resp.status_code
    return c
