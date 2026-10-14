"""The history endpoint must preserve spikes and gaps, not smooth them away.

The feature exists because a one-cycle CPU spike to 81.55% was invisible on a
dashboard that only shows the current instant. Two ways to make it invisible
again: average it into a bucket, or draw a line straight across a stretch where
the metric was not reporting. Both are tested here, because both are the
"obvious" implementation someone may reach for later.
"""
from datetime import datetime, timedelta, timezone

import pytest

from db import db
from models import Instance, InstanceGroup, MetricResult, ScanRun
from tests.conftest import make_app

INST = "i-hist-test"


def _seed(app, values, label="CPU", threshold=80.0, comparison=">"):
    """Write one scan run per value, five minutes apart, oldest first.

    ``None`` writes a row with no value - what the scanner records when
    CloudMonitor returns an empty datapoint list.
    """
    with app.app_context():
        group = InstanceGroup.query.filter_by(name="HistTest").first()
        if group is None:
            group = InstanceGroup(name="HistTest", sort_order=99)
            db.session.add(group)
            db.session.flush()
        if db.session.get(Instance, INST) is None:
            db.session.add(Instance(id=INST, name="History Test", role="backend",
                                    host="", group_id=group.id, enabled=False))
        base = datetime.now(timezone.utc) - timedelta(minutes=5 * len(values))
        for i, value in enumerate(values):
            when = base + timedelta(minutes=5 * i)
            run = ScanRun(scanned_at=when.isoformat(), mode="mock", total=1,
                          breach_count=0, services_total=0, services_down_count=0,
                          created_at=when)
            db.session.add(run)
            db.session.flush()
            db.session.add(MetricResult(
                scan_run_id=run.id, key=f"{INST}_cpu", group_name="HistTest",
                instance_id=INST, instance_name="History Test", label=label,
                value=value, unit="%", threshold=threshold, comparison=comparison,
                breached=bool(value is not None and value > threshold),
                source="mock"))
        db.session.commit()


def _cleanup(app):
    with app.app_context():
        runs = (ScanRun.query.join(MetricResult, MetricResult.scan_run_id == ScanRun.id)
                .filter(MetricResult.instance_id == INST).all())
        for run in runs:
            db.session.delete(run)
        inst = db.session.get(Instance, INST)
        if inst is not None:
            db.session.delete(inst)
        db.session.commit()


@pytest.fixture
def hist_app():
    app = make_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    yield app
    _cleanup(app)


@pytest.fixture
def hist_client(hist_app):
    c = hist_app.test_client()
    assert c.post("/auth/dev-login").status_code in (200, 302, 303)
    return c


def _series(payload, label="CPU"):
    for s in payload["series"]:
        if s["label"] == label:
            return s
    raise AssertionError(f"no {label} series in {[s['label'] for s in payload['series']]}")


# --- the point of the whole feature -----------------------------------------
def test_bucket_keeps_the_spike_not_the_average(hist_app, hist_client):
    """One spike among eleven quiet cycles must survive bucketing.

    The mean of this series is ~10; the spike is 81.55. A chart drawn from the
    mean would show a flat, healthy line - which is exactly the failure this
    endpoint was built to prevent.
    """
    values = [3.4] * 11 + [81.55]
    _seed(hist_app, values)

    # One bucket for the lot: points=10 over a 2h window forces a wide bucket.
    r = hist_client.get(f"/api/metrics/history?instance_id={INST}&hours=2&points=10")
    assert r.status_code == 200
    s = _series(r.get_json())
    assert s["aggregate"] == "max"
    seen = [p["v"] for p in s["points"] if p["v"] is not None]
    assert max(seen) == pytest.approx(81.55)

    mean = sum(values) / len(values)
    assert max(seen) > mean * 2, "bucket collapsed the spike toward the mean"


def test_no_data_stays_null_never_zero(hist_app, hist_client):
    """A cycle with no datapoint must not read as 0% usage."""
    _seed(hist_app, [40.0, None, None, 41.0])
    r = hist_client.get(f"/api/metrics/history?instance_id={INST}&hours=1&points=240")
    assert r.status_code == 200
    s = _series(r.get_json())
    values = [p["v"] for p in s["points"]]
    assert None in values, "the reporting gap disappeared"
    assert 0 not in values and 0.0 not in values, "no-data was rendered as zero"


def test_axis_is_continuous_so_a_gap_is_visible(hist_app, hist_client):
    """Empty buckets are emitted, not skipped, so a monitoring outage shows."""
    _seed(hist_app, [50.0])          # a single point in a one-hour window
    r = hist_client.get(f"/api/metrics/history?instance_id={INST}&hours=1&points=240")
    s = _series(r.get_json())
    assert len(s["points"]) > 5, "axis collapsed to only the buckets holding data"
    assert sum(1 for p in s["points"] if p["v"] is None) > 0


def test_lower_is_worse_takes_the_minimum(hist_app, hist_client):
    """A `<` rule inverts which extreme matters."""
    _seed(hist_app, [90.0, 90.0, 4.0], label="Free", threshold=10.0, comparison="<")
    r = hist_client.get(f"/api/metrics/history?instance_id={INST}&hours=2&points=10")
    s = _series(r.get_json(), label="Free")
    assert s["aggregate"] == "min"
    seen = [p["v"] for p in s["points"] if p["v"] is not None]
    assert min(seen) == pytest.approx(4.0)


# --- window handling --------------------------------------------------------
def test_explicit_from_to_window(hist_app, hist_client):
    _seed(hist_app, [10.0, 20.0, 30.0])
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=30)
    r = hist_client.get("/api/metrics/history",
                        query_string={"instance_id": INST,
                                      "from": start.isoformat(),
                                      "to": end.isoformat()})
    assert r.status_code == 200
    body = r.get_json()
    assert body["from"][:16] == start.isoformat()[:16]
    assert body["bucket_seconds"] >= 60


def test_bucket_is_a_whole_number_of_scan_intervals(hist_app, hist_client):
    import config
    _seed(hist_app, [1.0, 2.0])
    r = hist_client.get(f"/api/metrics/history?instance_id={INST}&hours=24&points=50")
    bucket = r.get_json()["bucket_seconds"]
    step = max(int(config.SCAN_INTERVAL_MINUTES) * 60, 60)
    assert bucket % step == 0 and bucket >= step


def test_instance_id_is_required(hist_client):
    assert hist_client.get("/api/metrics/history").status_code == 400


def test_history_requires_login(anon_client):
    r = anon_client.get(f"/api/metrics/history?instance_id={INST}")
    assert r.status_code == 401


def test_unknown_instance_is_empty_not_an_error(hist_client):
    """An instance with no rows yet returns an empty series, not a 500."""
    r = hist_client.get("/api/metrics/history?instance_id=i-does-not-exist&hours=1")
    assert r.status_code == 200
    assert r.get_json()["series"] == []
