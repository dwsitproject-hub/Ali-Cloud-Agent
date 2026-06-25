# Cloud Agent Monitoring

A two-agent cloud monitoring service with a live dashboard.

- **Agent 1 (scanner)** queries the latest value of each configured metric from
  **Alibaba CloudMonitor** (`DescribeMetricLast`) and checks it against an alert
  threshold.
- **Agent 2 (alerter)** composes an HTML alert and sends it through **Alibaba
  DirectMail** (`SingleSendMail`).
- A **scheduler** runs Agent 1 every 5 minutes and **auto-triggers Agent 2** when
  any threshold is breached.
- A **Flask dashboard** shows the latest scan, lets you scan on demand, and has a
  **Send** button to fire the alert email manually.
- **PostgreSQL** stores the monitored inventory, scan/alert history, and users.
- **SSO login** (Downstream Hub) gates every page; a **register page** lets you
  add/manage monitored instances without editing code.

Code is organized into `Backend/` (Python), `Frontend/` (templates + static),
`Docs/`, and `Assets/`.

The pieces this build wired up:

1. **Real CloudMonitor API** — `Backend/agents/agent1_scanner.py` makes signed
   `DescribeMetricLast` calls and parses the `Datapoints` JSON.
2. **Real email sender** — `Backend/agents/agent2_alerter.py` sends via DirectMail.
3. **Scheduling** — `Backend/scheduler.py` runs the scan on a 5-minute cron and
   auto-sends on breach.
4. **PostgreSQL + migrations** — inventory and history persist across restarts.
5. **SSO + dev login** — `Backend/auth/sso.py` verifies the Hub's HS256 JWT.

## Quick start (mock mode — no credentials needed)

Requires Docker (for Postgres) and Python 3.

```bash
pip install -r Backend/requirements.txt
cp .env.example .env                 # MOCK_MODE=true, DEV_LOGIN_ENABLED=true

docker compose up -d db              # PostgreSQL on host port 5440

# create tables + load the 9 default instances and a dev user
export PYTHONPATH=Backend FLASK_APP=app   # PowerShell: $env:PYTHONPATH="Backend"; $env:FLASK_APP="app"
python -m flask db upgrade
python -m flask seed

python Backend/app.py                # open http://127.0.0.1:5000
```

On Windows you can instead double-click **`start.bat`** (it installs deps and
runs the app; run the `docker compose` + `flask db upgrade` + `flask seed` steps
once first).

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
`/instances` (linked from the dashboard) to add an instance — its ECS id, display
name, `role` (selects the default service checks), `host` (IP/DNS for probes;
blank skips them), and group — or enable/disable/delete existing ones. Changes
are picked up on the next scan; no code edit or restart needed.

`flask seed` loads an initial 9 instances from `config.INSTANCE_GROUPS` (seed data
only). Metric definitions/thresholds (`METRIC_TEMPLATES`) and the per-role service
checks (`SERVICE_CHECKS_BY_ROLE`) remain in `Backend/config.py`. Namespaces and
metric names come from CloudMonitor's *Appendix 1: Metrics*.

## HTTP API

All routes require a login session except `/api/health` and `/auth/*`. Anonymous
`/api/*` calls return `401` JSON; pages redirect to `/auth/login`.

| Method      | Path                   | Purpose                                       |
|-------------|------------------------|-----------------------------------------------|
| GET         | `/`                    | Dashboard UI                                  |
| GET         | `/api/status`          | Latest scan + last alert + history (JSON)     |
| POST        | `/api/scan`            | Run Agent 1 now (`?auto_alert=true`)          |
| POST        | `/api/send`            | Run Agent 2 against the latest scan           |
| GET         | `/api/health`          | Liveness probe (public)                       |
| GET         | `/instances`           | Register / manage instances page              |
| GET/POST    | `/api/instances`       | List / create instances                       |
| PATCH/DELETE| `/api/instances/<id>`  | Toggle enabled / edit / delete                |
| POST        | `/auth/hub`            | SSO entry — Hub posts an HS256 JWT            |
| GET/POST    | `/auth/dev-login`      | Local dev login (gated by `DEV_LOGIN_ENABLED`)|
| GET         | `/auth/login`,`/logout`| Login page / sign out                         |

## Files

```
Backend/
  app.py                  create_app() factory, blueprints, login guard
  scheduler.py            APScheduler: 5-min scan + auto-alert (app-context job)
  config.py               Env config + metric templates / per-role checks (+ seed data)
  cloud_client.py         Signed Alibaba RPC client (CommonRequest)
  db.py / models.py       SQLAlchemy instance + ORM models
  state.py                DB-backed persist + snapshot() reconstruction
  seed.py                 `flask seed` (idempotent inventory + dev user)
  instances_api.py        Register/manage-instances page + CRUD API
  auth/sso.py             SSO (/auth/hub) + dev login + session
  agents/agent1_scanner.py   CloudMonitor scan + threshold evaluation
  agents/agent2_alerter.py   DirectMail email composition + send
  migrations/             Alembic migrations
  tests/                  pytest (contract, instances CRUD, auth)
Frontend/templates/       dashboard.html, register_instance.html, login.html
docker-compose.yml        Local PostgreSQL (host port 5440)
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
ExecStart=/opt/cloud-agent-monitoring/.venv/bin/gunicorn --workers 1 --chdir Backend --bind 127.0.0.1:5000 wsgi:app
```

(`Backend/wsgi.py` runs the bootstrap so the scheduler still starts. More than one
worker would duplicate the scheduler and double every scan/alert.)

## Production notes

- State and history live in **PostgreSQL**, but the **scheduler must still be a
  single process** (one APScheduler). Keep gunicorn at `--workers 1`, or move the
  scheduler to its own process if you scale web workers.
- Provide a strong `SECRET_KEY` and the Hub's `SSO_TOKEN_SECRET`; set
  `DEV_LOGIN_ENABLED=false`, `SESSION_COOKIE_SECURE=true`, and serve over HTTPS so
  the cross-site SSO landing (`SameSite=None`) works.
- Run DB migrations (`flask db upgrade`) on deploy. Scan history grows ~50 rows
  per cycle — add pruning if retention matters.
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
