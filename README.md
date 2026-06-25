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

The three things this build wired up:

1. **Real CloudMonitor API** — `agents/agent1_scanner.py` makes signed
   `DescribeMetricLast` calls and parses the `Datapoints` JSON (no more simulated scan).
2. **Real email sender** — `agents/agent2_alerter.py` sends via Alibaba DirectMail.
3. **Scheduling** — `scheduler.py` runs the scan on a 5-minute cron (APScheduler)
   and auto-sends on breach.

## Quick start (mock mode — no credentials needed)

```bash
pip install -r requirements.txt
cp .env.example .env          # MOCK_MODE=true by default
python app.py                 # open http://127.0.0.1:5000
```

In mock mode the scanner generates synthetic metric values (some breach, so you
can see alerts) and Agent 2 renders the email but does **not** transmit it.

## Going live

1. In the Alibaba Cloud console, create a RAM user with permissions
   `cms:QueryMetricLast` and `Dm:SingleSendMail`, and generate an AccessKey.
2. In **DirectMail**, verify a sender domain and create a sender address.
3. Edit `.env`:

   ```ini
   MOCK_MODE=false
   ALIBABA_ACCESS_KEY_ID=LTAI...
   ALIBABA_ACCESS_KEY_SECRET=********
   CLOUDMONITOR_REGION=cn-hangzhou
   DIRECTMAIL_REGION=cn-hangzhou
   DM_ACCOUNT_NAME=alert@mail.yourdomain.com
   ALERT_RECIPIENTS=you@example.com,ops@example.com
   ```

4. `python app.py`. The app will now make real API calls.

Credentials are read from the environment only — nothing is hard-coded, and
`.env` is git-ignored.

## Configuring what to monitor

Default metrics (ECS CPU/memory/disk, RDS CPU) and their thresholds live in
`config.py` (`DEFAULT_METRICS`). To override without editing code, drop a
`metrics.json` file next to `config.py`:

```json
[
  {
    "key": "ecs_cpu", "label": "ECS CPU", "namespace": "acs_ecs_dashboard",
    "metric_name": "CPUUtilization", "period": "60",
    "dimensions": "[{\"instanceId\":\"i-xxxx\"}]",
    "stat": "Average", "threshold": 80.0, "comparison": ">", "unit": "%"
  }
]
```

`dimensions` is optional; supply it to target a specific instance. Namespaces and
metric names come from CloudMonitor's *Appendix 1: Metrics*.

## HTTP API

| Method | Path          | Purpose                                            |
|--------|---------------|----------------------------------------------------|
| GET    | `/`           | Dashboard UI                                       |
| GET    | `/api/status` | Latest scan + last alert + recent history (JSON)   |
| POST   | `/api/scan`   | Run Agent 1 now (`?auto_alert=true` to also alert) |
| POST   | `/api/send`   | Run Agent 2 against the latest scan (Send button)  |
| GET    | `/api/health` | Liveness probe                                     |

## Files

```
app.py                  Flask app, routes, startup (first scan + scheduler)
scheduler.py            APScheduler: 5-min scan + auto-alert on breach
config.py               Env config + metric/threshold definitions
cloud_client.py         Signed Alibaba RPC client (CommonRequest)
state.py                In-memory last-scan / last-alert / history store
agents/agent1_scanner.py   CloudMonitor scan + threshold evaluation
agents/agent2_alerter.py   DirectMail email composition + send
templates/dashboard.html   Dashboard UI
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
sudo .venv/bin/pip install -r requirements.txt

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
ExecStart=/opt/cloud-agent-monitoring/.venv/bin/gunicorn --workers 1 --bind 127.0.0.1:5000 wsgi:app
```

(`wsgi.py` runs the bootstrap so the scheduler still starts. More than one worker
would duplicate the scheduler and double every scan/alert.)

## Production notes

- `state.py` is in-memory and fine for a single process. For multiple workers or
  instances, back it with Redis or a database.
- Run under a process manager (systemd, supervisor) or a container so the
  scheduler stays alive. Use a single worker, or move the scheduler to its own
  process so the cron job isn't duplicated.
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

### Configure (config.py -> INSTANCE_GROUPS)

Each instance has a `role` and a `host`. Set `host` to the IP/DNS the monitor can
reach; leave it blank to skip that server (its services show as "unconfigured",
never "down"). The `role` selects the default checks from `SERVICE_CHECKS_BY_ROLE`:

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
