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
    }


def _alert_to_dict(a: Alert) -> dict:
    return {
        "subject": a.subject, "recipients": a.recipients or [],
        "sent": a.sent, "mode": a.mode, "trigger": a.trigger,
        "reason": a.reason, "sent_at": a.sent_at, "preview_html": a.preview_html,
    }


# --- alerting support -------------------------------------------------------
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
