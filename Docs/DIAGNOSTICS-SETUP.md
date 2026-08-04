# On-breach diagnostics — setup guide

By default an alert can only say *"DB Staging CPU is 93%"*. With this feature enabled
the monitor SSHes to the affected host the moment a threshold breaches, runs one
read-only script, and embeds the result in the alert email — so the mail tells you
**which container/process** was responsible and **what activity** it was doing
(including the actual SQL and how long it had been running).

Do **Part A on every monitored host**, then **Part B once on the backend server**.

---

## Security model (read this first)

| Property | How it is enforced |
|---|---|
| The monitor logs in as an **unprivileged user** | dedicated `cloudmonitor` account, no shell login needed beyond running the script |
| It can run **exactly one command** | a sudoers entry whitelisting only `/usr/local/bin/cam-diag` |
| It is **not** in the `docker` group | that group is root-equivalent; the single sudo entry is narrower |
| The script is **read-only** | inspects processes/containers/queries; changes nothing. It is plain bash — review it before installing |
| No app data reaches a shell | the only argument is a keyword validated against a fixed allow-list (`cpu`/`memory`/`disk`/`service`/`all`) |
| Failures are contained | hard timeouts, max 3 hosts per alert, and any error just means the alert falls back to generic guidance |

The script lives at **`deploy/cam-diag.sh`** in this repo. Have each host owner read it.

---

# Part A — on each monitored host

Repeat for every server you want evidence from (start with the DB host — that is
where all incidents so far originated).

### A1. Create the unprivileged user

```bash
sudo useradd --system --create-home --shell /bin/bash cloudmonitor
```

### A2. Install the diagnostic script

Copy `deploy/cam-diag.sh` from the repo onto the host, then:

```bash
sudo install -m 0755 -o root -g root cam-diag.sh /usr/local/bin/cam-diag

# sanity-check it works and is read-only in effect
sudo /usr/local/bin/cam-diag cpu | head -30
```

### A3. Allow only that one command via sudo

```bash
echo 'cloudmonitor ALL=(root) NOPASSWD: /usr/local/bin/cam-diag' \
  | sudo tee /etc/sudoers.d/cloudmonitor-diag
sudo chmod 0440 /etc/sudoers.d/cloudmonitor-diag
sudo visudo -c        # must report "parsed OK"
```

> `visudo -c` is important — a malformed sudoers file can lock out sudo entirely.

### A4. Authorise the monitor's SSH key

You will generate the key **once** in step B1. Once you have its public key,
on each host:

```bash
sudo -u cloudmonitor mkdir -p /home/cloudmonitor/.ssh
sudo -u cloudmonitor tee -a /home/cloudmonitor/.ssh/authorized_keys <<'EOF'
<paste the PUBLIC key from step B1 here>
EOF
sudo -u cloudmonitor chmod 700 /home/cloudmonitor/.ssh
sudo -u cloudmonitor chmod 600 /home/cloudmonitor/.ssh/authorized_keys
```

**Optional hardening** — prefix the key line with source restrictions so it is
only usable from the backend server and cannot forward ports:

```
from="<backend-server-private-ip>",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAA... cloud-agent-diag
```

### A5. Verify locally

```bash
sudo -u cloudmonitor sudo -n /usr/local/bin/cam-diag cpu | head -20
```
Expect the diagnostic output. If you see *"sudo: a password is required"*, step A3
did not apply.

### A6. Network

The backend server must reach this host on **port 22**. Add a security-group rule
allowing `<backend-server-private-ip>` → this host `:22`.

---

# Part B — once, on the backend server

### B1. Generate the monitor's SSH key

```bash
sudo mkdir -p /opt/ali-cloud-agent/secrets
sudo ssh-keygen -t ed25519 -N '' -C 'cloud-agent-diag' \
     -f /opt/ali-cloud-agent/secrets/diag_ed25519
sudo chmod 600 /opt/ali-cloud-agent/secrets/diag_ed25519

# This is the PUBLIC key to paste into step A4 on every host:
sudo cat /opt/ali-cloud-agent/secrets/diag_ed25519.pub
```

The private key stays on the backend server only. `secrets/` is git-ignored.

### B2. Mount the key into the container

The app runs in Docker, so the key must be visible inside it. Add to the `app`
service in `docker-compose.app.yml`:

```yaml
    volumes:
      - ./secrets/diag_ed25519:/run/secrets/diag_ed25519:ro
```

### B3. Configure the app

Add to `/opt/ali-cloud-agent/.env` — **no inline comments** (Docker Compose keeps
them as part of the value):

```ini
DIAG_ENABLED=true
DIAG_SSH_USER=cloudmonitor
DIAG_SSH_KEY=/run/secrets/diag_ed25519
DIAG_SSH_PORT=22
DIAG_REMOTE_SCRIPT=/usr/local/bin/cam-diag
DIAG_TIMEOUT=20
DIAG_MAX_HOSTS=3
```

### B4. Make sure each instance has a `host` set

Diagnostics use the same `host` value as the service probes. On the register page
(`instances.html`), confirm each instance you want evidence from has its private
IP/DNS filled in — a blank host means it is skipped.

### B5. Apply

```bash
cd /opt/ali-cloud-agent
git pull origin production-refactor
docker compose -f docker-compose.app.yml up -d --build
```

### B6. Verify end-to-end

```bash
# 1. Is it enabled and can it reach a host?
docker exec cloud-agent-app python -c "
import diagnostics as d
print('enabled:', d.enabled())
print(d._run_remote('<monitored-host-ip>', 'cpu')[:600])"
```

Expect the `== HOST ==` / `== TOP PROCESSES BY CPU ==` sections. Then send a real
alert — click **Send alert email** on the dashboard while a breach is present, or
wait for the next one. The email will contain a **"What caused this — live evidence
from the affected host"** section.

---

## What the alert will now contain

Instead of generic possibilities, the email shows the captured reality:

```
== TOP PROCESSES BY CPU (ELAPSED = how long it has been running) ==
33.4  4.8  01:09  70  postgres  postgres: klip_db 10.0.0.57(44656) SELECT

== CONTAINERS BY CPU/MEM ==
klip-postgres   188.20%   880MiB / 3.4GiB   25.8%

== ACTIVE QUERIES — container: klip-postgres ==
klip_db | postgres | 10.0.0.57 | active | | 56 | WITH contract_candidates AS (SELECT DISTINCT ...
```

That names the **service** (`klip-postgres`, driven by the client at `10.0.0.57`),
the **activity** (the specific SQL), and **how long** it had been running (56 s) —
the three things previously only obtainable by SSHing in manually.

When evidence is available the generic "likely causes" list is suppressed, since
the alert no longer needs to guess. If collection fails (host unreachable, key not
authorised, sudo not configured) the alert still sends, with the generic guidance
as before.

---

## Troubleshooting

| Symptom (in `docker logs cloud-agent-app`) | Cause | Fix |
|---|---|---|
| no `diagnostics` lines at all | disabled or unset | check `DIAG_ENABLED=true`, `DIAG_SSH_USER`, `DIAG_SSH_KEY` |
| `AuthenticationException` | public key not authorised on the host | redo A4; check `authorized_keys` ownership/permissions |
| `(no output; stderr: sudo: a password is required)` | sudoers entry missing/typo | redo A3, run `visudo -c` |
| `timed out` / `NoValidConnectionsError` | port 22 blocked from the backend | add the security-group rule (A6) |
| `paramiko not installed` | image not rebuilt after the dependency was added | `docker compose … up -d --build` |
| evidence section absent but alert arrives | instance has no `host` in the inventory | set it on the register page (B4) |
| `docker stats unavailable` inside the output | the script ran but Docker is not present/permitted | expected on non-Docker hosts; other sections still work |

## Turning it off

```bash
sed -i 's|^DIAG_ENABLED=.*|DIAG_ENABLED=false|' /opt/ali-cloud-agent/.env
docker compose -f docker-compose.app.yml up -d --force-recreate
```
Alerts continue to work, with generic guidance instead of live evidence. To remove
access entirely, delete `/etc/sudoers.d/cloudmonitor-diag` and the
`authorized_keys` entry on each host.
