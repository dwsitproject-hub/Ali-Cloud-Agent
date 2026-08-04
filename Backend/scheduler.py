"""Scheduling layer.

Runs Agent 1 (CloudMonitor metrics) + service health checks every
SCAN_INTERVAL_MINUTES via APScheduler, stores the result in shared state, and -
when AUTO_ALERT_ON_BREACH is on - auto-triggers Agent 2 (DirectMail) whenever a
metric threshold is breached OR a service is down.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

import alerting
import config
import healthcheck
import state
from agents import agent1_scanner, agent2_alerter

log = logging.getLogger("scheduler")

_scheduler = None
_app = None  # Flask app captured at start(), for background-thread app contexts


def run_scan_job(auto_alert=None) -> dict:
    """One full cycle: scan metrics + run health checks, persist, maybe alert.

    Returns the combined scan document (metrics + services).
    """
    if auto_alert is None:
        auto_alert = config.AUTO_ALERT_ON_BREACH

    scan = agent1_scanner.run_scan()

    if config.HEALTHCHECK_ENABLED:
        health = healthcheck.run_health_checks()
        scan["services"] = health["results"]
        scan["services_down"] = health["down"]
        scan["services_down_count"] = health["down_count"]
        scan["services_total"] = health["total"]
    else:
        scan["services"] = []
        scan["services_down"] = []
        scan["services_down_count"] = 0
        scan["services_total"] = 0

    state.set_last_scan(scan)
    log.info("scan complete: mode=%s breaches=%s/%s services_down=%s/%s",
             scan["mode"], scan["breach_count"], scan["total"],
             scan["services_down_count"], scan["services_total"])

    if auto_alert:
        _maybe_auto_alert(scan)

    # Keep history bounded (runs every cycle; only rows past the retention
    # window are removed, so it is a cheap indexed delete most cycles).
    try:
        pruned = state.prune_history(config.HISTORY_RETENTION_DAYS)
        if pruned:
            log.info("pruned %s scan run(s) older than %s days",
                     pruned, config.HISTORY_RETENTION_DAYS)
    except Exception:  # pruning must never break a scan cycle
        log.exception("history pruning failed")

    return scan


def _maybe_auto_alert(scan: dict) -> None:
    """Decide whether this cycle warrants an email, and send if so.

    Transition-based (see alerting.decide): a new problem, a full recovery, or a
    periodic reminder — never one email per cycle for a stuck metric.
    """
    current_keys = {r["key"] for r in scan.get("results", [])
                    if r.get("breached") and r.get("key")}
    current_keys |= {s["key"] for s in scan.get("services_down", [])
                     if s.get("key")}

    reason = alerting.decide(
        current_keys,
        state.previous_problem_keys(),
        state.last_auto_alert_at(),
        datetime.now(timezone.utc),
        config.ALERT_RENOTIFY_MINUTES,
        config.ALERT_ON_RECOVERY,
    )
    if not reason:
        return

    log.warning("auto-alert (%s): %s metric breach, %s service down",
                reason, scan["breach_count"], scan["services_down_count"])

    # Identify WHICH service/query caused it, so the email carries evidence
    # rather than guesses. Never let a diagnostics failure block the alert.
    try:
        import diagnostics
        scan["diagnostics"] = diagnostics.collect_for_scan(scan)
    except Exception:
        log.exception("diagnostics collection failed; sending alert without it")

    alert = agent2_alerter.send_alert(scan)
    alert["trigger"] = "auto"
    alert["reason"] = reason if not alert.get("reason") else f"{reason}: {alert['reason']}"
    state.set_last_alert(alert)


def _scan_job() -> None:
    """APScheduler entry point. Runs in a background thread with no request
    context, so push the captured Flask app context for DB access."""
    if _app is not None:
        with _app.app_context():
            run_scan_job()
    else:
        run_scan_job()


def start(app=None) -> BackgroundScheduler:
    global _scheduler, _app
    _app = app
    if _scheduler is not None:
        return _scheduler

    from datetime import datetime

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        _scan_job,
        trigger="interval",
        minutes=config.SCAN_INTERVAL_MINUTES,
        id="cloudmonitor_scan",
        next_run_time=datetime.now(),  # run the FIRST scan immediately, in the background
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    log.info("scheduler started: scanning every %s min (auto_alert=%s, healthchecks=%s)",
             config.SCAN_INTERVAL_MINUTES, config.AUTO_ALERT_ON_BREACH, config.HEALTHCHECK_ENABLED)
    return _scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
