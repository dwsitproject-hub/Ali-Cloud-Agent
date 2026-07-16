# Cloud Agent Monitoring

A two-agent cloud monitoring service with a live dashboard.

- **Agent 1 (scanner)** queries the latest value of each configured metric from
  **Alibaba CloudMonitor** (`DescribeMetricLast`) and checks it against an alert
  threshold.
- **Agent 2 (alerter)** composes an HTML alert and sends it through **Alibaba
  DirectMail** (`SingleSendMail`).
- A **scheduler** runs Agent 1 every 5 minutes and **auto-triggers Agent 2** when
  any threshold is breached.
- A **dashboard** shows the latest scan, lets you scan on demand, and has a
  **Send** button to fire the alert email manually.
- **PostgreSQL** stores the monitored inventory, scan/alert history, and users.
- **SSO login** (Downstream Hub) gates access; a **register page** lets you
  add/manage monitored instances without editing code.

**Architecture — two tiers:** a **backend API** (`Backend/`, Flask/gunicorn —
agents, scheduler, JSON API, auth) and a **standalone static frontend**
(`Frontend/`, plain HTML/JS served by nginx) that calls the API cross-origin.
They deploy independently: the backend on the BE server, the frontend on the FE
server, PostgreSQL on the DB server. Code is organized into `Backend/` (Python),
`Frontend/` (static site), `Docs/`, and `Assets/`.

The pieces this build wired up:

1. **Real CloudMonitor API** — `Backend/agents/agent1_scanner.py` makes signed
   `DescribeMetricLast` calls and parses the `Datapoints` JSON.
2. **Real email sender** — `Backend/agents/agent2_alerter.py` sends via DirectMail.
3. **Scheduling** — `Backend/scheduler.py` runs the scan on a 5-minute cron and
   auto-sends on breach.
4. **PostgreSQL + migrations** — inventory and history persist across restarts.
5. **SSO + dev login** — `Backend/auth/sso.py` verifies the Hub's HS256 JWT.

## Quick start (mock mode — no credentials needed)

Requires Docker. The single-box compose runs all three tiers together —
frontend (nginx), backend API, and Postgres:

```bash
cp .env.example .env                 # MOCK_MODE=true, DEV_LOGIN_ENABLED=true
docker compose up --build            # fe :8080, app :5000, db :5440
# open http://localhost:8080  (click "Continue with dev login")
```

The frontend at **:8080** calls the backend API at **:5000** (CORS-allowed for
the `localhost:8080` origin). The backend applies migrations + seed on start.

To run the backend on the host instead (Postgres still in Docker):

```bash
pip install -r Backend/requirements.txt
docker compose up -d db              # PostgreSQL on host port 5440
export PYTHONPATH=Backend FLASK_APP=app   # PowerShell: $env:PYTHONPATH="Backend"; $env:FLASK_APP="app"
python -m flask db upgrade && python -m flask seed
python Backend/app.py                # backend API on http://127.0.0.1:5000
docker compose up -d --build fe      # frontend on http://localhost:8080
```

In mock mode the scanner generates synthetic metric values (some breach, so you
can see alerts) and Agent 2 renders the email but does **not** transmit it. The
dashboard is behind login — with `DEV_LOGIN_ENABLED=true` (default in mock mode)
click **Continue with dev login** at `/auth/login`.

## Going live

1. In the Alibaba Cloud console, create a RAM user with permissions
   `cms:QueryMetricLast` and `Dm:SingleSendMail`, and generate an AccessKey.
2. In **DirectMail**, verify a sender domain and create a sender address.
3. Edit `.env`:

   ```ini
   MOCK_MODE=false
   ALIBABA_ACCESS_KEY_ID=LTAI...
   ALIBABA_ACCESS_KEY_SECRET=********
   CLOUDMONITOR_REGION=ap-southeast-5
   DIRECTMAIL_REGION=ap-southeast-1
   DM_ACCOUNT_NAME=alert@mail.yourdomain.com
   ALERT_RECIPIENTS=you@example.com,ops@example.com

   # Database + auth (required)
   DATABASE_URL=postgresql+psycopg2://user:pass@db-host:5432/cloudagent
   SECRET_KEY=<random; python -c "import secrets;print(secrets.token_urlsafe(48))">
   SSO_TOKEN_SECRET=<shared secret from the Hub operator>
   DEV_LOGIN_ENABLED=false       # real SSO only in production
   SESSION_COOKIE_SAMESITE=None  # cross-site SSO landing (needs HTTPS)
   SESSION_COOKIE_SECURE=true
   ```

4. `python Backend/app.py`. The app will now make real API calls and require SSO.

Credentials are read from the environment only — nothing is hard-coded, and
`.env` is git-ignored. See `Docs/SSO-TARGET-APP-INTEGRATION.md` for the SSO
contract (the Hub POSTs an HS256 JWT to `/auth/hub`).

## Configuring what to monitor

The monitored inventory lives in **PostgreSQL**. Use the **register page** at
`instances.html` (linked from the dashboard) to add an instance — its ECS id, display
name, `role` (selects the default service checks), `host` (IP/DNS for probes;
blank skips them), and group — or enable/disable/delete existing ones. Changes
are picked up on the next scan; no code edit or restart needed.

`flask seed` loads an initial 9 instances from `config.INSTANCE_GROUPS` (seed data
only). Metric definitions/thresholds (`METRIC_TEMPLATES`) and the per-role service
checks (`SERVICE_CHECKS_BY_ROLE`) remain in `Backend/config.py`. Namespaces and
metric names come from CloudMonitor's *Appendix 1: Metrics*.

## HTTP API

The backend is **API-only** (JSON + auth); the UI is the separate frontend. All
routes require a login session except `/api/health`, `/api/ready`, and `/auth/*`.
Anonymous `/api/*` calls return `401` JSON. Cross-origin calls from the frontend
are allowed via CORS (`CORS_ORIGINS`) with credentials.

| Method      | Path                   | Purpose                                       |
|-------------|------------------------|-----------------------------------------------|
| GET         | `/api/status`          | Latest scan + last alert + history (JSON)     |
| POST        | `/api/scan`            | Run Agent 1 now (`?auto_alert=true`)          |
| POST        | `/api/send`            | Run Agent 2 against the latest scan           |
| GET         | `/api/csrf`            | CSRF token for the SPA's state-changing calls |
| GET         | `/api/health`          | Liveness probe (public)                       |
| GET         | `/api/ready`           | Readiness: DB + scan freshness (public)       |
| GET/POST    | `/api/instances`       | List / create instances                       |
| GET         | `/api/instances/meta`  | Roles + groups (for the frontend form)        |
| PATCH/DELETE| `/api/instances/<id>`  | Toggle enabled / edit / delete                |
| POST        | `/auth/hub`            | SSO entry — Hub posts an HS256 JWT            |
| GET         | `/auth/info`           | Whether dev-login is enabled (public)         |
| GET/POST    | `/auth/dev-login`      | Local dev login (gated by `DEV_LOGIN_ENABLED`)|
| GET         | `/auth/login`,`/logout`| Redirect to the frontend login page / sign out|

The frontend pages (served by nginx, not the backend): `index.html` (dashboard),
`instances.html` (register/manage), `login.html`.

## Files

```
Backend/                  BACKEND API (Flask/gunicorn)
  app.py                  create_app() factory, blueprints, CORS, login guard
  scheduler.py            APScheduler: 5-min scan + auto-alert (app-context job)
  alerting.py             transition-based auto-alert decision (flap control)
  config.py               Env config + metric templates / per-role checks (+ seed data)
  cloud_client.py         Signed Alibaba RPC client (CommonRequest)
  db.py / models.py       SQLAlchemy instance + ORM models
  state.py                DB-backed persist + snapshot() + history pruning
  seed.py                 `flask seed` (idempotent inventory + dev user)
  instances_api.py        Instance CRUD API (+ /meta)
  auth/sso.py             SSO (/auth/hub) + dev login + session + /auth/info
  agents/agent1_scanner.py   CloudMonitor scan + threshold evaluation
  agents/agent2_alerter.py   DirectMail email composition + send
  migrations/             Alembic migrations
  tests/                  pytest (contract, instances CRUD, auth, alerting)
Frontend/                 STANDALONE FRONTEND (static site under nginx)
  public/                 index.html, instances.html, login.html, config.js.template
  nginx.conf, Dockerfile, 40-config-js.sh   (runtime API_BASE injection)
docker-compose.yml        Single-box: fe + app + db
docker-compose.{db,app,fe}.yml   Per-server deploys (DB / BE / FE)
```

## Enable memory & disk metrics (install the CloudMonitor agent)

`CPUUtilization` is read at the hypervisor and needs no agent. **Memory and disk
usage are OS-level metrics** — CloudMonitor only reports them once the
**CloudMonitor agent** is running on the instance. Until then, those two cards
show "no datapoints".

Target instance: `i-k1a5ja5hi7ps6aa7x88r` (region `ap-southeast-5`, Linux).

### Option A — Console (gets the exact, region-correct command)

1. Open the [CloudMonitor console](https://cloudmonitor.console.aliyun.com) →
   **Host Monitoring**.
2. Click **Host and agent operations** (top of the host list) → **Manual install**.
3. **Select region: `ap-southeast-5` (Jakarta)** — the command is region-specific,
   so this matters.
4. Copy the generated installation command, then SSH into the instance as root and
   run it. It looks like this (the host/version in the URL come from the console
   for your region — don't hand-edit them):

   ```bash
   # run as root on the ECS instance
   REGIONID=ap-southeast-5 \
   wget -qO- https://cms-agent-<region>.oss-<region>.aliyuncs.com/cms-go-agent/cms_install.sh | bash
   ```

### Option B — Cloud Assistant (no SSH, install remotely)

ECS console → select the instance → **Maintenance & Diagnostics → Send Command**
(Cloud Assistant), and run the install command above. Useful for fleets.

### Verify

- On the instance: `systemctl status cms_agent` (or `ps -ef | grep cms`) should
  show it running.
- In the console, Host Monitoring lists the host as **Online** within ~1 minute.
- OS metrics are collected every 15s. After a minute, re-run a scan
  (`POST /api/scan` or the dashboard's **Scan now**) and the **Memory** and
  **Disk** cards will populate.

Notes:
- The agent auto-starts on boot and uses the instance's own identity — no extra
  AccessKey needed on the host.
- To auto-install on future instances, enable **Auto-install CloudMonitor for new
  ECS** in the Host Monitoring console.

## Production topology (3 servers: FE / BE / DB)

Staging and production each run on **three separate servers** — Frontend, Backend,
Database — and the app now maps onto all three: the **static frontend on the FE
server**, the **backend API on the BE server**, **PostgreSQL on the DB server**.
(The FE/BE/DB servers are *also* things the app monitors — unrelated to where its
own components deploy.)

```
   browser ─┬─ loads UI ──▶ ┌─────────────┐
            │                │  FE server  │  nginx: static frontend
            └─ API calls ─┐  └─────────────┘
       (CORS, credentials)│
                          ▼
        ┌─────────────┐   metrics (HTTPS)    Alibaba CloudMonitor / DirectMail
        │  BE server  │ ───────────────────▶ (public API, needs creds)
        │ api+gunicorn │
        │ (scheduler)  │ ──probe──┐
        └──────┬───────┘          ├─▶ FE server   (HTTP/HTTPS/SSH)
               │ SQL 5432         ├─▶ BE server   (API/SSH — itself)
               ▼                  └─▶ DB server   (DB port/SSH)
        ┌─────────────┐
        │  DB server  │  PostgreSQL (monitor's inventory + history + users)
        └─────────────┘
```

> **Staging deployment:** step-by-step runbook (per-server commands, TLS setup,
> and DWS Hub SSO registration) in [Docs/DEPLOY-STAGING.md](Docs/DEPLOY-STAGING.md).

Serve **both** the FE and BE over **HTTPS**. If they share a parent domain
(e.g. `monitor.example.com` + `monitor-api.example.com`) the SSO session cookie
works as same-site; otherwise it relies on `SameSite=None; Secure` (already the
production default). Set `CORS_ORIGINS`/`FRONTEND_URL` on the backend to the FE's
public URL, and `API_BASE` on the frontend to the BE's public URL.

### 1. DB server — PostgreSQL

```bash
cp deploy/env.db.example .env         # set a strong POSTGRES_PASSWORD
docker compose -f docker-compose.db.yml up -d
```

Lock inbound `5432` to the **BE server's private IP** in the security group; don't
expose it publicly. Keep this database separate from any monitored production DB.

### 2. BE server — the backend API

```bash
cp deploy/env.app.example .env        # creds, secrets, DATABASE_URL, FRONTEND_URL, CORS_ORIGINS
# DATABASE_URL -> the DB server's PRIVATE IP, e.g.
#   postgresql+psycopg2://cloudagent:PASS@10.0.0.10:5432/cloudagent
docker compose -f docker-compose.app.yml up -d --build
```

Runs `flask db upgrade` + `flask seed`, then serves under gunicorn
(`--workers 1 --threads 4` → single scheduler, concurrent web). Front it with
HTTPS. Point uptime checks at `GET /api/ready`.

### 3. FE server — the frontend

```bash
cp deploy/env.fe.example .env         # set API_BASE to the BE's public URL
docker compose -f docker-compose.fe.yml up -d --build
```

nginx serves the static dashboard; `API_BASE` is baked into `config.js` at start.
Front it with HTTPS. Register each monitored box's probe `host` on the register
page, or seed declaratively from the BE server's `.env`
(`MONITOR_HOST_PROD_FRONTEND`, `…_BACKEND`, `…_DB`, and the `STAGING_` trio);
`host` only drives the TCP/HTTP service probes, metrics scan by ECS id regardless.

### Network / security-group matrix

| From → To            | Port(s)                        | Why                          |
|----------------------|--------------------------------|------------------------------|
| browser → FE server  | 443                            | load the dashboard UI        |
| browser → BE server  | 443                            | API calls (CORS, credentials)|
| BE → DB server       | 5432                           | app's own PostgreSQL         |
| BE → Alibaba (egress)| 443                            | CloudMonitor + DirectMail    |
| BE → FE server       | 80, 443, 22                    | `frontend` role probes       |
| BE → BE server       | 8080 (/health), 22             | `backend` role probes        |
| BE → DB server       | DB port (3306/5432), 22        | `db` role probes             |

The default `db` role probes **MySQL 3306** — if your DB servers run PostgreSQL,
change that port in `SERVICE_CHECKS_BY_ROLE` (`Backend/config.py`) to `5432`.

### Operational caveat

The monitor's state DB lives on the DB server, so a **DB-server outage blinds the
monitor** — it can't read its inventory to scan or record/send the "DB down"
alert. Keep the monitor's database separate from monitored production databases;
if you need the monitor to survive a DB-server outage, host its Postgres on the
BE server instead (bundle the `db` service into `docker-compose.app.yml`).

## Run as a service on ECS (systemd)

A unit file is provided at `deploy/cloud-agent-monitoring.service`. It runs the
app as a dedicated user, restarts on crash, and starts on boot. Because the
scheduler runs in-process, keep it to a **single process** (this unit does).

```bash
# 1. Put the project at /opt and create a service user
sudo mkdir -p /opt/cloud-agent-monitoring
sudo cp -r ./* /opt/cloud-agent-monitoring/        # include .env (your real one)
sudo useradd --system --no-create-home --shell /usr/sbin/nologin cloudagent

# 2. Virtualenv + dependencies
cd /opt/cloud-agent-monitoring
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r Backend/requirements.txt

# 2b. Provision the database (point DATABASE_URL at your Postgres first)
sudo PYTHONPATH=Backend FLASK_APP=app .venv/bin/python -m flask db upgrade
sudo PYTHONPATH=Backend FLASK_APP=app .venv/bin/python -m flask seed

# 3. Lock down the secret file and hand ownership to the service user
sudo chown -R cloudagent:cloudagent /opt/cloud-agent-monitoring
sudo chmod 600 /opt/cloud-agent-monitoring/.env

# 4. Install and start the service
sudo cp deploy/cloud-agent-monitoring.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cloud-agent-monitoring

# 5. Check it
systemctl status cloud-agent-monitoring
journalctl -u cloud-agent-monitoring -f      # live logs (scans every 5 min)
```

Manage it:

```bash
sudo systemctl restart cloud-agent-monitoring   # e.g. after rotating the AccessKey in .env
sudo systemctl stop cloud-agent-monitoring
```

### Reaching the dashboard

By default the app binds `127.0.0.1:5000` (local only — safest). To view it
from your laptop, the recommended option is an SSH tunnel:

```bash
ssh -L 5000:127.0.0.1:5000 root@<ecs-public-ip>     # then open http://localhost:5000
```

If you instead want it reachable directly, set `FLASK_HOST=0.0.0.0` in `.env`,
open the port in the instance's **Security Group**, and ideally put nginx +
HTTPS in front rather than exposing Flask's built-in server. For higher traffic,
swap `ExecStart` to gunicorn with **one** worker:

```ini
ExecStart=/opt/cloud-agent-monitoring/.venv/bin/gunicorn --workers 1 --threads 4 --chdir Backend --bind 127.0.0.1:5000 wsgi:app
```

(`Backend/wsgi.py` runs the bootstrap so the scheduler still starts. More than one
worker would duplicate the scheduler and double every scan/alert.)

## Production notes

- State and history live in **PostgreSQL**, but the **scheduler must still be a
  single process** (one APScheduler). Keep gunicorn at `--workers 1`; use
  `--threads N` for web concurrency so a long scan can't block dashboard polling.
- Provide a strong `SECRET_KEY` and the Hub's `SSO_TOKEN_SECRET`; set
  `DEV_LOGIN_ENABLED=false`, `SESSION_COOKIE_SECURE=true`, and serve over HTTPS so
  the cross-site SSO landing (`SameSite=None`) works. When `MOCK_MODE=false` the
  app **fails fast at boot** if credentials, `DM_ACCOUNT_NAME`, `SSO_TOKEN_SECRET`,
  or a non-default `SECRET_KEY` are missing — it will not silently run in mock mode.
- Run DB migrations (`flask db upgrade`) on deploy. Scan history is pruned after
  every cycle per `HISTORY_RETENTION_DAYS` (default 30; set 0 to keep everything).
- **Alerting is transition-based** (flood control): a mail goes out on a new
  breach/outage, on full recovery, or as a reminder every `ALERT_RENOTIFY_MINUTES`
  — not once per cycle for a stuck metric.
- **Health endpoints:** `GET /api/health` is liveness (always 200);
  `GET /api/ready` is readiness (checks DB, reports last-scan staleness, 503 if
  the DB is unreachable) — point container/uptime checks at `/api/ready`.
- DirectMail throttles and requires a warmed, verified domain; check send quotas.

## Service health checks (Datadog-style)

Alongside CPU/memory/disk, the monitor probes each server's **services** every
cycle and shows up/down + latency per server, factoring service outages into the
same email alerts.

How it works: network probes from the monitor host.
- `tcp`  - opens a socket to `host:port` (is the service accepting connections?)
- `http` - GETs `scheme://host:port/path` and checks the status code + latency

These confirm a service (typically a docker container's published port) is up and
answering. Probes run from wherever the app runs, so that host must be able to
reach each server's ports (same VPC private IPs, or public IP + security group).

### Configure (register page + `SERVICE_CHECKS_BY_ROLE`)

Each instance has a `role` and a `host`, set on the `/instances` register page.
Set `host` to the IP/DNS the monitor can reach; leave it blank to skip that server
(its services show as "unconfigured", never "down"). The `role` selects the
default checks from `SERVICE_CHECKS_BY_ROLE` in `Backend/config.py`:

  db       -> MySQL 3306, SSH 22
  frontend -> HTTP 80, HTTPS 443, SSH 22
  backend  -> API http 8080 /health, SSH 22
  web      -> HTTP 80, HTTPS 443
  mail     -> SMTP 25, Submission 587, SMTPS 465, IMAPS 993
  router   -> SSH 22, API 8728, Winbox 8291

Edit `SERVICE_CHECKS_BY_ROLE` to change ports/paths, or add per-instance checks.
Toggle the whole feature with `HEALTHCHECK_ENABLED` in `.env`.

### Limitation / next tier

Network probes confirm a service *answers on its port* but cannot list docker
container names/images/uptime the way Datadog's container view does. That needs
SSH (`docker ps`) or the Docker Engine API on each host - available as a follow-up
tier if you want true container-level visibility.
