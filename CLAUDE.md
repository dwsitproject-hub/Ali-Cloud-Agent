# CLAUDE.md - project guide for Claude Code

## What this is
Cloud Agent Monitoring: a Flask app that watches Alibaba Cloud ECS instances.
Every few minutes it (1) pulls CPU/memory/disk metrics from Alibaba CloudMonitor,
(2) runs TCP/HTTP service health probes, and (3) emails an alert via Alibaba
DirectMail when a metric threshold is breached or a service is down. A web
dashboard shows everything grouped by environment. Access is gated by SSO
(Downstream Hub); the monitored inventory and all history live in PostgreSQL.

## Layout
- `Backend/` - all Python. Flat modules (run with `Backend/` on `sys.path`).
- `Frontend/` - **standalone static frontend** (plain HTML/JS under nginx, NOT
  served by Flask). `public/` has `index.html` (dashboard), `instances.html`,
  `login.html`, `config.js.template` (runtime `API_BASE`); plus `nginx.conf`,
  `Dockerfile`, `40-config-js.sh`. It calls the backend API cross-origin with
  `credentials:'include'`; the backend is API-only.
- `Docs/` - docs (incl. `SSO-TARGET-APP-INTEGRATION.md`). `Assets/` - images.
- `deploy/` - systemd unit + per-server env templates (`env.app.example`,
  `env.db.example`, `env.fe.example`). Compose (repo root): `docker-compose.yml`
  = single-box (fe+app+db); `docker-compose.db.yml` = Postgres on the DB server;
  `docker-compose.app.yml` = backend API on the BE server (remote DB);
  `docker-compose.fe.yml` = static frontend on the FE server. The prod/staging
  topology is 3 servers (FE/BE/DB): frontend→FE, backend API→BE, Postgres→DB.
  FE/BE/DB are *also* what it monitors. See README "Production topology".
- `.env` lives at the **repo root**; `Backend/config.py` loads it via
  `load_dotenv(BASE_DIR.parent / ".env")`.

## Architecture (two "agents" + scheduler + DB + auth + UI)
- `Backend/app.py` - `create_app()` factory: **API-only** (no templates/pages).
  Registers blueprints (`main`, `instances`, `auth`), inits SQLAlchemy +
  Flask-Migrate, Flask-Login, CSRF, **Flask-CORS** (credentialed, pinned to
  `CORS_ORIGINS`), a `before_request` login guard (lets `OPTIONS` preflights
  through), and `validate_startup()` fail-fast. `_bootstrap(app)` starts the
  scheduler.
- `Backend/agents/agent1_scanner.py` - Agent 1: CloudMonitor `DescribeMetricLast`;
  reads the enabled instance inventory from the DB at scan time. Mock mode synthesises.
- `Backend/healthcheck.py` - service probes (`tcp`/`http`); also builds checks
  from the DB inventory per cycle.
- `Backend/agents/agent2_alerter.py` - Agent 2: builds + sends the HTML alert via
  DirectMail `SingleSendMail`.
- `Backend/scheduler.py` - APScheduler runs one cycle every `SCAN_INTERVAL_MINUTES`;
  the background job runs inside an `app.app_context()`. Auto-alerts on breach/outage.
- `Backend/cloud_client.py` - signed Alibaba RPC client (product-agnostic).
- `Backend/db.py` - shared SQLAlchemy instance. `Backend/models.py` - User,
  InstanceGroup, Instance, ScanRun, MetricResult, ServiceResult, Alert (+
  `enabled_instance_dicts()`).
- `Backend/state.py` - **DB-backed** persistence: `set_last_scan`/`set_last_alert`
  write rows; `snapshot()` reconstructs the legacy dict shapes for `/api/status`.
- `Backend/instances_api.py` - instance CRUD API + `/api/instances/meta` (roles+groups).
- `Backend/auth/sso.py` - DWS Hub SSO via **OIDC (Authlib, Auth Code + PKCE,
  public client)**: `/auth/oidc/login` + `/auth/oidc/callback`; plus
  `/auth/dev-login`, `/auth/info` (public), `/auth/login`+`/auth/logout` (redirect
  to `FRONTEND_URL`). `init_oauth(app)` registers the client.
- `Backend/config.py` - env settings + `METRIC_TEMPLATES`, `SERVICE_CHECKS_BY_ROLE`,
  `INSTANCE_GROUPS` (seed only), `build_metrics()`/`build_service_checks()`.
- `Backend/seed.py` - `flask seed` (idempotent: groups/instances/dev-user).
- `Backend/wsgi.py` - gunicorn entry (`--workers 1`; inserts Backend/ on path).

## HTTP API
**API-only backend** (UI is the separate static frontend). GET `/api/status` |
POST `/api/scan` (`?auto_alert=true`) | POST `/api/send` | GET `/api/csrf` |
GET `/api/health` (liveness, public) | GET `/api/ready` (readiness: DB + scan
freshness, public) | GET/POST `/api/instances`, GET `/api/instances/meta`,
PATCH/DELETE `/api/instances/<id>` | GET `/auth/oidc/login` +
`/auth/oidc/callback` (OIDC), GET `/auth/info` (public), `/auth/dev-login`,
`/auth/login` (→ FE login), `/auth/logout`.
All routes require login except `/api/health`, `/api/ready`, and `/auth/*`.
`/api/*` returns 401 JSON when anonymous. Login/SSO redirect to `FRONTEND_URL`.

## Run / dev
- Easiest: `docker compose up --build` = fe (**:8080**) + app API (**:5000**) +
  db (**:5440**). Open **http://localhost:8080**.
- Backend on host instead: `docker compose up -d db`, then with `Backend/`
  importable (`$env:PYTHONPATH="Backend"; $env:FLASK_APP="app"`):
  `python -m flask db upgrade` then `python -m flask seed`, then
  `python Backend/app.py` (API on :5000); `docker compose up -d --build fe` for UI.
- Frontend→backend is cross-origin: backend needs `CORS_ORIGINS`/`FRONTEND_URL`
  set to the FE origin; the FE's `API_BASE` (config.js) points at the backend.
- `MOCK_MODE=true` -> synthetic metrics, no Alibaba calls, no email. Also flips
  `DEV_LOGIN_ENABLED` on by default, so `/auth/dev-login` works without the Hub.
- Live needs `.env` with `ALIBABA_ACCESS_KEY_ID/SECRET`, regions, DirectMail
  sender, `DATABASE_URL`, `SECRET_KEY`, and the DWS Hub OIDC settings
  (`OIDC_DISCOVERY_URL`, `OIDC_CLIENT_ID`, `OIDC_REDIRECT_URI`).
- Tests: `MOCK_MODE=true python -m pytest Backend/tests` (needs Postgres up).
  Fixtures: `client` (authed via dev-login), `anon_client`.

## Inventory model (now in PostgreSQL)
- Instances/groups live in the DB; edit them via the **register page**
  (frontend `instances.html`) or the CRUD API. The agents read enabled instances
  each scan, so changes apply on the next cycle.
- `config.INSTANCE_GROUPS` is **seed data only** (`flask seed`), not the live source.
- `METRIC_TEMPLATES` (CPU no-agent; Memory/Disk need the CloudMonitor agent) and
  `SERVICE_CHECKS_BY_ROLE` (default TCP/HTTP checks per role) remain code; the
  `build_metrics`/`build_service_checks` helpers expand instance dicts into the
  flat lists the agents consume. `host=""` skips service probes for that box.

## Conventions / gotchas
- Keep the scheduler to ONE process (single APScheduler). State is now in
  Postgres, but the scheduler must still be single; run gunicorn `--workers 1`.
- The scheduler's background scan + the immediate first scan run inside an
  app context (see `scheduler._scan_job`) — required for DB access.
- `/api/scan` returns the live in-memory scan dict; `state.set_last_scan`
  persists it as a side effect; `snapshot()` rebuilds the same shape from DB for
  `/api/status`. Keep these JSON shapes stable — `Backend/tests/test_contract.py`
  guards them.
- SSO: OIDC Authorization Code + PKCE (Authlib). SP-initiated: `/auth/oidc/login`
  → Hub authorize → `/auth/oidc/callback` exchanges the code and verifies the
  id_token via the Hub JWKS. Public client (no secret). Session cookies:
  `SameSite=None; Secure` for split-origin HTTPS, or `Lax`/insecure for
  single-origin HTTP staging. `OIDC_REDIRECT_URI` must be on the Hub allowlist.
- Never commit `.env` (gitignored). Region: ECS metrics `ap-southeast-5`;
  DirectMail `ap-southeast-1` (no Jakarta endpoint).
- Migrations live in `Backend/migrations/` (pinned via `Migrate(directory=...)`).
- Verify: `python -m py_compile` modules + run the pytest suite in mock mode.

## Ideas / backlog
- Docker container visibility (SSH `docker ps` or Docker API tier).
- Per-instance thresholds; historical metric charts; scan-history pruning.
- Per-channel alerts (Slack/webhook); alert de-duplication.
- Single-use SSO token enforcement (jti/replay table) beyond the 60s exp window.
