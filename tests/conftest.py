"""Shared pytest fixtures.

Forces MOCK_MODE before the app is imported so tests never touch Alibaba Cloud
or send real mail. ``load_dotenv`` does not override existing env vars, so
setting them here wins over any local .env.
"""
import os

# Must be set BEFORE config/app are imported anywhere.
os.environ.setdefault("MOCK_MODE", "true")
os.environ.setdefault("HEALTHCHECK_ENABLED", "true")
os.environ.setdefault("AUTO_ALERT_ON_BREACH", "false")  # keep scans side-effect free
os.environ.setdefault("ALERT_RECIPIENTS", "test@example.com")
