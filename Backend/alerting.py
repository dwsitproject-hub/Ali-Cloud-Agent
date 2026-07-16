"""Auto-alert decision logic (flap / flood control).

The scheduler used to email on *every* cycle that had a breach or outage, so a
metric stuck above its threshold produced an alert every SCAN_INTERVAL_MINUTES.
``decide()`` replaces that with transition-based alerting:

  * "new"       - a problem key appears that wasn't in the previous cycle
                  (first detection or escalation) -> alert
  * "recovered" - all problems cleared this cycle (previous cycle had some)
                  -> send a single all-clear, if ALERT_ON_RECOVERY
  * "renotify"  - the same problem set persists and the re-notify interval has
                  elapsed since the last auto-alert -> reminder
  * None        - nothing actionable this cycle (stay quiet)

Kept pure (no DB / no clock of its own) so it is trivially unit-testable; the
scheduler supplies the current/previous problem keys, the last auto-alert time,
and ``now``.
"""
from __future__ import annotations

from datetime import datetime


def decide(
    current_keys,
    prev_keys,
    last_auto_alert_at: datetime | None,
    now: datetime,
    renotify_minutes: int,
    alert_on_recovery: bool,
) -> str | None:
    """Return an alert reason string, or None to stay silent this cycle.

    ``current_keys`` / ``prev_keys`` are sets of problem identifiers (breached
    metric keys + down service keys) for this cycle and the one before it.
    """
    current = set(current_keys)
    prev = set(prev_keys)

    if not current:
        return "recovered" if (prev and alert_on_recovery) else None

    # Any brand-new problem is always worth an immediate alert.
    if current - prev:
        return "new"

    # Same (or a subset of the) problems as last cycle: only a periodic reminder.
    if last_auto_alert_at is None:
        return "ongoing"
    elapsed_min = (now - last_auto_alert_at).total_seconds() / 60.0
    if elapsed_min >= renotify_minutes:
        return "renotify"
    return None
