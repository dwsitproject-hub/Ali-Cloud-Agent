"""Auto-alert flap/flood-control decision logic (pure, no DB)."""
from datetime import datetime, timedelta, timezone

import alerting

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


def _decide(current, prev, last_alert, renotify=60, recovery=True):
    return alerting.decide(current, prev, last_alert, NOW, renotify, recovery)


def test_new_problem_alerts():
    assert _decide({"a_cpu"}, set(), None) == "new"


def test_escalation_new_key_while_others_ongoing_alerts():
    # b_cpu is new even though a_cpu was already breaching.
    assert _decide({"a_cpu", "b_cpu"}, {"a_cpu"}, NOW) == "new"


def test_ongoing_same_problem_is_silent_before_renotify():
    last = NOW - timedelta(minutes=30)  # < 60 min renotify window
    assert _decide({"a_cpu"}, {"a_cpu"}, last) is None


def test_ongoing_renotifies_after_interval():
    last = NOW - timedelta(minutes=90)  # > 60 min
    assert _decide({"a_cpu"}, {"a_cpu"}, last) == "renotify"


def test_ongoing_with_no_prior_alert_notifies_once():
    # Same as previous cycle but we never actually alerted yet.
    assert _decide({"a_cpu"}, {"a_cpu"}, None) == "ongoing"


def test_recovery_fires_once():
    assert _decide(set(), {"a_cpu"}, NOW - timedelta(minutes=1)) == "recovered"


def test_recovery_suppressed_when_disabled():
    assert _decide(set(), {"a_cpu"}, None, recovery=False) is None


def test_steady_state_all_clear_is_silent():
    # No problems now, none before -> nothing to say.
    assert _decide(set(), set(), None) is None
