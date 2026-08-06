"""Auto-alert flap/flood-control decision logic (pure, no DB).

``decide()`` takes problem-key sets for the most recent scans, NEWEST FIRST, and
requires a problem to persist across ``confirm_scans`` consecutive scans before it
is alerted — otherwise a metric oscillating across its threshold would email a
new+recovered pair every cycle.
"""
from datetime import datetime, timedelta, timezone

import alerting

NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)
CPU = "i-db_cpu"
MEM = "i-db_mem"


def _decide(sets, last_alert=None, renotify=60, recovery=True, confirm=2):
    """last_alert: (datetime, reason) of the previous auto alert, or None."""
    return alerting.decide(sets, last_alert, NOW, renotify, recovery, confirm)


# --- flap guard (the behaviour this exists for) ------------------------------
def test_single_scan_spike_is_silent():
    # Breached on this scan only — not yet confirmed, so no email.
    assert _decide([{CPU}, set(), set()]) is None


def test_flapping_across_threshold_is_silent():
    # on / off / on — never two consecutive, so nothing is confirmed.
    assert _decide([{CPU}, set(), {CPU}]) is None
    assert _decide([set(), {CPU}, set()]) is None


def test_sustained_breach_alerts_once_confirmed():
    # Present in the two most recent scans, absent before -> newly sustained.
    assert _decide([{CPU}, {CPU}, set()]) == "new"


def test_sustained_breach_does_not_realert_next_cycle():
    # Still sustained and already alerted recently -> stay quiet.
    assert _decide([{CPU}, {CPU}, {CPU}], last_alert=(NOW - timedelta(minutes=5), "new")) is None


# --- escalation / recovery --------------------------------------------------
def test_additional_sustained_problem_alerts():
    # MEM becomes sustained while CPU already was.
    assert _decide([{CPU, MEM}, {CPU, MEM}, {CPU}]) == "new"


def test_recovery_fires_once_clear_is_confirmed():
    # Clear for the last two scans, previously sustained.
    assert _decide([set(), set(), {CPU}, {CPU}],
                   last_alert=(NOW - timedelta(minutes=10), "new")) == "recovered"


def test_recovery_suppressed_when_disabled():
    assert _decide([set(), set(), {CPU}, {CPU}], recovery=False,
                   last_alert=(NOW - timedelta(minutes=10), "new")) is None


def test_steady_state_all_clear_is_silent():
    assert _decide([set(), set(), set()]) is None


# --- re-notify while ongoing ------------------------------------------------
def test_ongoing_renotifies_after_interval():
    assert _decide([{CPU}, {CPU}, {CPU}],
                   last_alert=(NOW - timedelta(minutes=90), "new")) == "renotify"


def test_ongoing_silent_before_interval():
    assert _decide([{CPU}, {CPU}, {CPU}],
                   last_alert=(NOW - timedelta(minutes=30), "new")) is None


def test_ongoing_with_no_prior_alert_notifies_once():
    assert _decide([{CPU}, {CPU}, {CPU}], last_alert=None) == "ongoing"


# --- edges ------------------------------------------------------------------
def test_insufficient_history_is_silent():
    # Fresh database: not enough scans to confirm anything yet.
    assert _decide([{CPU}]) is None
    assert _decide([]) is None


def test_confirm_one_restores_immediate_alerting():
    # confirm_scans=1 means alert on the first breaching scan (old behaviour).
    assert _decide([{CPU}, set()], confirm=1) == "new"


def test_recovery_not_repeated():
    # Already announced the recovery -> stay silent on subsequent clear scans.
    assert _decide([set(), set(), set()],
                   last_alert=(NOW - timedelta(minutes=5), "recovered")) is None


def test_no_recovery_if_nothing_was_announced():
    # Clear, but we never told anyone about a problem (e.g. it only flapped).
    assert _decide([set(), set(), {CPU}], last_alert=None) is None


def test_flapping_does_not_trigger_recovery():
    # on/off/on: nothing sustained, but also NOT confirmed clear -> silence.
    assert _decide([set(), {CPU}, set()],
                   last_alert=(NOW - timedelta(minutes=5), "new")) is None
