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
import re
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


def _pct(raw: str):
    """`docker stats` percentages arrive as "12.55%" — and as "--" for a
    container that has just started or is not running."""
    try:
        return round(float((raw or "").strip().rstrip("%")), 2)
    except ValueError:
        return None


# docker prints MemUsage as "435MiB / 1GiB", in whichever unit reads best.
_UNITS = {"b": 1, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4,
          "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4}


def _bytes(raw: str):
    """"435MiB" -> 456130560. None for "--" or anything unparseable."""
    text = (raw or "").strip().lower()
    match = re.match(r"^([0-9]*\.?[0-9]+)\s*([a-z]+)$", text)
    if not match:
        return None
    unit = _UNITS.get(match.group(2))
    if unit is None:
        return None
    try:
        return int(float(match.group(1)) * unit)
    except ValueError:
        return None


def _usage_pair(raw: str) -> tuple:
    """"435MiB / 1GiB" -> ``(used_bytes, limit_bytes)``, either may be None."""
    parts = (raw or "").split("/")
    used = _bytes(parts[0]) if parts else None
    limit = _bytes(parts[1]) if len(parts) > 1 else None
    return used, limit


def host_mem_bytes(rows: list):
    """Total RAM of the host, read back out of docker's own output.

    A container with no ``mem_limit`` has the whole machine as its ceiling, so
    `docker stats` prints the HOST total as its limit - and several such
    containers all print the same number. That makes the most-repeated limit a
    reliable measure of host RAM, and crucially a *self-correcting* one: when
    the instance was resized from 8 GB to 16 GB on 2026-11-25 the cached
    `Instance.memory_mb` spec still read 8192 for hours (SPECS_REFRESH_HOURS),
    while this figure was right on the next scan.

    Returns None when every container is capped - then no host-relative share
    can be computed, and callers must fall back to absolute bytes.
    """
    counts = {}
    for row in rows:
        limit = row.get("mem_limit_bytes")
        if limit:
            counts[limit] = counts.get(limit, 0) + 1
    # Two containers sharing a ceiling means it is the machine, not a coincidence
    # of two services configured with the same mem_limit.
    shared = [limit for limit, n in counts.items() if n >= 2]
    return max(shared) if shared else None


def mem_share(row: dict):
    """A container's memory as a share of the HOST, or None if unknown.

    NOT `docker stats` MemPerc. That is measured against the container's own
    ``mem_limit`` when one is set, so the column mixes denominators and cannot
    be compared across rows or against the host metric beside it. On Backend
    Production this inverted the ranking outright: `aastar-price-api` showed
    42.48% (435 MiB of a 1 GiB cap) while
    `plantation-rainfall-backend-production` showed 6.06% for a LARGER 449 MiB,
    having no cap at all.
    """
    return row.get("mem_pct_host")


# Below this share a container is not an explanation for a host breach. Without a
# floor, a ranking of containers that ALL read 0.0% still has a first row, and the
# email and dashboard presented it as "the heaviest" - sending the reader to an
# idle service. That happens whenever the load is somewhere `docker stats` cannot
# see: an image build or a restart loop (both run in the daemon), a process on the
# host itself, or a spike that ended before the sample. The frontend keeps the
# same number (NEGLIGIBLE_PCT in app.js).
NEGLIGIBLE_PCT = 5.0


def _rank_key(by: str) -> str:
    """Which field orders a "heaviest" list.

    CPU% is already host-relative, so it ranks directly. Memory must rank on
    absolute bytes: see mem_share() for why MemPerc cannot be compared between
    containers.
    """
    return "cpu_pct" if by == "cpu" else "mem_bytes"


def _share(row: dict, by: str):
    """A container's share OF THE HOST for ``by``, or None when unknown.

    Falls back to limit-relative MemPerc when mem_pct_host is absent, which
    happens for rows written by an older build and still sitting in
    `Instance.containers_json` until the next scan rewrites them. Treating a
    missing field as 0 instead would mute the alert's "which service" line for
    a cycle - a silent regression, and the worst kind.
    """
    if by == "cpu":
        return row.get("cpu_pct")
    host = row.get("mem_pct_host")
    return host if host is not None else row.get("mem_pct")


def stands_out(rows: list, by: str = "cpu") -> bool:
    """True when at least one container is busy enough to be worth naming.

    Measured against the host, so the floor means the same thing for memory as
    for CPU. A capped container sitting at 90% of its own 256 MiB limit is 0.3%
    of a 16 GB host and explains nothing about a host breach.
    """
    return any((_share(r, by) or 0.0) >= NEGLIGIBLE_PCT for r in rows)


def top_consumers(rows: list, by: str = "cpu", limit: int = 5) -> list:
    """The heaviest containers by ``cpu`` or ``mem``, biggest first.

    Used by the alert email to answer "which service caused this" in one line,
    rather than leaving the reader to scan a wall of `docker stats` output -
    so getting the ORDER wrong sends the reader to the wrong service, which is
    worse than saying nothing.
    """
    key = _rank_key(by)
    ranked = [r for r in rows if r.get(key) is not None]
    if not ranked and by != "cpu":
        # Host script too old to report MemUsage: fall back to MemPerc rather
        # than showing an empty list, and accept the mixed denominator.
        ranked = [r for r in rows if r.get("mem_pct") is not None]
        key = "mem_pct"
    ranked.sort(key=lambda r: r[key], reverse=True)
    return ranked[:limit]


def parse(output: str) -> tuple:
    """``(containers, error)`` from the remote script's `containers` output.

    Tab-separated on purpose: image names carry ":" and "/", and Status carries
    spaces and parentheses, so neither is a safe delimiter.
    """
    text = output or ""
    rows, stats, error = [], {}, None
    for line in text.splitlines():
        parts = line.rstrip("\n").split("\t")
        if parts[0] in ("ERR", "WARN"):
            # WARN downgrades rather than blanks: `docker stats` can fail while
            # `docker ps` succeeded, leaving a usable list without usage figures.
            error = parts[1] if len(parts) > 1 else "unknown error"
            continue
        if parts[0] == "STAT" and len(parts) >= 5:
            usage = parts[4].strip()
            used, cap = _usage_pair(usage)
            stats[parts[1]] = {
                "cpu_pct": _pct(parts[2]),
                # Kept for continuity, but it is limit-relative - read
                # mem_pct_host instead. See mem_share().
                "mem_pct": _pct(parts[3]),
                "mem_usage": usage,
                "mem_bytes": used,
                "mem_limit_bytes": cap,
            }
            continue
        if parts[0] != "CTR" or len(parts) < 5:
            continue
        name, image, state, status = parts[1], parts[2], parts[3], parts[4]
        ports = parts[5] if len(parts) > 5 else ""
        rows.append({
            "name": name, "image": image, "state": state, "status": status,
            "health": classify(state, status), "ports": ports,
            # Filled from the STAT lines below; a stopped container has none.
            "cpu_pct": None, "mem_pct": None, "mem_usage": "",
            "mem_bytes": None, "mem_limit_bytes": None, "mem_pct_host": None,
        })
    for row in rows:
        row.update(stats.get(row["name"], {}))

    # One comparable memory figure per container, as a share of the whole box.
    total = host_mem_bytes(rows)
    if total:
        for row in rows:
            used = row.get("mem_bytes")
            if used is not None:
                row["mem_pct_host"] = round(used / total * 100, 2)
    if not rows and error is None and text.strip() and _SENTINEL not in text:
        # Output, but not OUR output: an outdated host script (see _SENTINEL).
        error = _STALE_SCRIPT
    # Problems first, then the heaviest consumer - which is the question being
    # asked when a host spikes: not "what is running" but "what is eating it".
    rows.sort(key=lambda c: (c["health"] not in ("unhealthy", "stopped", "restarting"),
                             -max(c["cpu_pct"] or 0.0,
                                  c["mem_pct_host"] if c["mem_pct_host"] is not None
                                  else (c["mem_pct"] or 0.0)),
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
    wanted = [t[0] for t in targets]

    # End the read transaction BEFORE the SSH round trips. Collection takes tens
    # of seconds, and a transaction left open across it pins its snapshot - which
    # stops VACUUM reclaiming the rows prune_history() deletes every cycle, so the
    # two largest tables bloat steadily. Nothing has been written yet, so there is
    # nothing to lose by ending it here.
    db.session.rollback()

    workers = min(config.SCAN_CONCURRENCY, len(targets))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_collect_one, targets))

    # Re-read for the write phase: the objects above belong to the transaction
    # just ended.
    by_id = {i.id: i for i in Instance.query.filter(Instance.id.in_(wanted)).all()}
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


def for_alert(scan: dict) -> dict:
    """Top CPU/memory consumers on the instances implicated in this scan.

    Reads the rows collected on the previous cycle rather than re-running
    `docker stats` now: during a sustained breach a five-minute-old sample is
    representative, and the alert path should not grow an SSH round trip per host
    for something the scan already has. ``updated_at`` travels with it so the
    email can be honest about how fresh the figures are.

    Must run inside a Flask app context.
    """
    from models import Instance

    ids = {b.get("instance_id") for b in scan.get("breaches", []) if b.get("instance_id")}
    ids |= {s.get("instance_id") for s in scan.get("services_down", []) if s.get("instance_id")}
    if not ids:
        return {}

    out = {}
    for inst in Instance.query.filter(Instance.id.in_(list(ids))).all():
        rows = inst.containers_json or []
        if not rows:
            continue
        out[inst.name] = {
            "updated_at": (inst.containers_updated_at.isoformat()
                           if inst.containers_updated_at else None),
            "cpu": top_consumers(rows, "cpu", 5),
            "mem": top_consumers(rows, "mem", 5),
        }
    return out
