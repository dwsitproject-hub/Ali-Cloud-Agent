"""The monitor must report the window in which it was not watching.

On 2026-11-25 Backend Production breached memory at 92.18% while the monitor
hosted on that same box was hung. Its history showed a hole, which reads exactly
like "nothing happened" - and that is how it was first read. Only the peer
monitor on another host had the record.

The monitor cannot observe its own outage, but it CAN notice, on the first scan
afterwards, that it was away - and say so, rather than quietly resuming.
"""
from datetime import datetime, timedelta, timezone

import pytest

import config
import scheduler
from agents import agent2_alerter
from db import db
from models import ScanRun
from tests.conftest import make_app

NOW = datetime(2026, 11, 25, 7, 35, tzinfo=timezone.utc)


# --- detection --------------------------------------------------------------
def test_a_normal_cycle_is_not_a_gap():
    previous = NOW - timedelta(minutes=config.SCAN_INTERVAL_MINUTES)
    assert scheduler.detect_scan_gap(previous, NOW) is None


def test_a_slightly_late_cycle_is_not_a_gap():
    """A cycle that overran must not page anyone."""
    previous = NOW - timedelta(minutes=config.SCAN_INTERVAL_MINUTES * 2)
    assert scheduler.detect_scan_gap(previous, NOW) is None


def test_the_real_outage_is_a_gap():
    """07:00 to 07:35 with a 5-minute interval: the actual incident."""
    previous = datetime(2026, 11, 25, 7, 0, tzinfo=timezone.utc)
    gap = scheduler.detect_scan_gap(previous, NOW)
    assert gap is not None
    assert gap["minutes"] == 35.0
    assert gap["missed_scans"] == 6
    assert gap["from"].startswith("2026-11-25T07:00")


def test_first_scan_of_a_fresh_database_is_not_a_gap():
    """No previous run means a first start, not an outage."""
    assert scheduler.detect_scan_gap(None, NOW) is None


def test_missed_scans_is_never_zero_for_a_real_gap():
    """A gap just past the threshold still names at least one missed scan."""
    previous = NOW - timedelta(minutes=config.SCAN_INTERVAL_MINUTES * 3 + 1)
    gap = scheduler.detect_scan_gap(previous, NOW)
    assert gap and gap["missed_scans"] >= 1


# --- the email ---------------------------------------------------------------
def _scan(**kw):
    base = {"scanned_at": NOW.isoformat(), "mode": "live", "total": 3,
            "breach_count": 0, "results": [], "breaches": [],
            "services": [], "services_down": [], "services_down_count": 0,
            "services_total": 0}
    base.update(kw)
    return base


def test_a_gap_alone_does_not_send_an_all_clear():
    """The dangerous failure mode: "healthy" for a window nobody observed."""
    gap = {"from": "2026-11-25T07:00:00+00:00", "to": "2026-11-25T07:35:00+00:00",
           "minutes": 35.0, "missed_scans": 6}
    email = agent2_alerter.build_email(_scan(monitoring_gap=gap))
    assert "All systems healthy" not in email["subject"]
    assert "monitoring gap 35.0 min" in email["subject"]
    assert "no scan ran" in email["html_body"]
    assert "07:00:00" in email["html_body"] and "07:35:00" in email["html_body"]


def test_a_gap_leads_the_subject_ahead_of_a_breach():
    """It qualifies every other figure in the email, so it comes first."""
    gap = {"from": "a", "to": "b", "minutes": 35.0, "missed_scans": 6}
    breach = {"instance_name": "Backend Production", "label": "Memory",
              "value": 92.18, "unit": "%", "threshold": 85.0, "comparison": ">"}
    email = agent2_alerter.build_email(_scan(monitoring_gap=gap, breaches=[breach],
                                             breach_count=1))
    subject = email["subject"]
    assert subject.index("monitoring gap") < subject.index("threshold breach")


def test_no_gap_leaves_the_email_unchanged():
    email = agent2_alerter.build_email(_scan())
    assert "All systems healthy" in email["subject"]
    assert "Monitoring gap" not in email["html_body"]


# --- state.last_scan_at ------------------------------------------------------
@pytest.fixture
def app():
    a = make_app()
    a.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return a


def test_last_scan_at_is_timezone_aware(app):
    """A naive timestamp would make the subtraction in detect_scan_gap raise."""
    import state
    with app.app_context():
        run = ScanRun(scanned_at=NOW.isoformat(), mode="mock", total=0,
                      breach_count=0, services_total=0, services_down_count=0,
                      created_at=NOW)
        db.session.add(run)
        db.session.commit()
        try:
            at = state.last_scan_at()
            assert at is not None and at.tzinfo is not None
            # The thing that matters: this must not raise.
            assert scheduler.detect_scan_gap(at, at + timedelta(hours=1)) is not None
        finally:
            db.session.delete(run)
            db.session.commit()
