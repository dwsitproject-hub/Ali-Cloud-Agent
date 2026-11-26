"""Sizing a mem_limit needs a distribution, not a reading.

A cap below a service's real working set converts a healthy container into an
OOM-kill loop, and one above its ceiling protects nothing. These cover the parts
that decide whether the recommendation is trustworthy: the peak a limit must
clear, whether that peak is representative, and whether there is enough data to
believe either.
"""
from datetime import datetime, timedelta, timezone

import pytest

import containers
from db import db
from models import ContainerSample, Instance, InstanceGroup
from tests.conftest import make_app

INST = "i-usage-test"
MIB = 1024 ** 2


@pytest.fixture
def app():
    a = make_app()
    a.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    yield a
    with a.app_context():
        ContainerSample.query.filter_by(instance_id=INST).delete(
            synchronize_session=False)
        inst = db.session.get(Instance, INST)
        if inst is not None:
            db.session.delete(inst)
        db.session.commit()


@pytest.fixture
def client(app):
    c = app.test_client()
    assert c.post("/auth/dev-login").status_code in (200, 302, 303)
    return c


def _seed(app, name, mem_mibs, limit_mib=None, host_mib=15319):
    """One sample per entry, a minute apart, newest last."""
    with app.app_context():
        group = InstanceGroup.query.first()
        if db.session.get(Instance, INST) is None:
            db.session.add(Instance(
                id=INST, name="Usage Test", role="backend", host="",
                group_id=group.id, enabled=False,
                containers_json=[
                    {"name": "uncapped-a", "mem_usage": "100MiB / %sMiB" % host_mib,
                     "mem_limit_bytes": host_mib * MIB},
                    {"name": "uncapped-b", "mem_usage": "100MiB / %sMiB" % host_mib,
                     "mem_limit_bytes": host_mib * MIB},
                ]))
        base = datetime.now(timezone.utc) - timedelta(minutes=len(mem_mibs))
        for i, m in enumerate(mem_mibs):
            db.session.add(ContainerSample(
                instance_id=INST, name=name, image="x", health="healthy",
                cpu_pct=1.0, mem_bytes=(None if m is None else int(m * MIB)),
                mem_limit_bytes=(limit_mib or host_mib) * MIB,
                sampled_at=base + timedelta(minutes=i)))
        db.session.commit()


def _row(body, name):
    return next(r for r in body["usage"] if r["name"] == name)


def test_suggestion_clears_the_observed_peak(app, client):
    """The one property a limit must have: it is above what we have seen."""
    _seed(app, "steady", [400] * 300 + [900])
    body = client.get("/api/containers/usage?instance_id=%s&days=14" % INST).get_json()
    r = _row(body, "steady")
    assert r["mem_peak_mib"] == 900
    assert r["suggested_limit_mib"] >= 900 * 1.5
    assert r["suggested_limit_mib"] % 64 == 0, "not a number anyone would type"


def test_a_one_off_spike_is_flagged_not_designed_around(app, client):
    """Peak far above p99 means one excursion. Sizing for it wastes the host."""
    _seed(app, "spiky", [200] * 400 + [4000])
    r = _row(client.get(
        "/api/containers/usage?instance_id=%s&days=14" % INST).get_json(), "spiky")
    assert r["peak_is_an_outlier"] is True
    assert r["mem_p99_mib"] < r["mem_peak_mib"] / 1.5


def test_a_steady_service_is_not_flagged_as_an_outlier(app, client):
    _seed(app, "flat", [500] * 400)
    r = _row(client.get(
        "/api/containers/usage?instance_id=%s&days=14" % INST).get_json(), "flat")
    assert r["peak_is_an_outlier"] is False


def test_thin_data_is_labelled_thin(app, client):
    """Four readings are not a fortnight, and must not read as one."""
    _seed(app, "new-service", [300, 310, 305, 320])
    r = _row(client.get(
        "/api/containers/usage?instance_id=%s&days=14" % INST).get_json(), "new-service")
    assert r["enough_samples"] is False
    assert r["mem_samples"] == 4


def test_a_stopped_container_contributes_no_zero(app, client):
    """No usage is not zero usage - averaging nulls as 0 would undersize a cap."""
    _seed(app, "sometimes-off", [600] * 250 + [None] * 50)
    r = _row(client.get(
        "/api/containers/usage?instance_id=%s&days=14" % INST).get_json(), "sometimes-off")
    assert r["samples"] == 300
    assert r["mem_samples"] == 250
    assert r["mem_median_mib"] == 600


def test_an_uncapped_container_reports_no_current_limit(app, client):
    """docker prints HOST ram as an uncapped container's ceiling - not a limit."""
    _seed(app, "uncapped-a", [700] * 300)          # limit defaults to host size
    r = _row(client.get(
        "/api/containers/usage?instance_id=%s&days=14" % INST).get_json(), "uncapped-a")
    assert r["current_limit_mib"] is None


def test_a_real_limit_is_reported(app, client):
    _seed(app, "capped", [700] * 300, limit_mib=2048)
    r = _row(client.get(
        "/api/containers/usage?instance_id=%s&days=14" % INST).get_json(), "capped")
    assert r["current_limit_mib"] == 2048


def test_window_excludes_older_samples(app, client):
    _seed(app, "recent", [100] * 10)
    with app.app_context():
        old = datetime.now(timezone.utc) - timedelta(days=30)
        db.session.add(ContainerSample(
            instance_id=INST, name="recent", image="x", health="healthy",
            cpu_pct=1.0, mem_bytes=9999 * MIB, mem_limit_bytes=None,
            sampled_at=old))
        db.session.commit()
    r = _row(client.get(
        "/api/containers/usage?instance_id=%s&days=1" % INST).get_json(), "recent")
    assert r["mem_peak_mib"] == 100, "a month-old sample leaked into a 1-day window"


def test_instance_id_required_and_login_enforced(client, anon_client):
    assert client.get("/api/containers/usage").status_code == 400
    assert anon_client.get(
        "/api/containers/usage?instance_id=x").status_code == 401


# --- collection + pruning ----------------------------------------------------
def test_samples_are_recorded_for_each_container(app):
    with app.app_context():
        group = InstanceGroup.query.first()
        if db.session.get(Instance, INST) is None:
            db.session.add(Instance(id=INST, name="Usage Test", role="backend",
                                    host="", group_id=group.id, enabled=False))
            db.session.commit()
        now = datetime.now(timezone.utc)
        n = containers._record_samples(INST, [
            {"name": "a", "image": "i", "health": "healthy", "cpu_pct": 2.0,
             "mem_bytes": 100 * MIB, "mem_limit_bytes": 1024 * MIB},
            {"name": "b", "image": "i", "health": "stopped", "cpu_pct": None,
             "mem_bytes": None, "mem_limit_bytes": None},
        ], now)
        db.session.commit()
        assert n == 2
        got = {s.name: s for s in
               ContainerSample.query.filter_by(instance_id=INST).all()}
        assert got["a"].mem_bytes == 100 * MIB
        assert got["b"].mem_bytes is None       # stopped: unknown, not zero


def test_prune_removes_only_old_samples(app):
    with app.app_context():
        now = datetime.now(timezone.utc)
        for age_days, name in ((40, "old"), (1, "fresh")):
            db.session.add(ContainerSample(
                instance_id=INST, name=name, image="x", health="healthy",
                cpu_pct=1.0, mem_bytes=MIB, mem_limit_bytes=None,
                sampled_at=now - timedelta(days=age_days)))
        db.session.commit()
        containers.prune_samples(30)
        left = {s.name for s in
                ContainerSample.query.filter_by(instance_id=INST).all()}
        assert left == {"fresh"}


def test_collection_can_be_switched_off(app, monkeypatch):
    import config
    monkeypatch.setattr(config, "CONTAINER_HISTORY_DAYS", 0)
    with app.app_context():
        assert containers._record_samples(INST, [
            {"name": "a", "mem_bytes": MIB}], datetime.now(timezone.utc)) == 0
        assert containers.prune_samples(0) == 0
