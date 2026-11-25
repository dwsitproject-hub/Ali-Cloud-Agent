"""A hole in the chart must say which kind of hole it is.

"The metric stopped reporting" is a problem with one agent. "No scan ran" means
the monitor itself was absent and has nothing to say about that window for ANY
instance - the worse of the two, and the one that read as "nothing happened" on
2026-11-25. Both render as a break in the line, so the data has to distinguish
them.
"""
from datetime import datetime, timedelta, timezone

import pytest

from db import db
from models import Instance, InstanceGroup, MetricResult, ScanRun
from tests.conftest import make_app

INST = "i-gap-test"


@pytest.fixture
def app():
    a = make_app()
    a.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    yield a
    with a.app_context():
        runs = (ScanRun.query.join(MetricResult, MetricResult.scan_run_id == ScanRun.id)
                .filter(MetricResult.instance_id == INST).all())
        for r in runs:
            db.session.delete(r)
        inst = db.session.get(Instance, INST)
        if inst is not None:
            db.session.delete(inst)
        db.session.commit()


@pytest.fixture
def client(app):
    c = app.test_client()
    assert c.post("/auth/dev-login").status_code in (200, 302, 303)
    return c


def _seed(app, entries):
    """``entries`` are ``(minutes_ago, value)``; value None == scan ran, no datapoint.

    A minute with no entry at all is a minute in which no scan ran.
    """
    with app.app_context():
        group = InstanceGroup.query.first()
        if group is None:
            group = InstanceGroup(name="GapTest", sort_order=97)
            db.session.add(group)
            db.session.flush()
        if db.session.get(Instance, INST) is None:
            db.session.add(Instance(id=INST, name="Gap Test", role="backend",
                                    host="", group_id=group.id, enabled=False))
        now = datetime.now(timezone.utc)
        for minutes_ago, value in entries:
            when = now - timedelta(minutes=minutes_ago)
            run = ScanRun(scanned_at=when.isoformat(), mode="mock", total=1,
                          breach_count=0, services_total=0, services_down_count=0,
                          created_at=when)
            db.session.add(run)
            db.session.flush()
            db.session.add(MetricResult(
                scan_run_id=run.id, key=INST + "_cpu", group_name="GapTest",
                instance_id=INST, instance_name="Gap Test", label="CPU",
                value=value, unit="%", threshold=80.0, comparison=">",
                breached=False, source="mock"))
        db.session.commit()


def _cpu(payload):
    return next(s for s in payload["series"] if s["label"] == "CPU")


def test_a_scan_that_ran_with_no_datapoint_is_not_a_monitoring_gap(app, client):
    """The agent went quiet; the monitor was there. s > 0, v is None."""
    _seed(app, [(10, 40.0), (5, None)])
    r = client.get("/api/metrics/history?instance_id=%s&hours=1&points=240" % INST)
    assert r.status_code == 200
    body = r.get_json()
    pts = [p for p in _cpu(body)["points"] if p["s"] > 0]
    assert any(p["v"] is None for p in pts), "the blank datapoint disappeared"
    # Those buckets had a scan, so they are not in gaps.
    assert all(p["s"] > 0 for p in pts)


def test_buckets_with_no_scan_are_reported_as_gaps(app, client):
    """Nothing written for a stretch: s == 0, and it lands in `gaps`."""
    _seed(app, [(50, 40.0), (5, 41.0)])
    body = client.get(
        "/api/metrics/history?instance_id=%s&hours=1&points=240" % INST).get_json()
    assert body["gaps"], "a 45-minute hole was not reported"
    biggest = max(body["gaps"], key=lambda g: g["minutes"])
    assert biggest["minutes"] >= 30
    assert biggest["buckets"] >= 6
    # Every point inside a gap says so.
    assert any(p["s"] == 0 and p["v"] is None for p in _cpu(body)["points"])


def test_a_fully_covered_window_reports_no_gaps(app, client):
    _seed(app, [(m, 40.0) for m in range(0, 30, 5)])
    body = client.get(
        "/api/metrics/history?instance_id=%s&hours=0.4&points=240" % INST).get_json()
    inside = [g for g in body["gaps"] if g["minutes"] > 5]
    assert not inside, "contiguous scans reported as a gap: %s" % inside


def test_gap_window_covers_the_whole_bucket(app, client):
    """`to` is the far edge of the last empty bucket, not its start.

    Reported as the start, a one-bucket gap would read as zero minutes long.
    """
    _seed(app, [(50, 40.0), (5, 41.0)])
    body = client.get(
        "/api/metrics/history?instance_id=%s&hours=1&points=240" % INST).get_json()
    for g in body["gaps"]:
        assert g["minutes"] > 0
        assert g["to"] > g["from"]


def test_scan_count_is_independent_of_this_instance(app, client):
    """A scan that wrote no row for this instance still counts as a scan.

    Otherwise a newly-added instance looks like a monitoring outage for all the
    history before it existed.
    """
    with app.app_context():
        now = datetime.now(timezone.utc)
        other = ScanRun(scanned_at=now.isoformat(), mode="mock", total=0,
                        breach_count=0, services_total=0, services_down_count=0,
                        created_at=now - timedelta(minutes=20))
        db.session.add(other)
        db.session.commit()
        other_id = other.id
    try:
        _seed(app, [(5, 40.0)])
        body = client.get(
            "/api/metrics/history?instance_id=%s&hours=1&points=240" % INST).get_json()
        pts = _cpu(body)["points"]
        # The bucket ~20 min back holds a scan but no row for this instance:
        # v is None, yet s > 0, so it must not be counted as a monitoring gap.
        assert any(p["s"] > 0 and p["v"] is None for p in pts)
    finally:
        with app.app_context():
            run = db.session.get(ScanRun, other_id)
            if run is not None:
                db.session.delete(run)
                db.session.commit()
