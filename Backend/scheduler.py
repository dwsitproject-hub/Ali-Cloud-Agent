"""Scheduling layer.

Runs Agent 1 (CloudMonitor metrics) + service health checks every
SCAN_INTERVAL_MINUTES via APScheduler, stores the result in shared state, and -
when AUTO_ALERT_ON_BREACH is on - auto-triggers Agent 2 (DirectMail) whenever a
metric threshold is breached OR a service is down.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

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

    problems = scan["breach_count"] + scan["services_down_count"]
    if auto_alert and problems > 0:
        log.warning("problem detected (%s metric breach, %s service down) -> Agent 2",
                    scan["breach_count"], scan["services_down_count"])
        alert = agent2_alerter.send_alert(scan)
        alert["trigger"] = "auto"
        state.set_last_alert(alert)

    return scan


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
