"""Historical metric series for the dashboard charts.

  GET /api/metrics/history?instance_id=<id>&hours=1
  GET /api/metrics/history?instance_id=<id>&from=<iso>&to=<iso>

A card shows one instant. That is enough to answer "is it bad now" and useless
for "was it bad at 09:32, and for how long". The rows already exist -
`metric_results` keeps every value for `HISTORY_RETENTION_DAYS` - so this is a
read over data the scanner has been writing all along.

Three decisions worth stating, because each one has a tempting wrong answer:

* **Buckets keep the WORST value, never the mean.** A one-cycle spike to 81.55%
  averaged into an hour-long bucket reads as ~8% and disappears - and that spike
  is precisely the event this endpoint was built to let someone find. `>` rules
  keep the max, `<` rules keep the min, which is the same rule
  `agent1_scanner._pick_point()` already applies across disk devices.
* **A gap stays a gap.** A bucket with no datapoint returns ``null``, never 0 and
  never an interpolated value, so "the agent stopped reporting" and "usage was
  low" cannot look alike on a chart. Empty buckets are emitted rather than
  skipped so the time axis stays continuous and a monitoring outage is visible
  as a hole.
* **The bucket width is derived, not requested.** The caller asks for a range and
  roughly how many points it can draw; the server picks a width that is a whole
  multiple of the scan interval. Asking for 8640 points of a 30-day range is
  neither useful on screen nor cheap to serialise.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request
from sqlalchemy import func

import config
from db import db
from models import Instance, MetricResult, ScanRun

history_bp = Blueprint("history", __name__)

# Enough resolution for a wide screen, few enough points to stay responsive.
DEFAULT_POINTS = 240
MAX_POINTS = 1000
MAX_RANGE_DAYS = 400          # absurd-range guard, independent of retention


def _parse_iso(raw: str):
    """ISO-8601 from the browser (``...Z`` or an explicit offset) -> aware UTC."""
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    # A naive value (e.g. straight from <input type="datetime-local">) is taken
    # as UTC rather than rejected; the frontend sends offsets, but a hand-made
    # request should still get a sane answer instead of a 400.
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _window():
    """``(start, end)`` from either ``hours`` or ``from``/``to``."""
    now = datetime.now(timezone.utc)
    start = _parse_iso(request.args.get("from", ""))
    end = _parse_iso(request.args.get("to", "")) or now

    if start is None:
        try:
            hours = float(request.args.get("hours", "1"))
        except ValueError:
            hours = 1.0
        hours = min(max(hours, 1.0 / 60), MAX_RANGE_DAYS * 24)
        start = end - timedelta(hours=hours)

    if start >= end:
        start = end - timedelta(hours=1)
    if (end - start) > timedelta(days=MAX_RANGE_DAYS):
        start = end - timedelta(days=MAX_RANGE_DAYS)
    return start, end


def _bucket_seconds(start, end, points: int) -> int:
    """Bucket width: a whole number of scan intervals, >= one interval.

    Snapping to the scan interval keeps each bucket holding a whole number of
    scans, so bucket-to-bucket counts do not alternate 1,2,1,2 and make a flat
    series look like it is oscillating.
    """
    step = max(int(config.SCAN_INTERVAL_MINUTES) * 60, 60)
    span = max((end - start).total_seconds(), step)
    needed = span / max(points, 1)
    return int(math.ceil(needed / step)) * step if needed > step else step


def _series_meta(inst_id: str) -> dict:
    """``{label: {unit, threshold, comparison}}`` from this instance's newest rows.

    Read from the most recent rows rather than recomputed from config, so a chart
    draws the threshold the scanner actually applied.
    """
    recent = (
        MetricResult.query
        .filter(MetricResult.instance_id == inst_id)
        .order_by(MetricResult.id.desc())
        .limit(40).all()
    )
    meta = {}
    for row in recent:
        if row.label and row.label not in meta:
            meta[row.label] = {
                "unit": row.unit or "",
                "threshold": row.threshold,
                "comparison": row.comparison or ">",
            }
    return meta


@history_bp.route("/api/metrics/history", methods=["GET"])
def metrics_history():
    inst_id = (request.args.get("instance_id") or "").strip()
    if not inst_id:
        return jsonify(error="instance_id is required"), 400

    inst = db.session.get(Instance, inst_id)
    start, end = _window()

    try:
        points = int(request.args.get("points", DEFAULT_POINTS))
    except ValueError:
        points = DEFAULT_POINTS
    points = min(max(points, 10), MAX_POINTS)

    bucket = _bucket_seconds(start, end, points)

    # floor(epoch / bucket) * bucket -> the bucket's left edge, as a timestamp.
    edge = func.to_timestamp(
        func.floor(func.extract("epoch", ScanRun.created_at) / bucket) * bucket
    )

    # Both extremes per bucket; which one is "worst" depends on the comparison,
    # and that is decided per label below rather than branched into the SQL.
    # NOTE: group by the expressions themselves. A positional `GROUP BY 1,2`
    # errors once an aggregate shares the select list - a trap this codebase has
    # already hit twice.
    rows = (
        db.session.query(
            MetricResult.label.label("label"),
            edge.label("edge"),
            func.max(MetricResult.value).label("vmax"),
            func.min(MetricResult.value).label("vmin"),
            func.count(MetricResult.value).label("n"),
        )
        .join(ScanRun, ScanRun.id == MetricResult.scan_run_id)
        .filter(
            MetricResult.instance_id == inst_id,
            ScanRun.created_at >= start,
            ScanRun.created_at < end,
        )
        .group_by(MetricResult.label, edge)
        .order_by(edge)
        .all()
    )

    meta = _series_meta(inst_id)
    by_label = {}
    for r in rows:
        if not r.label:
            continue
        by_label.setdefault(r.label, {})[int(r.edge.timestamp())] = r

    # A continuous axis, so a stretch where the monitor itself was not running
    # shows as a hole instead of a line drawn straight across it.
    first = int(math.floor(start.timestamp() / bucket) * bucket)
    last = int(math.floor((end.timestamp() - 1) / bucket) * bucket)
    edges = list(range(first, last + bucket, bucket))

    series = []
    for label, points_by_edge in sorted(by_label.items()):
        info = meta.get(label, {})
        worst_is_max = (info.get("comparison") or ">") in (">", ">=")
        out = []
        for e in edges:
            row = points_by_edge.get(e)
            value = None
            count = 0
            if row is not None and row.n:
                value = row.vmax if worst_is_max else row.vmin
                count = int(row.n)
            out.append({
                "t": datetime.fromtimestamp(e, timezone.utc).isoformat(),
                "v": round(value, 2) if value is not None else None,
                "n": count,
            })
        series.append({
            "label": label,
            "unit": info.get("unit", ""),
            "threshold": info.get("threshold"),
            "comparison": info.get("comparison", ">"),
            "aggregate": "max" if worst_is_max else "min",
            "points": out,
        })

    return jsonify(
        instance_id=inst_id,
        instance_name=inst.name if inst else inst_id,
        **{"from": start.isoformat(), "to": end.isoformat()},
        bucket_seconds=bucket,
        interval_minutes=config.SCAN_INTERVAL_MINUTES,
        retention_days=config.HISTORY_RETENTION_DAYS,
        series=series,
    )
