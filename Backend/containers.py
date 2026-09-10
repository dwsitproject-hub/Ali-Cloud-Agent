"""What docker workloads each monitored host reports running.

Complements healthcheck.py rather than duplicating it. A health probe answers
"can the monitor reach port 5432 from outside?"; this answers "what does the box
itself say is running, and does docker consider it healthy?" - which is how you
tell a container that is up but failing its healthcheck, or one stuck in a restart
loop, from one that is simply unreachable across the network.

Collection reuses the diagnostics SSH path deliberately: the same unprivileged
user, the same single whitelisted sudo command, the same per-role port. The only
new capability is one more allow-listed focus argument (`containers`), which runs
one `docker ps` and prints nothing else. No Docker socket is exposed, and the
monitor never gains the ability to run arbitrary commands.

Requires the §7 diagnostics setup on each host (Docs/DIAGNOSTICS-SETUP.md). With
no SSH user configured this collects nothing and says so, instead of failing.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import config

log = logging.getLogger("containers")

# docker's State vocabulary -> whether it deserves attention on a dashboard.
_BAD_STATES = {"exited", "dead", "removing"}
_WARN_STATES = {"restarting", "paused", "created"}


def enabled() -> bool:
    """Container collection needs the same SSH identity diagnostics uses."""
    return bool(config.CONTAINERS_ENABLED and config.DIAG_SSH_USER
                and config.DIAG_SSH_KEY)


def classify(state: str, status: str) -> str:
    """Normalise docker's (State, Status) pair into one dashboard word.

    docker reports health only inside the free-text Status ("Up 4 hours
    (healthy)"), so a container with a failing healthcheck still has
    State=running - which is exactly the case worth surfacing.
    """
    state = (state or "").strip().lower()
    blob = (status or "").lower()
    if "(unhealthy)" in blob:
        return "unhealthy"
    if state in _BAD_STATES:
        return "stopped"
    if state in _WARN_STATES:
        return state
    if "(healthy)" in blob:
        return "healthy"
    if "(health: starting)" in blob:
        return "starting"
    if state == "running":
        return "running"
    return state or "unknown"


# The `containers` focus opens with this sentinel. Its absence means the host is
# running a cam-diag from before that focus existed: an old copy does not
# recognise the argument, silently falls back to FOCUS=all, and returns the full
# human-readable report. Detecting that matters twice over - the panel would
# otherwise sit empty with no explanation, AND the host would run the entire
# diagnostic sweep every scan cycle instead of one cheap `docker ps`.
_SENTINEL = "HOSTNAME\t"

_STALE_SCRIPT = ("host script is too old to support the 'containers' focus - "
                 "re-install deploy/cam-diag.sh on this host "
                 "(Docs/DIAGNOSTICS-SETUP.md step 2b)")


def parse(output: str) -> tuple:
    """``(containers, error)`` from the remote script's `containers` output.

    Tab-separated on purpose: image names carry ":" and "/", and Status carries
    spaces and parentheses, so neither is a safe delimiter.
    """
    text = output or ""
    rows, error = [], None
    for line in text.splitlines():
        parts = line.rstrip("\n").split("\t")
        if parts[0] == "ERR":
            error = parts[1] if len(parts) > 1 else "unknown error"
            continue
        if parts[0] != "CTR" or len(parts) < 5:
            continue
        name, image, state, status = parts[1], parts[2], parts[3], parts[4]
        ports = parts[5] if len(parts) > 5 else ""
        rows.append({
            "name": name, "image": image, "state": state, "status": status,
            "health": classify(state, status), "ports": ports,
        })
    if not rows and error is None and text.strip() and _SENTINEL not in text:
        # Output, but not OUR output: an outdated host script (see _SENTINEL).
        error = _STALE_SCRIPT
    rows.sort(key=lambda c: (c["health"] not in ("unhealthy", "stopped", "restarting"),
                             c["name"]))
    return rows, error


def _collect_one(target: tuple) -> tuple:
    """``(instance_id, containers, error)`` for one host."""
    inst_id, host, role = target
    import diagnostics
    output = diagnostics.run_focus(host, "containers", role, inst_id)
    if output is None:
        return inst_id, [], "SSH collection failed (see backend log)"
    rows, error = parse(output)
    return inst_id, rows, error


def refresh() -> int:
    """Update every enabled instance's container list. Returns hosts collected.

    Must run inside a Flask app context. Never raises - a host that cannot be
    reached records the reason and leaves the rest of the estate alone.
    """
    if not enabled():
        return 0

    from db import db
    from models import Instance

    instances = [i for i in Instance.query.filter_by(enabled=True).all()
                 if (i.host or "").strip() and (i.role or "") in config.CONTAINER_ROLES]
    if not instances:
        return 0

    targets = [(i.id, i.host.strip(), i.role or "") for i in instances]
    workers = min(config.SCAN_CONCURRENCY, len(targets))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_collect_one, targets))

    by_id = {i.id: i for i in instances}
    now = datetime.now(timezone.utc)
    collected = 0
    for inst_id, rows, error in results:
        inst = by_id.get(inst_id)
        if inst is None:
            continue
        inst.containers_json = rows
        inst.containers_error = error
        inst.containers_updated_at = now
        if rows:
            collected += 1

    db.session.commit()
    unhealthy = sum(1 for _, rows, _ in results
                    for c in rows if c["health"] in ("unhealthy", "stopped", "restarting"))
    log.info("containers: %s/%s host(s) reported, %s container(s) needing attention",
             collected, len(targets), unhealthy)
    return collected
