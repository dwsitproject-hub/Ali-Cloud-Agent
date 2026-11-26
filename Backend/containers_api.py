"""Per-container usage over a window, and a memory limit sized from it.

  GET /api/containers/usage?instance_id=<id>&days=14

Setting a `mem_limit` from one reading is a coin flip in both directions: too
high and it protects nothing, too low and it converts a healthy service into an
OOM-kill loop. This reads `container_samples` - one row per container per scan -
and reports the distribution, so the number comes from evidence.

What it reports and why:

* **peak** is the figure a limit must clear. A cap below the observed maximum
  will be hit.
* **p95/p99** say whether the peak is representative or a single excursion. A
  peak far above p99 is one event worth understanding before it is designed
  around.
* **suggested** is peak x `HEADROOM`, rounded up to a round step, floored at
  `MIN_LIMIT`. A suggestion, not an answer: a service whose sampled fortnight
  never included its month-end job will be sized for the quiet weeks.
* **samples** is how much the figures are worth. A handful of rows is not a
  fortnight, and the response says so rather than letting a confident-looking
  number stand on four observations.

Node services need a second step the container limit cannot do on its own: V8
sizes its heap from the HOST's memory unless `--max-old-space-size` is set, so a
node process under a 2 GB cap still believes it may grow to many GB, never GCs
hard enough, and is killed by the kernel instead of collecting. The response
carries that note.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request
from sqlalchemy import func

import config
from db import db
from models import ContainerSample, Instance

containers_api_bp = Blueprint("containers_api", __name__)

MIB = 1024 ** 2
DEFAULT_DAYS = 14
MAX_DAYS = 90
#: A cap this far above the observed peak absorbs normal variation while still
#: catching a runaway. Below ~1.3 the first busy day pages someone.
HEADROOM = 1.5
MIN_LIMIT = 128 * MIB
#: Enough samples to believe a peak. Under this the response says so.
ENOUGH_SAMPLES = 200


def _round_up(value: float) -> int:
    """Up to a step an operator would actually type into a compose file."""
    if value <= 0:
        return MIN_LIMIT
    for step in (64, 128, 256, 512, 1024, 2048, 4096):
        step_b = step * MIB
        if value <= step_b * 8:
            return int(-(-value // step_b) * step_b)
    step_b = 4096 * MIB
    return int(-(-value // step_b) * step_b)


@containers_api_bp.route("/api/containers/usage", methods=["GET"])
def container_usage():
    inst_id = (request.args.get("instance_id") or "").strip()
    if not inst_id:
        return jsonify(error="instance_id is required"), 400

    try:
        days = float(request.args.get("days", DEFAULT_DAYS))
    except ValueError:
        days = DEFAULT_DAYS
    days = min(max(days, 0.01), MAX_DAYS)

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)

    inst = db.session.get(Instance, inst_id)

    # NOTE: group by the column itself. A positional `GROUP BY 1` errors once an
    # aggregate shares the select list - a trap this codebase has hit twice.
    mem = ContainerSample.mem_bytes
    cpu = ContainerSample.cpu_pct
    rows = (
        db.session.query(
            ContainerSample.name.label("name"),
            func.count(ContainerSample.id).label("samples"),
            func.count(mem).label("mem_samples"),
            func.max(mem).label("mem_max"),
            func.percentile_cont(0.99).within_group(mem.asc()).label("mem_p99"),
            func.percentile_cont(0.95).within_group(mem.asc()).label("mem_p95"),
            func.percentile_cont(0.50).within_group(mem.asc()).label("mem_med"),
            func.max(cpu).label("cpu_max"),
            func.percentile_cont(0.95).within_group(cpu.asc()).label("cpu_p95"),
            func.max(ContainerSample.mem_limit_bytes).label("limit_seen"),
        )
        .filter(ContainerSample.instance_id == inst_id,
                ContainerSample.sampled_at >= start)
        .group_by(ContainerSample.name)
        .order_by(func.max(mem).desc().nullslast())
        .all()
    )

    # Host RAM read back out of docker's own output: an uncapped container's
    # ceiling IS the machine, so a limit equal to it means "no limit set". Taken
    # from the live container list, which is self-correcting across a resize in a
    # way the cached spec is not.
    host_bytes = None
    try:
        import containers as containers_mod
        host_bytes = containers_mod.host_mem_bytes(
            (inst.containers_json or []) if inst else [])
    except Exception:
        host_bytes = None

    def capped(limit):
        """A limit within 2% of host RAM is docker reporting "uncapped"."""
        if not limit:
            return None
        if host_bytes and abs(limit - host_bytes) <= host_bytes * 0.02:
            return None
        return int(limit)

    out = []
    for r in rows:
        peak = int(r.mem_max) if r.mem_max is not None else None
        current = capped(r.limit_seen)
        suggested = _round_up(peak * HEADROOM) if peak else None
        out.append({
            "name": r.name,
            "samples": int(r.samples or 0),
            "mem_samples": int(r.mem_samples or 0),
            "mem_peak_mib": round(peak / MIB, 1) if peak else None,
            "mem_p99_mib": round(r.mem_p99 / MIB, 1) if r.mem_p99 else None,
            "mem_p95_mib": round(r.mem_p95 / MIB, 1) if r.mem_p95 else None,
            "mem_median_mib": round(r.mem_med / MIB, 1) if r.mem_med else None,
            "cpu_peak_pct": round(r.cpu_max, 1) if r.cpu_max is not None else None,
            "cpu_p95_pct": round(r.cpu_p95, 1) if r.cpu_p95 is not None else None,
            "current_limit_mib": round(current / MIB) if current else None,
            "suggested_limit_mib": round(suggested / MIB) if suggested else None,
            # A peak well above p99 is one excursion, not a working set.
            "peak_is_an_outlier": bool(
                peak and r.mem_p99 and peak > r.mem_p99 * 1.5),
            "enough_samples": int(r.mem_samples or 0) >= ENOUGH_SAMPLES,
        })

    total_suggested = sum(c["suggested_limit_mib"] or 0 for c in out)
    host_mib = round(host_bytes / MIB) if host_bytes else None
    return jsonify(
        instance_id=inst_id,
        instance_name=inst.name if inst else inst_id,
        **{"from": start.isoformat(), "to": now.isoformat()},
        days=days,
        host_mem_mib=host_mib,
        containers=len(out),
        sum_suggested_mib=total_suggested,
        # Limits are caps, not reservations, so the sum may exceed host RAM -
        # but if every service claimed its cap at once the host would die, so
        # the ratio is worth seeing beside the table.
        sum_suggested_vs_host=(round(total_suggested / host_mib, 2)
                               if host_mib else None),
        retention_days=config.CONTAINER_HISTORY_DAYS,
        headroom=HEADROOM,
        note=("suggested = peak x %s, rounded up. A container limit alone does not "
              "bound a Node process: V8 sizes its heap from HOST memory unless "
              "NODE_OPTIONS=--max-old-space-size is set below the cap, so it will "
              "grow past the limit and be OOM-killed rather than collect."
              % HEADROOM),
        usage=out,
    )
