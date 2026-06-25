# CLAUDE.md - project guide for Claude Code

## What this is
Cloud Agent Monitoring: a small Flask app that watches Alibaba Cloud ECS
instances. Every few minutes it (1) pulls CPU/memory/disk metrics from Alibaba
CloudMonitor, (2) runs TCP/HTTP service health probes, and (3) emails an alert
via Alibaba DirectMail when a metric threshold is breached or a service is down.
A web dashboard shows everything grouped by environment.

## Architecture (two "agents" + scheduler + UI)
- `agents/agent1_scanner.py` - Agent 1: CloudMonitor `DescribeMetricLast` calls,
  parses Datapoints, evaluates thresholds. Mock mode synthesises values.
- `healthcheck.py` - service probes: `tcp` (socket connect) and `http` (GET +
  status/latency). Mock mode synthesises up/down.
- `agents/agent2_alerter.py` - Agent 2: builds the HTML alert and sends via
  DirectMail `SingleSendMail`.
- `scheduler.py` - APScheduler runs one cycle (metrics + health) every
  `SCAN_INTERVAL_MINUTES`; auto-triggers Agent 2 on any breach/outage.
- `cloud_client.py` - signed Alibaba RPC client (CommonRequest), product-agnostic.
- `state.py` - in-memory last scan / last alert / history (single process).
- `app.py` - Flask routes + startup; `wsgi.py` - gunicorn entry (1 worker).
- `config.py` - ALL configuration: instances, groups, thresholds, service checks.
- `templates/dashboard.html` - single-file UI (vanilla JS, polls /api/status).

## HTTP API
GET `/` dashboard | GET `/api/status` | POST `/api/scan` (`?auto_alert=true`) |
POST `/api/send` | GET `/api/health`.

## Run / dev
- Windows: double-click `start.bat`. Or: `python app.py` (http://127.0.0.1:5000).
- `MOCK_MODE=true` in `.env` -> synthetic data, no Alibaba calls, no email sent.
  Use this for all local development/tests.
- Live needs `.env` with `ALIBABA_ACCESS_KEY_ID/SECRET`, `CLOUDMONITOR_REGION`
  (ap-southeast-5), `DIRECTMAIL_REGION` (ap-southeast-1), DirectMail sender.

## Config model (edit config.py)
- `INSTANCE_GROUPS`: list of {group, instances:[{id,name,role,host}]}. 9 ECS
  instances across Production/Staging/Others. `host` = IP/DNS the monitor reaches
  for service probes (blank = skip). `role` selects default service checks.
- `METRIC_TEMPLATES`: per-instance metrics (CPU no-agent; Memory/Disk need the
  CloudMonitor agent installed on the box).
- `SERVICE_CHECKS_BY_ROLE`: default TCP/HTTP checks per role.
- `_build_metrics()` / `_build_service_checks()` expand these into flat lists
  (`METRICS`, `SERVICE_CHECKS`) consumed by the agents.

## Conventions / gotchas
- Keep the scheduler to ONE process (in-memory state + single scheduler). For
  multiple workers, move state to Redis/DB and run the scheduler separately.
- Never commit `.env` (gitignored). Secrets come only from env.
- Region: ECS metrics are in `ap-southeast-5`; DirectMail has no Jakarta
  endpoint, so it uses `ap-southeast-1`.
- Service probes confirm a port answers; they do NOT list docker containers.
  A docker-aware tier (SSH `docker ps` or Docker API) is a planned enhancement.
- Verify changes in mock mode: `MOCK_MODE=true python app.py` then hit the
  endpoints, or `python -m py_compile` the modules.

## Ideas / backlog
- Docker container visibility (SSH or Docker API tier).
- Per-instance thresholds; historical metric storage + charts.
- Per-channel alerts (Slack/webhook) alongside email; alert de-duplication.
- Auth on the dashboard before exposing it beyond localhost.
