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


#: Decisions that told someone about a problem (so a recovery is worth sending).
_PROBLEM_REASONS = ("new", "ongoing", "renotify")


def decide(
    recent_key_sets,
    last_alert,
    now: datetime,
    renotify_minutes: int,
    alert_on_recovery: bool,
    confirm_scans: int = 2,
) -> str | None:
    """Return an alert reason string, or None to stay silent this cycle.

    ``recent_key_sets`` holds the problem-key sets (breached metrics + down
    services) for the most recent scans, **newest first** — supply at least
    ``confirm_scans + 1`` of them.

    A problem must appear in ``confirm_scans`` consecutive scans before it counts
    as real. That is the flap guard: a metric oscillating across its threshold
    would otherwise emit a "new" + "recovered" pair on every cycle. With the
    default of 2 and a 5-minute interval, a breach must hold for ~10 minutes
    before anyone is emailed, and must be clear for ~10 minutes to count as
    recovered.
    """
    sets = [set(s) for s in (recent_key_sets or [])]
    confirm = max(1, int(confirm_scans))
    if len(sets) < confirm:
        return None                     # not enough history to confirm anything

    last_at, last_reason = last_alert if last_alert else (None, None)
    # Stored reasons look like "new" or "new: <transport error>" — take the verb.
    last_decision = (last_reason or "").split(":", 1)[0].strip().lower()

    window = sets[0:confirm]
    sustained = set.intersection(*window)   # present in EVERY recent scan
    any_recent = set.union(*window)         # present in ANY recent scan

    if not any_recent:
        # Nothing wrong at all for `confirm` consecutive scans => confirmed clear.
        # Only worth an email if we actually announced a problem earlier.
        if alert_on_recovery and last_decision in _PROBLEM_REASONS:
            return "recovered"
        return None

    if not sustained:
        # Something is crossing the threshold intermittently but nothing has held
        # for `confirm` scans. This is the flapping case — stay silent.
        return None

    prev_window = sets[1:confirm + 1]
    prev_sustained = (set.intersection(*prev_window)
                      if len(prev_window) == confirm else set())

    # A newly sustained problem is worth an immediate alert.
    if sustained - prev_sustained:
        return "new"

    # Same sustained problems as before: only a periodic reminder.
    if last_at is None:
        return "ongoing"
    elapsed_min = (now - last_at).total_seconds() / 60.0
    if elapsed_min >= renotify_minutes:
        return "renotify"
    return None
