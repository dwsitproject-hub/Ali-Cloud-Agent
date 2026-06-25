"""Central configuration for Cloud Agent Monitoring.

Reads settings from environment (.env) and exposes:
  * METRICS  - CloudMonitor metrics Agent 1 scans (CPU/mem/disk per instance)
  * SERVICE_CHECKS - per-instance service health checks (TCP/HTTP probes)

Edit INSTANCE_GROUPS below to add/rename instances, set their `host` (IP or DNS
the monitor can reach) and `role` (drives the default service checks).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# .env lives at the repo root (one level above Backend/). load_dotenv does not
# override variables already set in the environment.
load_dotenv(BASE_DIR.parent / ".env")


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# --- Credentials / endpoints -------------------------------------------------
ACCESS_KEY_ID = os.getenv("ALIBABA_ACCESS_KEY_ID", "").strip()
ACCESS_KEY_SECRET = os.getenv("ALIBABA_ACCESS_KEY_SECRET", "").strip()
CLOUDMONITOR_REGION = os.getenv("CLOUDMONITOR_REGION", "cn-hangzhou").strip()
DIRECTMAIL_REGION = os.getenv("DIRECTMAIL_REGION", "cn-hangzhou").strip()

# --- DirectMail --------------------------------------------------------------
DM_ACCOUNT_NAME = os.getenv("DM_ACCOUNT_NAME", "").strip()
DM_FROM_ALIAS = os.getenv("DM_FROM_ALIAS", "Cloud Monitor").strip()
ALERT_RECIPIENTS = [
    a.strip() for a in os.getenv("ALERT_RECIPIENTS", "").split(",") if a.strip()
]

# --- Behaviour ---------------------------------------------------------------
MOCK_MODE = _b("MOCK_MODE", True)
SCAN_INTERVAL_MINUTES = int(os.getenv("SCAN_INTERVAL_MINUTES", "5"))
AUTO_ALERT_ON_BREACH = _b("AUTO_ALERT_ON_BREACH", True)
HEALTHCHECK_ENABLED = _b("HEALTHCHECK_ENABLED", True)
HEALTHCHECK_TIMEOUT = float(os.getenv("HEALTHCHECK_TIMEOUT", "5"))
FLASK_HOST = os.getenv("FLASK_HOST", "127.0.0.1")
FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))

# --- Database (PostgreSQL) ---------------------------------------------------
# Default points at the local docker-compose Postgres (see docker-compose.yml).
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://cloudagent:cloudagent@localhost:5440/cloudagent",
).strip()

# --- Auth / SSO (see Docs/SSO-TARGET-APP-INTEGRATION.md, used in Phase 7) -----
SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-change-me").strip()
SSO_TOKEN_SECRET = os.getenv("SSO_TOKEN_SECRET", "").strip()
SSO_TOKEN_LEEWAY = int(os.getenv("SSO_TOKEN_LEEWAY", "10"))
# Dev login bypass (no Hub needed). Defaults ON whenever MOCK_MODE is on.
DEV_LOGIN_ENABLED = _b("DEV_LOGIN_ENABLED", MOCK_MODE)
DEV_LOGIN_EMAIL = os.getenv("DEV_LOGIN_EMAIL", "dev@localhost").strip()
# Cross-site SSO landing needs SameSite=None; Secure (HTTPS). Relax for local HTTP.
SESSION_COOKIE_SECURE = _b("SESSION_COOKIE_SECURE", not DEV_LOGIN_ENABLED)
SESSION_COOKIE_SAMESITE = os.getenv(
    "SESSION_COOKIE_SAMESITE", "Lax" if DEV_LOGIN_ENABLED else "None").strip()


# --- What to monitor ---------------------------------------------------------
# Each instance: id, name, role (drives default service checks), and host (the
# IP/DNS the monitor connects to for service probes - fill these in to enable
# service health checks; leave "" to skip probes for that box).
#
# All instances live in CLOUDMONITOR_REGION (ap-southeast-5 / Jakarta).

INSTANCE_GROUPS = [
    {
        "group": "Production",
        "instances": [
            {"id": "i-k1a5irk321vnht0kec3j", "name": "DB Production", "role": "db", "host": ""},
            {"id": "i-k1ad3kyn8xfme3vsx78c", "name": "Frontend Production", "role": "frontend", "host": ""},
            {"id": "i-k1aenopo7x0qcfemnkye", "name": "Backend Production", "role": "backend", "host": ""},
        ],
    },
    {
        "group": "Staging",
        "instances": [
            {"id": "i-k1ab5rh48e40enbqa7ii", "name": "DB Staging", "role": "db", "host": ""},
            {"id": "i-k1a5ja5hi7ps6aa7x88r", "name": "Frontend Staging", "role": "frontend", "host": ""},
            {"id": "i-k1a4m0oobaw170notm7p", "name": "Backend Staging", "role": "backend", "host": ""},
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


def _build_metrics() -> list:
    metrics = []
    for grp in INSTANCE_GROUPS:
        for inst in grp["instances"]:
            for tpl in METRIC_TEMPLATES:
                metrics.append({
                    "key": f"{inst['id']}_{tpl['suffix']}",
                    "label": tpl["label"],
                    "group": grp["group"],
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


def _build_service_checks() -> list:
    checks = []
    for grp in INSTANCE_GROUPS:
        for inst in grp["instances"]:
            host = (inst.get("host") or "").strip()
            for svc in SERVICE_CHECKS_BY_ROLE.get(inst.get("role", ""), []):
                checks.append({
                    "key": f"{inst['id']}_{svc['name'].lower().replace(' ', '_')}",
                    "group": grp["group"],
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


def load_metrics() -> list:
    override = BASE_DIR / "metrics.json"
    if override.exists():
        return json.loads(override.read_text(encoding="utf-8"))
    return _build_metrics()


METRICS = load_metrics()
SERVICE_CHECKS = _build_service_checks()


def credentials_present() -> bool:
    return bool(ACCESS_KEY_ID and ACCESS_KEY_SECRET)
