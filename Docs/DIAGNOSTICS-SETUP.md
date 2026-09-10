# On-breach diagnostics — setup guide

By default an alert can only say *"DB Staging CPU is 93%"*. With this feature enabled
the monitor SSHes to the affected host the moment a threshold breaches, runs one
read-only script, and embeds the result in the alert email — so the mail tells you
**which container/process** was responsible and **what activity** it was doing
(including the actual SQL and how long it had been running).

**Deploy branch: whichever branch that server already runs.** `UAT` is the
repository default, but the staging and production servers on this estate were
deployed from **`SIT`** — check with `git -C /opt/ali-cloud-agent rev-parse
--abbrev-ref HEAD` before any `git pull` below and use that branch, or you will
roll the server back.

## Do it in this order

| Part | Where | Repeat? |
|---|---|---|
| **Part 1 — generate the key** | backend server | once |
| **Part 2 — provision the host** | *each* monitored host | per host |
| **Part 3 — wire up the app** | backend server | once |

> Part 2 needs the public key produced in Part 1, so **do Part 1 first**.

### Progress tracker

Fill this in as you go. Part 1 runs **once per environment** (staging and production each have their own
backend server, so each needs its own keypair). Part 2 runs per host.

**Staging** — ✅ **complete**, and every staging host trusts **both** monitors:

| Host | Role | 2a | 2b | 2c | 2d | 2e | network |
|---|---|---|---|---|---|---|---|
| DB Staging | database | ☑ | ☑ | ☑ | ☑ | ☑ | ☑ :22 |
| Backend Staging | app + monitor | ☑ | ☑ | ☑ | ☑ | ☑ | ☑ :22 |
| Frontend Staging | web | ☑ | ☑ | ☑ | ☑ | ☑ | ☑ :22 |

**Production** — ✅ **complete**. Part 1, all three hosts, and Part 3 done;
verified in-container against all three (`enabled: True`, 3/3 OK).

| Host | Role | 2a | 2b | 2c | 2d | 2e | network |
|---|---|---|---|---|---|---|---|
| DB Production | database | ☑ | ☑ | ☑ | ☑ | ☑ | ☑ **:22** |
| Backend Production | app + monitor | ☑ | ☑ | ☑ | ☑ | ☑ | ☑ **:1818** |
| Frontend Production | web | ☑ | ☑ | ☑ | ☑ | ☑ | ☑ **:1818** |

### Why staging hosts carry TWO keys

Production is the estate's only alerting source (see the production runbook §11),
so it is production that needs to collect evidence when a *staging* instance
breaches. The staging monitor separately needs access for its own dashboard's
docker panel. Each staging host therefore holds:

| `from=` | Key | Purpose |
|---|---|---|
| `172.28.92.57` | staging | the staging monitor, reaching other hosts |
| `192.168.32.0/20` | staging | the staging monitor reaching **its own host** (Backend Staging only) |
| `172.28.80.51` | production | breach diagnostics from the alerting monitor |

Verified from the production BE, passing instance ids so the
`PROBE_SSH_PORT_OVERRIDES` path is exercised — production's `frontend`/`backend`
roles default to 1818, and only the per-instance override reaches staging's 22:

```bash
docker exec cloud-agent-app python -c "
import diagnostics as d
for host, role, iid in (('172.28.92.60','db','i-k1ab5rh48e40enbqa7ii'),('172.28.92.56','frontend','i-k1a5ja5hi7ps6aa7x88r'),('172.28.92.57','backend','i-k1a4m0oobaw170notm7p')):
    out = d.run_focus(host,'containers',role,iid)
    print(host, role, '->', 'OK' if out and 'HOSTNAME' in out else 'FAILED')
"
```

### Two things learned provisioning six hosts

**Container subnets are per project and differ per host.** Production BE detected
`172.25.0.0/16`; Backend Staging detected `192.168.32.0/20`. Never assume the
usual `172.17`/`172.18` — always run the detection command, and re-run it if the
network is ever recreated, or that host silently stops reporting.

**Partially-provisioned hosts produce duplicate `authorized_keys` lines.** Four of
the six hosts already had a `cloudmonitor` user from an earlier attempt, so
appending produced duplicates (Backend Staging ended with 6 lines for 3 distinct
entries). Harmless — sshd takes the first match — but confusing during an
incident. Check and dedupe:

```bash
grep -no 'from="[^"]*"' /home/cloudmonitor/.ssh/authorized_keys
```

```bash
awk '!seen[$0]++' /home/cloudmonitor/.ssh/authorized_keys > /tmp/ak && mv /tmp/ak /home/cloudmonitor/.ssh/authorized_keys && chown cloudmonitor:cloudmonitor /home/cloudmonitor/.ssh/authorized_keys && chmod 600 /home/cloudmonitor/.ssh/authorized_keys && grep -o 'from="[^"]*"' /home/cloudmonitor/.ssh/authorized_keys
```

Watch for a *stale* subnet in that listing — a `from=` for a subnet the current
network no longer uses is dead weight that stops working silently.

(Actual IPs for this estate are in the internal runbooks, not in this repo.)

---

## Security model (read this first)

| Property | How it is enforced |
|---|---|
| The monitor logs in as an **unprivileged user** | dedicated `cloudmonitor` account |
| It can run **exactly one command** | a sudoers entry whitelisting only `/usr/local/bin/cam-diag` |
| It is **not** in the `docker` group | that group is root-equivalent; the single sudo entry is narrower |
| The key only works from one source | `from="<BACKEND_IP>"` restriction in `authorized_keys` |
| The script is **read-only** | inspects processes/containers/queries; changes nothing. Plain bash — review it before installing |
| No app data reaches a shell | the only argument is a keyword validated against a fixed allow-list (`cpu`/`memory`/`disk`/`service`/`containers`/`all`) |
| Failures are contained | hard timeouts, max 3 hosts per alert; any error just means the alert falls back to generic guidance |

The script is **`deploy/cam-diag.sh`** in this repo. Have each host owner read it.

---

# Part 1 — generate the monitor's key (backend server, once)

```bash
sudo mkdir -p /opt/ali-cloud-agent/secrets
sudo ssh-keygen -t ed25519 -N '' -C 'cloud-agent-diag' \
     -f /opt/ali-cloud-agent/secrets/diag_ed25519
sudo chmod 600 /opt/ali-cloud-agent/secrets/diag_ed25519

# The PUBLIC key — you will paste this into step 2d on every host:
sudo cat /opt/ali-cloud-agent/secrets/diag_ed25519.pub
```

The private key never leaves the backend server. `secrets/` is git-ignored.
**One keypair serves all hosts** — do not generate a new one per host.

---

# Part 2 — provision each monitored host

Run these on the host you are adding. Commands assume you are **root** (drop the
`sudo` if so).

### 2·0. Set these three variables first

Paste this once per shell session, with your real values. Every command below uses
them, so there are **no placeholders left to hand-edit**:

```bash
BACKEND_IP='10.0.0.57'                                        # the backend server's private IP
HOST_IP='10.0.0.60'                                           # the host you are provisioning
PUBKEY='ssh-ed25519 AAAA...replace-with-Part-1-output... cloud-agent-diag'
```

> ⚠️ Never paste `<SOMETHING>` into a shell — bash reads `<` as an input redirect
> and fails with `No such file or directory`. That is why these are variables.
>
> ⚠️ **Shell variables die with the session.** A web terminal (ECS Workbench)
> hands you a new session on reconnect or in a new tab, and these variables are
> then empty — step 2d silently writes `from="",...` with no key, producing a
> malformed `authorized_keys` line that grants nothing and is easy to miss because
> every command still "succeeds". Either re-paste 2·0 in the session you are
> actually using, or paste 2d with the values written in literally. Check with
> `cat /home/cloudmonitor/.ssh/authorized_keys` — an entry with `from=""` or no
> `ssh-ed25519 AAAA...` portion must be removed:
>
> ```bash
> sed -i '/^from="",/d' /home/cloudmonitor/.ssh/authorized_keys
> ```

### 2a. Create the unprivileged user

```bash
useradd --system --create-home --shell /bin/bash cloudmonitor
```

### 2b. Install the diagnostic script

**Check first, then pick ONE option — do not run both:**

```bash
ls -d /opt/ali-cloud-agent 2>/dev/null && echo "repo present -> use Option 1" \
                                       || echo "no repo -> use Option 2"
```

If you deployed with the split compose files (`docker-compose.{db,app,fe}.yml`),
**every** server has a checkout — frontend included — so Option 1 is the normal
path on all of them. Option 2 exists only for a host with no checkout at all.

**Option 1 — repo present on this host:**

```bash
cd /opt/ali-cloud-agent
git pull origin "$(git rev-parse --abbrev-ref HEAD)"
ls -l deploy/cam-diag.sh                     # confirm the file is present
install -m 0755 -o root -g root deploy/cam-diag.sh /usr/local/bin/cam-diag
```

**Option 2 — no repo on this host.** Copy the script over from the backend server.
Note the first command runs on the **backend**, the second on the target host:

```bash
# 1) on the BACKEND server — set HOST_IP to the host you are provisioning:
HOST_IP='10.0.0.56'
scp -P 22 /opt/ali-cloud-agent/deploy/cam-diag.sh root@"$HOST_IP":/tmp/   # -P 1818 on prod FE/BE

# 2) then on THAT host:
install -m 0755 -o root -g root /tmp/cam-diag.sh /usr/local/bin/cam-diag
```

> `Could not resolve hostname :` means `HOST_IP` was empty — set it first, and make
> sure you are running the `scp` on the **backend** server, not the target.

Either way, confirm and sanity-check (read-only, changes nothing):

```bash
ls -l /usr/local/bin/cam-diag
/usr/local/bin/cam-diag cpu | head -30
```

> `install: cannot stat 'cam-diag.sh'` means you are not in the directory holding the
> file — use the full `deploy/cam-diag.sh` path from the repo root, as above.

### 2c. Allow only that one command via sudo

Use plain redirection — a piped `tee` can be mangled by web terminals:

```bash
printf 'cloudmonitor ALL=(root) NOPASSWD: /usr/local/bin/cam-diag\n' > /etc/sudoers.d/cloudmonitor-diag
chmod 0440 /etc/sudoers.d/cloudmonitor-diag
visudo -c | tail -3
```

`visudo -c` must list `/etc/sudoers.d/cloudmonitor-diag: parsed OK`.

> **Only if `visudo -c` reports an error** for that file, delete it immediately so
> `sudo` is not left broken (`rm -f /etc/sudoers.d/cloudmonitor-diag`) and retry.
> **If it parsed OK, do NOT delete it** — that is the entry the monitor needs.

### 2d. Authorise the monitor's key

Uses `$PUBKEY` and `$BACKEND_IP` from step 2·0. The `from=` restriction makes the
key usable *only* from the backend server:

```bash
install -d -m 700 -o cloudmonitor -g cloudmonitor /home/cloudmonitor/.ssh

printf 'from="%s",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty %s\n' \
  "$BACKEND_IP" "$PUBKEY" >> /home/cloudmonitor/.ssh/authorized_keys

chown cloudmonitor:cloudmonitor /home/cloudmonitor/.ssh/authorized_keys
chmod 600 /home/cloudmonitor/.ssh/authorized_keys

# verify the line looks right (one line, starts with from=, ends with the comment)
tail -1 /home/cloudmonitor/.ssh/authorized_keys
```

`no-pty` is safe: the app uses an SSH *exec* channel, not an interactive shell.

### 2e. Verify locally

```bash
sudo -u cloudmonitor sudo -n /usr/local/bin/cam-diag cpu | head -8
```

Expect `== HOST ==` / `== CPU SNAPSHOT ==`. *"sudo: a password is required"* means
2c did not apply.

### 2f. Network

Allow **the backend server's IP → this host's sshd port** in the security group.
That is `:22` on most hosts but **`:1818` on the production FE and BE boxes** —
check with `ss -tlnp | grep sshd` on the host rather than assuming.

### 2g. Confirm from the backend server

Run this **on the backend server** (set `HOST_IP` there too — see 2·0). It
exercises the exact path the app uses: SSH → sudo → script.

```bash
HOST_IP='10.0.0.60'      # the host you just provisioned

ssh -i /opt/ali-cloud-agent/secrets/diag_ed25519 -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new \
    cloudmonitor@"$HOST_IP" 'sudo -n /usr/local/bin/cam-diag cpu' | head -10
```

---

## Notes per host type

**Database hosts** — the highest value. The script enumerates every running
PostgreSQL container and reports live `pg_stat_activity` rows plus recent
errors/OOM messages, which is what actually names the offending query.

**The backend server is itself a monitored host** — and it needs one extra step.

It is both monitor and target, so it needs the full Part 2. Set its inventory `host`
to the **private IP, not `127.0.0.1`** (inside the container, localhost is the
container, not the host).

⚠️ **The `from=` restriction needs a second entry on this host.** When the container
reaches *other* hosts, the traffic leaves the box and is SNAT'd to the backend's own
IP — so `from="$BACKEND_IP"` matches. But when it connects to the backend's **own**
IP the traffic never leaves: it is delivered locally over the Docker bridge, so sshd
sees the **container's** address instead and rejects the key
(`AuthenticationException: Authentication failed`, even though the same `ssh` command
works from the host shell).

Add a second `authorized_keys` line scoped to the container subnet — detected
automatically, so there is nothing to look up:

```bash
NET=$(docker inspect cloud-agent-app --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}')
SUBNET=$(docker network inspect "$NET" --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}')
echo "container network: $NET  subnet: $SUBNET"

printf 'from="%s",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty %s\n' \
  "$SUBNET" "$PUBKEY" >> /home/cloudmonitor/.ssh/authorized_keys

wc -l /home/cloudmonitor/.ssh/authorized_keys      # expect 2
```

The subnet is Docker-internal and not routable from outside the host, so this remains
least-privilege. On the production BE it detected `172.25.0.0/16` — do not assume
the usual `172.17`/`172.18`; Compose allocates per project, and the value changes
if the network is recreated, which would silently break collection for that one
host. Re-run the detection command if `docker network prune` or a
`docker compose down` has removed the network since.

**Do not use 2g to verify this host.** `ssh` from its own shell to its own IP
carries the host's source address, so it matches the FIRST line and passes even
if the subnet line is wrong or missing. Only the in-container check (3e) proves
it. Re-running the container-side check should then report `OK` for the
backend's own IP.

**Frontend / non-database hosts** — everything works except the PostgreSQL
sections, which are simply omitted when no Postgres container is present. You still
get load, top processes, container CPU/memory, and listening ports. `docker stats
unavailable` on a host without Docker is expected and harmless.

---

# Part 3 — wire up the app (backend server, once)

### 3a. Mount the key into the container

**Already in the repo** — `docker-compose.app.yml` mounts the secrets directory
read-only, so the Part 1 key appears inside the container at
`/run/secrets/diag_ed25519`:

```yaml
    volumes:
      - ./secrets:/run/secrets:ro
```

Nothing to edit on the server; the `git pull` in 3c picks it up. (A directory is
mounted rather than a single file so this stays harmless before a key exists.)

### 3b. Configure

Append to `/opt/ali-cloud-agent/.env` — **no inline comments**, Docker Compose
keeps them as part of the value:

```bash
cd /opt/ali-cloud-agent
sed -i '/^DIAG_/d' .env
printf '%s\n' \
  'DIAG_ENABLED=true' \
  'DIAG_SSH_USER=cloudmonitor' \
  'DIAG_SSH_KEY=/run/secrets/diag_ed25519' \
  'DIAG_REMOTE_SCRIPT=/usr/local/bin/cam-diag' \
  'DIAG_TIMEOUT=45' \
  'DIAG_MAX_HOSTS=3' >> .env
grep '^DIAG_' .env
```

### The dashboard's "Docker services" panel uses this same setup

Once the SSH identity below works, the dashboard shows every container each host
reports, with its docker health state (`healthy`, `unhealthy`, `restarting`,
`stopped`). That comes from one extra allow-listed focus, `cam-diag containers`,
which runs a single `docker ps` and prints nothing else - no new privilege beyond
what breach evidence already needs.

Two consequences worth knowing:

* **Re-install `cam-diag.sh` on every host after upgrading the app.** An older
  copy does not know the `containers` argument and falls back to printing the full
  human-readable report, which the parser will discard - the panel stays empty
  with no obvious error.
* It runs **every scan cycle**, not only on a breach, so keep the host script
  current; `CAM_DIAG_MAX_CONTAINERS` (default 60) bounds the output, and
  `CONTAINER_ROLES` keeps the monitor from asking hosts that have no docker (a
  managed RDS instance, a router, a Windows box).

Set `CONTAINERS_ENABLED=false` to turn the panel off while leaving breach
diagnostics enabled.

**`DIAG_SSH_PORT` is deliberately absent above.** Left unset, diagnostics
connects on the same port as that role's SSH *probe* - `PROBE_SSH_PORT`, or
`PROBE_<ROLE>_SSH_PORT` for a single role. A host whose sshd is not on 22 is
then corrected in one place and both the probe and evidence collection follow.
Set `DIAG_SSH_PORT` only if diagnostics must use a different port than the probe.

If a host's SSH probe is red with `ConnectionRefusedError`, diagnostics will fail
against that host for the same reason - fix the port before enabling this.

`DIAG_MAX_HOSTS=3` caps how many hosts are contacted per alert, so a multi-host
breach cannot stretch the scan cycle. Raise it if you monitor more than three hosts
and want evidence from all of them.

### 3c. Apply

```bash
cd /opt/ali-cloud-agent
git pull origin "$(git rev-parse --abbrev-ref HEAD)"
docker compose -f docker-compose.app.yml up -d --build
```

`--build` (not just `--force-recreate`) is required: `paramiko` is a dependency.

### 3d. Confirm each instance has a `host`

Diagnostics reuse the service-probe `host`. On the register page (`instances.html`),
every instance you want evidence from needs its private IP/DNS filled in — a blank
host is skipped silently.

### 3e. Verify end-to-end

```bash
HOST_IP='10.0.0.60'      # a host you have provisioned

docker exec cloud-agent-app python -c "
import diagnostics as d
print('enabled:', d.enabled())
out = d._run_remote('$HOST_IP', 'cpu')
print(out[:400] if out else 'FAILED - see docker logs cloud-agent-app')"
```

Expect `enabled: True` and the `== HOST ==` sections. Then trigger a real alert —
click **Send alert email** on the dashboard while a breach is present, or wait for
the next one.

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

That names the **service** (`klip-postgres`, driven by that client), the **activity**
(the specific SQL) and **how long** it had been running (56 s) — the three things
previously only obtainable by SSHing in by hand.

When evidence is available the generic "likely causes" list is suppressed. If
collection fails (host unreachable, key not authorised, sudo not configured) the
alert still sends, with the generic guidance as before.

---

## Troubleshooting

| Symptom (in `docker logs cloud-agent-app`) | Cause | Fix |
|---|---|---|
| no `diagnostics` lines at all | disabled or unset | check `DIAG_ENABLED=true`, `DIAG_SSH_USER`, `DIAG_SSH_KEY` |
| `AuthenticationException` | key not authorised, or `from=` does not match the backend's source IP | redo 2d; check `authorized_keys` ownership/permissions |
| `AuthenticationException` **only for the backend's own IP**, while `ssh` works from that host's shell | container→own-host traffic arrives from the Docker bridge, not the host IP, so `from=` fails | add the container-subnet `authorized_keys` line — see "Notes per host type" |
| `(no output; stderr: sudo: a password is required)` | sudoers entry missing or deleted | redo 2c, run `visudo -c` |
| `sudo: sorry, you must have a tty` | `requiretty` set in sudoers (rare on Ubuntu) | add `Defaults:cloudmonitor !requiretty` |
| `timed out` / `NoValidConnectionsError` | port 22 blocked from the backend | add the security-group rule (2f) |
| `paramiko not installed` | image not rebuilt | `docker compose … up -d --build` |
| evidence section absent but alert arrives | instance has no `host` in the inventory | set it on the register page (3d) |
| evidence for only some hosts | `DIAG_MAX_HOSTS` reached | raise it in `.env` |
| `docker stats unavailable` in the output | no Docker on that host, or not permitted | expected on non-Docker hosts; other sections still work |

## Turning it off

```bash
sed -i 's|^DIAG_ENABLED=.*|DIAG_ENABLED=false|' /opt/ali-cloud-agent/.env
docker compose -f docker-compose.app.yml up -d --force-recreate
```

Alerts continue to work with generic guidance instead of live evidence. To revoke
access entirely, on each host delete `/etc/sudoers.d/cloudmonitor-diag` and the
`authorized_keys` entry (and optionally `userdel -r cloudmonitor`).
