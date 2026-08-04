"""Central configuration for Cloud Agent Monitoring.

Reads settings from environment (.env) and exposes the metric/service-check
builders the agents use to expand the DB-backed instance inventory:
  * build_metrics(instances)        - CloudMonitor metrics (CPU/mem/disk)
  * build_service_checks(instances) - per-instance TCP/HTTP probes

INSTANCE_GROUPS below is now only seed data for `flask seed`; the live inventory
lives in PostgreSQL and is edited via the register page.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# .env lives at the repo root (one level above Backend/). load_dotenv does not
# override variables already set in the environment.
load_dotenv(BASE_DIR.parent / ".env")


def _clean(raw: str) -> str:
    """Strip an inline `# comment` and surrounding whitespace from an env value.

    Docker Compose's ``env_file`` does NOT strip trailing comments, so a line like
    ``SMTP_SECURE=true   # use SSL`` arrives as the literal string
    ``"true   # use SSL"``. Without this, such a value silently parses as False —
    which is exactly how alert emails broke (STARTTLS attempted on an SSL-only
    port 465, raising SMTPNotSupportedError).
    """
    return raw.split("#", 1)[0].strip()


def _b(name: str, default: bool) -> bool:
    return _clean(os.getenv(name, str(default))).lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    """Integer env var, tolerant of inline comments and blanks."""
    try:
        return int(_clean(os.getenv(name, str(default))) or default)
    except ValueError:
        return default


def _host(name: str) -> str:
    """A monitored server's probe host (private IP/DNS) from the environment.

    Lets the FE/BE/DB hosts for each environment be set declaratively in the
    BE server's .env, so `flask seed` populates the inventory with real probe
    targets. Blank (unset) keeps the old behaviour: service probes are skipped
    for that box until a host is set (here or via the register page)."""
    return os.getenv(name, "").strip()


# --- Credentials / endpoints -------------------------------------------------
ACCESS_KEY_ID = os.getenv("ALIBABA_ACCESS_KEY_ID", "").strip()
ACCESS_KEY_SECRET = os.getenv("ALIBABA_ACCESS_KEY_SECRET", "").strip()
CLOUDMONITOR_REGION = os.getenv("CLOUDMONITOR_REGION", "cn-hangzhou").strip()
DIRECTMAIL_REGION = os.getenv("DIRECTMAIL_REGION", "cn-hangzhou").strip()

# --- Alert email transport ---------------------------------------------------
# Agent 2 sends via SMTP when SMTP_HOST is set, otherwise via Alibaba DirectMail.
DM_ACCOUNT_NAME = os.getenv("DM_ACCOUNT_NAME", "").strip()
DM_FROM_ALIAS = os.getenv("DM_FROM_ALIAS", "Cloud Monitor").strip()
ALERT_RECIPIENTS = [
    a.strip() for a in os.getenv("ALERT_RECIPIENTS", "").split(",") if a.strip()
]

# SMTP (used when SMTP_HOST is set). SMTP_SECURE=true -> implicit SSL (e.g. 465);
# false -> STARTTLS (e.g. 587). SMTP_REJECT_UNAUTHORIZED=false skips TLS cert
# verification (for self-signed internal mail servers).
SMTP_HOST = _clean(os.getenv("SMTP_HOST", ""))
SMTP_PORT = _int("SMTP_PORT", 587)
# Port 465 is implicit TLS by convention, so default SMTP_SECURE from the port.
SMTP_SECURE = _b("SMTP_SECURE", SMTP_PORT == 465)
SMTP_USER = _clean(os.getenv("SMTP_USER", ""))
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")   # verbatim — may contain '#' or spaces
SMTP_FROM = (_clean(os.getenv("SMTP_FROM", "")) or SMTP_USER)
SMTP_REJECT_UNAUTHORIZED = _b("SMTP_REJECT_UNAUTHORIZED", True)
# Opt-in escape hatch: allow AUTH over an unencrypted connection when the server
# offers no STARTTLS. Off by default — credentials must not travel in cleartext.
SMTP_ALLOW_INSECURE = _b("SMTP_ALLOW_INSECURE", False)


def smtp_use_ssl() -> bool:
    """Whether to use implicit SSL (SMTP_SSL) rather than STARTTLS.

    Port 465 is implicit-TLS-only and does NOT offer STARTTLS, so treat it as
    authoritative even if SMTP_SECURE was mis-set — attempting STARTTLS there
    raises SMTPNotSupportedError and no mail is ever delivered.
    """
    return SMTP_SECURE or SMTP_PORT == 465


def smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER)

# --- Behaviour ---------------------------------------------------------------
MOCK_MODE = _b("MOCK_MODE", True)
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "5"))
AUTO_ALERT_ON_BREACH = _b("AUTO_ALERT_ON_BREACH", True)
HEALTHCHECK_ENABLED = _b("HEALTHCHECK_ENABLED", True)
HEALTHCHECK_TIMEOUT = float(os.getenv("HEALTHCHECK_TIMEOUT", "5"))
FLASK_HOST = os.getenv("FLASK_HOST", "127.0.0.1")
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))
# Max metric queries / service probes to run concurrently per scan cycle. The
# work is network-bound, so parallelising keeps a cycle fast even with many
# instances or slow/dead hosts (which otherwise each wait out the timeout).
SCAN_CONCURRENCY = int(os.getenv("SCAN_CONCURRENCY", "8"))

# --- Alerting cadence (flap / flood control) ---------------------------------
# Auto-alerts fire on TRANSITIONS, not every cycle: a mail goes out when a NEW
# problem appears, when everything RECOVERS, or as a periodic reminder every
# ALERT_RENOTIFY_MINUTES while the same problem persists. This stops a stuck
# metric from emailing every SCAN_INTERVAL_MINUTES.
ALERT_RENOTIFY_MINUTES = int(os.getenv("ALERT_RENOTIFY_MINUTES", "60"))
ALERT_ON_RECOVERY = _b("ALERT_ON_RECOVERY", True)

# --- History retention -------------------------------------------------------
# Scan history is written every cycle and would otherwise grow without bound.
# Runs (and their child metric/service/alert rows) older than this are pruned
# after each cycle. 0 disables pruning (keep everything).
HISTORY_RETENTION_DAYS = int(os.getenv("HISTORY_RETENTION_DAYS", "30"))

# --- Database (PostgreSQL) ---------------------------------------------------
# Default points at the local docker-compose Postgres (see docker-compose.yml).
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://cloudagent:cloudagent@localhost:5440/cloudagent",
).strip()

# --- Auth ---------------------------------------------------------------------
# Flask session-signing key (OUR app's own secret; not from the Hub).
SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-change-me").strip()

# DWS Hub SSO via OpenID Connect. The Hub is an OAuth2/OIDC provider and this app
# is a PUBLIC client (Authorization Code + PKCE, no client secret). The discovery
# URL exposes the authorize/token/jwks endpoints; see auth/sso.py.
OIDC_DISCOVERY_URL = os.getenv("OIDC_DISCOVERY_URL", "").strip()  # .../.well-known/openid-configuration
OIDC_CLIENT_ID = os.getenv("OIDC_CLIENT_ID", "").strip()
# Our callback; must be in the Hub's registered redirect_uri allowlist.
OIDC_REDIRECT_URI = os.getenv("OIDC_REDIRECT_URI", "").strip()
OIDC_SCOPES = os.getenv("OIDC_SCOPES", "openid email profile").strip()

# Dev login bypass (no Hub needed). Defaults ON whenever MOCK_MODE is on.
DEV_LOGIN_ENABLED = _b("DEV_LOGIN_ENABLED", MOCK_MODE)
DEV_LOGIN_EMAIL = os.getenv("DEV_LOGIN_EMAIL", "dev@localhost").strip()


def oidc_configured() -> bool:
    return bool(OIDC_DISCOVERY_URL and OIDC_CLIENT_ID and OIDC_REDIRECT_URI)
# Cross-site SSO landing needs SameSite=None; Secure (HTTPS). Relax for local HTTP.
SESSION_COOKIE_SECURE = _b("SESSION_COOKIE_SECURE", not DEV_LOGIN_ENABLED)
SESSION_COOKIE_SAMESITE = os.getenv(
    "SESSION_COOKIE_SAMESITE", "Lax" if DEV_LOGIN_ENABLED else "None").strip()

# --- Standalone frontend (served separately; talks to this API cross-origin) --
# Where the static frontend is served from. Login/SSO/logout redirect here, and
# it is the default CORS origin. Default targets the local dev FE (nginx on 8080).
# A single origin (used for post-login redirects). Defensive: if someone
# comma-joins values (that's CORS_ORIGINS' job, not this), take the first.
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:8080").split(",")[0].strip().rstrip("/")
# Browser origins allowed to call this API with credentials (comma-separated).
# Defaults to FRONTEND_URL. Never use "*" with credentials.
CORS_ORIGINS = [
    o.strip().rstrip("/")
    for o in os.getenv("CORS_ORIGINS", FRONTEND_URL).split(",") if o.strip()
]


# --- What to monitor ---------------------------------------------------------
# Each instance: id, name, role (drives default service checks), and host (the
# IP/DNS the monitor connects to for service probes - fill these in to enable
# service health checks; leave "" to skip probes for that box).
#
# All instances live in CLOUDMONITOR_REGION (ap-southeast-5 / Jakarta).

# Each environment (Production / Staging) is three servers — Frontend, Backend,
# and DB — matching the real deployment topology. Set each server's probe host
# (private IP/DNS) in the BE server's .env via MONITOR_HOST_<ENV>_<ROLE>; blank
# leaves probes skipped for that box until a host is set.
INSTANCE_GROUPS = [
    {
        "group": "Production",
        "instances": [
            {"id": "i-k1a5irk321vnht0kec3j", "name": "DB Production", "role": "db", "host": _host("MONITOR_HOST_PROD_DB")},
            {"id": "i-k1ad3kyn8xfme3vsx78c", "name": "Frontend Production", "role": "frontend", "host": _host("MONITOR_HOST_PROD_FRONTEND")},
            {"id": "i-k1aenopo7x0qcfemnkye", "name": "Backend Production", "role": "backend", "host": _host("MONITOR_HOST_PROD_BACKEND")},
        ],
    },
    {
        "group": "Staging",
        "instances": [
            {"id": "i-k1ab5rh48e40enbqa7ii", "name": "DB Staging", "role": "db", "host": _host("MONITOR_HOST_STAGING_DB")},
            {"id": "i-k1a5ja5hi7ps6aa7x88r", "name": "Frontend Staging", "role": "frontend", "host": _host("MONITOR_HOST_STAGING_FRONTEND")},
            {"id": "i-k1a4m0oobaw170notm7p", "name": "Backend Staging", "role": "backend", "host": _host("MONITOR_HOST_STAGING_BACKEND")},
        ],
    },
    {
        "group": "Others",
        "instances": [
            {"id": "i-k1ab2zd81llhjxrx0k24", "name": "Cisadane Web", "role": "web", "host": ""},
            {"id": "i-k1ah9q8qki7rtnjtc0qo", "name": "Mail Server", "role": "mail", "host": ""},
            {"id": "i-k1aj5f6zvz0ve19tyd0s", "name": "CHR", "role": "router", "host": ""},
        ],
    },
]

# Per-instance metric set. Each template becomes one DescribeMetricLast call.
METRIC_TEMPLATES = [
    {
        # Hypervisor-level - works WITHOUT the CloudMonitor agent.
        "suffix": "cpu", "label": "CPU", "namespace": "acs_ecs_dashboard",
        "metric_name": "CPUUtilization", "period": "60", "stat": "Average",
        "threshold": 80.0, "comparison": ">", "unit": "%", "agent_required": False,
    },
    {
        "suffix": "mem", "label": "Memory", "namespace": "acs_ecs_dashboard",
        "metric_name": "memory_usedutilization", "period": "60", "stat": "Average",
        "threshold": 85.0, "comparison": ">", "unit": "%", "agent_required": True,
    },
    {
        "suffix": "disk", "label": "Disk", "namespace": "acs_ecs_dashboard",
        "metric_name": "diskusage_utilization", "period": "60", "stat": "Average",
        "threshold": 90.0, "comparison": ">", "unit": "%", "agent_required": True,
    },
]

# --- Service health checks ---------------------------------------------------
# Default service probes per ROLE. The monitor connects to the instance's `host`.
#   type "tcp"  -> connection succeeds on `port`
#   type "http" -> GET scheme://host:port`path` returns a status in `expect`
# These map to the containers/services each box runs (the docker service's
# published port). Refine ports/paths to match your actual setup.
SERVICE_CHECKS_BY_ROLE = {
    "db": [
        {"name": "MySQL", "type": "tcp", "port": 3306},
        {"name": "SSH", "type": "tcp", "port": 22},
    ],
    "frontend": [
        {"name": "HTTP", "type": "http", "scheme": "http", "port": 80, "path": "/", "expect": [200, 301, 302]},
        {"name": "HTTPS", "type": "http", "scheme": "https", "port": 443, "path": "/", "expect": [200, 301, 302]},
        {"name": "SSH", "type": "tcp", "port": 22},
    ],
    "backend": [
        {"name": "API", "type": "http", "scheme": "http", "port": 8080, "path": "/health", "expect": [200, 204]},
        {"name": "SSH", "type": "tcp", "port": 22},
    ],
    "web": [
        {"name": "HTTP", "type": "http", "scheme": "http", "port": 80, "path": "/", "expect": [200, 301, 302]},
        {"name": "HTTPS", "type": "http", "scheme": "https", "port": 443, "path": "/", "expect": [200, 301, 302]},
    ],
    "mail": [
        {"name": "SMTP", "type": "tcp", "port": 25},
        {"name": "Submission", "type": "tcp", "port": 587},
        {"name": "SMTPS", "type": "tcp", "port": 465},
        {"name": "IMAPS", "type": "tcp", "port": 993},
    ],
    "router": [
        {"name": "SSH", "type": "tcp", "port": 22},
        {"name": "API", "type": "tcp", "port": 8728},
        {"name": "Winbox", "type": "tcp", "port": 8291},
    ],
}


# The monitored inventory now lives in PostgreSQL (see models.py / the register
# page). The agents fetch enabled instances at scan time and expand them into
# the flat metric/service-check lists via the builders below. Each `instance` is
# a plain dict: {id, name, role, host, group}. INSTANCE_GROUPS above is retained
# only as seed data for `flask seed`.

def build_metrics(instances: list) -> list:
    """Expand instance dicts into per-metric scan descriptors (one per template)."""
    metrics = []
    for inst in instances:
        for tpl in METRIC_TEMPLATES:
            metrics.append({
                "key": f"{inst['id']}_{tpl['suffix']}",
                "label": tpl["label"],
                "group": inst.get("group", "Ungrouped"),
                "instance_id": inst["id"],
                "instance_name": inst["name"],
                "namespace": tpl["namespace"],
                "metric_name": tpl["metric_name"],
                "period": tpl["period"],
                "dimensions": f'[{{"instanceId":"{inst["id"]}"}}]',
                "stat": tpl["stat"],
                "threshold": tpl["threshold"],
                "comparison": tpl["comparison"],
                "unit": tpl["unit"],
                "agent_required": tpl["agent_required"],
            })
    return metrics


def build_service_checks(instances: list) -> list:
    """Expand instance dicts into per-service health-probe descriptors by role."""
    checks = []
    for inst in instances:
        host = (inst.get("host") or "").strip()
        for svc in SERVICE_CHECKS_BY_ROLE.get(inst.get("role", ""), []):
            checks.append({
                "key": f"{inst['id']}_{svc['name'].lower().replace(' ', '_')}",
                "group": inst.get("group", "Ungrouped"),
                "instance_id": inst["id"],
                "instance_name": inst["name"],
                "name": svc["name"],
                "type": svc["type"],
                "host": host,
                "port": svc.get("port"),
                "scheme": svc.get("scheme", "http"),
                "path": svc.get("path", "/"),
                "expect": svc.get("expect", [200]),
            })
    return checks


def credentials_present() -> bool:
    return bool(ACCESS_KEY_ID and ACCESS_KEY_SECRET)


_INSECURE_SECRET_KEY = "dev-insecure-change-me"


def validate_startup() -> tuple[list, list]:
    """Fail-fast configuration check. Returns ``(errors, warnings)``.

    Production requirements are only enforced when MOCK_MODE is off, so local
    dev/tests (which run in mock mode) are never blocked. ``errors`` are fatal
    (the app refuses to boot); ``warnings`` are logged but non-fatal.

    This closes the "silent fallback to mock" footgun: without these checks a
    live deployment with a missing/typo'd AccessKey would quietly synthesise
    fake metrics and send nothing, while appearing healthy.
    """
    errors: list = []
    warnings: list = []

    # SECRET_KEY must never be the shipped default, in any mode that isn't a
    # throwaway mock run (a weak signing key is a full session-forgery bypass).
    if not MOCK_MODE and (not SECRET_KEY or SECRET_KEY == _INSECURE_SECRET_KEY):
        errors.append(
            "SECRET_KEY must be set to a strong random value in production "
            '(python -c "import secrets; print(secrets.token_urlsafe(48))")')

    if MOCK_MODE:
        return errors, warnings

    if not credentials_present():
        errors.append(
            "ALIBABA_ACCESS_KEY_ID / ALIBABA_ACCESS_KEY_SECRET are required when "
            "MOCK_MODE=false (otherwise the app cannot reach CloudMonitor/DirectMail)")
    if not DM_ACCOUNT_NAME and not smtp_configured():
        errors.append(
            "An alert email transport is required when MOCK_MODE=false: set the "
            "SMTP_* vars (SMTP_HOST/SMTP_USER/…) or DM_ACCOUNT_NAME (DirectMail sender)")
    if not oidc_configured() and not DEV_LOGIN_ENABLED:
        errors.append(
            "OIDC_DISCOVERY_URL / OIDC_CLIENT_ID / OIDC_REDIRECT_URI (DWS Hub SSO) "
            "are required when DEV_LOGIN_ENABLED=false — no one could sign in otherwise")

    if DEV_LOGIN_ENABLED:
        warnings.append(
            "DEV_LOGIN_ENABLED=true in a live (non-mock) deployment — the "
            "no-password dev-login bypass is active; set it false in production")
    if not SESSION_COOKIE_SECURE:
        warnings.append(
            "SESSION_COOKIE_SECURE=false in a live deployment — session cookies "
            "will be transmitted over plain HTTP")
    if not ALERT_RECIPIENTS:
        warnings.append(
            "ALERT_RECIPIENTS is empty — breach/outage alerts will be composed "
            "but delivered to no one")

    return errors, warnings
