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
