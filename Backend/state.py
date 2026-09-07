"""Persistent state for scan + alert results, backed by PostgreSQL.

Replaces the old in-memory store. ``set_last_scan``/``set_last_alert`` persist
to the DB as a side effect; ``snapshot()`` reconstructs the exact dict shapes the
dashboard JS expects (so /api/status and /api/send are unchanged). The live scan
dict returned by /api/scan is still the in-memory document built by the
scheduler - this module only persists and reconstructs.

Must be called inside a Flask application context (request handlers have one;
the scheduler's background job pushes one - see scheduler._scan_job).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import config
from db import db
from models import Alert, MetricResult, ScanRun, ServiceResult

HISTORY_LIMIT = 50


# --- persist ----------------------------------------------------------------
def set_last_scan(scan: dict) -> None:
    """Persist a scan document (metrics + services) as a ScanRun + child rows."""
    run = ScanRun(
        scanned_at=scan.get("scanned_at", ""),
        mode=scan.get("mode", "mock"),
        total=scan.get("total", 0),
        breach_count=scan.get("breach_count", 0),
        services_total=scan.get("services_total", 0),
        services_down_count=scan.get("services_down_count", 0),
    )
    db.session.add(run)
    db.session.flush()  # assign run.id

    for r in scan.get("results", []):
        db.session.add(MetricResult(
            scan_run_id=run.id,
            key=r.get("key"), group_name=r.get("group"),
            instance_id=r.get("instance_id"), instance_name=r.get("instance_name"),
            label=r.get("label"), namespace=r.get("namespace"),
            metric_name=r.get("metric_name"), stat=r.get("stat"),
            value=r.get("value"), unit=r.get("unit"),
            threshold=r.get("threshold"), comparison=r.get("comparison"),
            agent_required=r.get("agent_required"), breached=r.get("breached"),
            source=r.get("source"), error=r.get("error"),
        ))

    for s in scan.get("services", []):
        db.session.add(ServiceResult(
            scan_run_id=run.id,
            key=s.get("key"), group_name=s.get("group"),
            instance_id=s.get("instance_id"), instance_name=s.get("instance_name"),
            name=s.get("name"), svc_type=s.get("type"), target=s.get("target"),
            up=s.get("up"), latency_ms=s.get("latency_ms"),
            detail=s.get("detail"), error=s.get("error"), source=s.get("source"),
        ))

    db.session.commit()


def attach_diagnostics(diags: dict) -> None:
    """Store on-breach evidence on the most recent scan run.

    Diagnostics are gathered after the run is persisted (the alert decision comes
    first), so this updates the row in place. Best-effort: never raise into the
    scan cycle."""
    if not diags:
        return
    try:
        latest = ScanRun.query.order_by(ScanRun.id.desc()).first()
        if latest is not None:
            latest.diagnostics = diags
            db.session.commit()
    except Exception:
        db.session.rollback()


def set_last_alert(alert: dict) -> None:
    """Persist an alert send summary, linked to the most recent scan run."""
    latest = ScanRun.query.order_by(ScanRun.id.desc()).first()
    db.session.add(Alert(
        scan_run_id=latest.id if latest else None,
        subject=alert.get("subject"),
        recipients=alert.get("recipients", []),
        sent=bool(alert.get("sent")),
        mode=alert.get("mode"),
        trigger=alert.get("trigger"),
        reason=alert.get("reason"),
        sent_at=alert.get("sent_at"),
        preview_html=alert.get("preview_html"),
    ))
    db.session.commit()


# --- reconstruct ------------------------------------------------------------
def _metric_to_dict(m: MetricResult) -> dict:
    return {
        "key": m.key, "label": m.label, "group": m.group_name,
        "instance_id": m.instance_id, "instance_name": m.instance_name,
        "namespace": m.namespace, "metric_name": m.metric_name, "stat": m.stat,
        "value": m.value, "unit": m.unit, "threshold": m.threshold,
        "comparison": m.comparison, "agent_required": m.agent_required,
        "breached": m.breached, "source": m.source, "error": m.error,
    }


def _service_to_dict(s: ServiceResult) -> dict:
    return {
        "key": s.key, "group": s.group_name, "instance_id": s.instance_id,
        "instance_name": s.instance_name, "name": s.name, "type": s.svc_type,
        "target": s.target, "up": s.up, "latency_ms": s.latency_ms,
        "detail": s.detail, "error": s.error, "source": s.source,
    }


def _scan_run_to_dict(run: ScanRun) -> dict:
    """Rebuild the scan document the dashboard + Agent 2 consume."""
    results = [_metric_to_dict(m) for m in run.metric_results]
    services = [_service_to_dict(s) for s in run.service_results]
    return {
        "scanned_at": run.scanned_at,
        "mode": run.mode,
        "total": run.total,
        "breach_count": run.breach_count,
        "results": results,
        # build_email() reads these back when /api/send re-renders the last scan.
        "breaches": [r for r in results if r["breached"]],
        "services": services,
        "services_down": [s for s in services if s["up"] is False],
        "services_down_count": run.services_down_count,
        "services_total": run.services_total,
        # On-breach SSH evidence, so the dashboard can show what the email shows.
        "diagnostics": run.diagnostics or {},
    }


def _alert_to_dict(a: Alert) -> dict:
    return {
        "subject": a.subject, "recipients": a.recipients or [],
        "sent": a.sent, "mode": a.mode, "trigger": a.trigger,
        "reason": a.reason, "sent_at": a.sent_at, "preview_html": a.preview_html,
    }


# --- alerting support -------------------------------------------------------
def recent_problem_key_sets(n: int) -> list:
    """Problem-key sets for the last ``n`` scan runs, newest first.

    A "problem key" is a breached metric key, a down service key, or - when
    ALERT_ON_NO_DATA is on - a ``<key>:nodata`` marker for a metric that STOPPED
    reporting. That last one exists because a metric with no value is not
    compared against its threshold, so it used to count as healthy: Backend
    Staging sat at 95% memory for an hour without alerting, because the value kept
    coming back empty.

    "Stopped" means null now but non-null in the run before, so a box with no
    CloudMonitor agent (memory/disk permanently blank) never triggers it. One
    extra run is fetched to judge the oldest set. alerting.decide then applies the
    usual ALERT_CONFIRM_SCANS hysteresis, so a single missed datapoint stays quiet.
    """
    want = max(1, n)
    runs = ScanRun.query.order_by(ScanRun.id.desc()).limit(want + 1).all()
    out = []
    for i, r in enumerate(runs[:want]):
        keys = {m.key for m in r.metric_results if m.breached and m.key}
        keys |= {s.key for s in r.service_results if s.up is False and s.key}
        if config.ALERT_ON_NO_DATA and i + 1 < len(runs):
            reported_before = {m.key for m in runs[i + 1].metric_results
                               if m.value is not None and m.key}
            keys |= {f"{m.key}:nodata" for m in r.metric_results
                     if m.value is None and m.key and m.key in reported_before}
        out.append(keys)
    return out


def stopped_reporting() -> list:
    """Metrics in the latest run that were reporting in the previous one.

    Feeds the alert email so a "stopped reporting" alert can name what went
    quiet, instead of arriving with an empty problem list.
    """
    runs = ScanRun.query.order_by(ScanRun.id.desc()).limit(2).all()
    if len(runs) < 2:
        return []
    reported_before = {m.key for m in runs[1].metric_results
                       if m.value is not None and m.key}
    return [{"key": m.key, "label": m.label, "instance_id": m.instance_id,
             "instance_name": m.instance_name, "group": m.group_name,
             "error": m.error}
            for m in runs[0].metric_results
            if m.value is None and m.key and m.key in reported_before]


def previous_problem_keys() -> set:
    """Problem keys from the scan run immediately BEFORE the latest one.

    "Problem keys" = breached metric keys + down service keys. The current
    cycle's run is already persisted by the time the scheduler asks, so the
    previous run is the second-most-recent. Empty set if there is no prior run.
    """
    runs = ScanRun.query.order_by(ScanRun.id.desc()).limit(2).all()
    if len(runs) < 2:
        return set()
    prev = runs[1]
    keys = {m.key for m in prev.metric_results if m.breached and m.key}
    keys |= {s.key for s in prev.service_results if s.up is False and s.key}
    return keys


def last_auto_alert() -> tuple:
    """``(created_at, reason)`` of the most recent AUTO alert, or ``(None, None)``.

    The reason tells decide() whether we already announced a problem (so a
    recovery is worth sending) or already announced the recovery."""
    a = (Alert.query.filter(Alert.trigger == "auto")
         .order_by(Alert.id.desc()).first())
    return (a.created_at, a.reason) if a else (None, None)


def last_auto_alert_at() -> datetime | None:
    """Timestamp of the most recent AUTO alert (drives the re-notify clock).

    Manual sends don't reset the reminder cadence, so only ``trigger='auto'``
    rows count.
    """
    a = (Alert.query.filter(Alert.trigger == "auto")
         .order_by(Alert.id.desc()).first())
    return a.created_at if a else None


def prune_history(retention_days: int) -> int:
    """Delete scan runs (and cascaded child rows) older than the retention
    window. Returns the number of runs removed. No-op when retention_days<=0.
    """
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    # Delete via the ORM (not a bulk query) so the relationship cascade removes
    # child metric/service/alert rows — the FKs have no ON DELETE CASCADE. Only
    # a handful of runs age past the boundary each cycle, so this stays cheap.
    old = ScanRun.query.filter(ScanRun.created_at < cutoff).all()
    for run in old:
        db.session.delete(run)
    if old:
        db.session.commit()
    return len(old)


def snapshot() -> dict:
    """Latest scan + latest alert + recent history, in the legacy dict shape."""
    last_run = ScanRun.query.order_by(ScanRun.id.desc()).first()
    last_alert = Alert.query.order_by(Alert.id.desc()).first()
    history_runs = (
        ScanRun.query.order_by(ScanRun.id.desc()).limit(HISTORY_LIMIT).all()
    )
    return {
        "last_scan": _scan_run_to_dict(last_run) if last_run else None,
        "last_alert": _alert_to_dict(last_alert) if last_alert else None,
        "history": [
            {"scanned_at": r.scanned_at, "mode": r.mode,
             "breach_count": r.breach_count, "total": r.total}
            for r in history_runs
        ],
    }
