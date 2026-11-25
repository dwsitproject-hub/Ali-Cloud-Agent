"""A container docker calls broken must be able to email, and must block an all-clear.

`pct-api` answered nothing on Backend Production for 2h 20m. The dashboard knew -
it was issue 05 in the triage queue, with a runbook - but container health was not
a problem key, so `recent_problem_key_sets()` returned nothing and
`alerting.decide()` sent "All systems healthy" over the top of a dead API.

A port probe could not have caught it either: the wedged process kept its
published port bound, so the socket existed and nothing behind it answered. Only
docker's own verdict saw it, and that verdict reached the page but not the email.
"""
import config
import containers
from agents import agent2_alerter
from db import db
from models import Instance, InstanceGroup
from tests.conftest import make_app

INST = "i-ctr-alert-test"


def _app():
    a = make_app()
    a.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return a


def _set_containers(app, rows):
    with app.app_context():
        group = InstanceGroup.query.first()
        inst = db.session.get(Instance, INST)
        if inst is None:
            inst = Instance(id=INST, name="Ctr Alert Test", role="backend",
                            host="", group_id=group.id, enabled=True)
            db.session.add(inst)
        inst.containers_json = rows
        db.session.commit()


def _cleanup(app):
    with app.app_context():
        inst = db.session.get(Instance, INST)
        if inst is not None:
            db.session.delete(inst)
            db.session.commit()


def _ctr(name, health, status=""):
    return {"name": name, "image": name + ":latest", "state": "running",
            "status": status or ("Up 2 hours (%s)" % health), "health": health,
            "ports": "", "cpu_pct": 101.6, "mem_pct": 15.2,
            "mem_usage": "2.276GiB / 14.96GiB", "mem_bytes": 2443408179,
            "mem_limit_bytes": 16064051609, "mem_pct_host": 15.21}


# --- which states count ------------------------------------------------------
def test_unhealthy_and_restarting_are_problems():
    app = _app()
    try:
        _set_containers(app, [_ctr("pct-api", "unhealthy"),
                              _ctr("flappy", "restarting")])
        with app.app_context():
            names = {p["name"] for p in containers.problems()}
        assert names == {"pct-api", "flappy"}
    finally:
        _cleanup(app)


def test_a_finished_job_is_not_a_problem():
    """ecr-migrate-prod exited 0 two days ago. It is SUPPOSED to be stopped.

    Alerting on that trains people to ignore the mail, which costs more than it
    buys. It stays on the dashboard.
    """
    app = _app()
    try:
        _set_containers(app, [
            {"name": "ecr-migrate-prod", "health": "stopped",
             "status": "Exited (0) 2 days ago", "image": "x", "state": "exited",
             "ports": "", "cpu_pct": None, "mem_pct": None, "mem_usage": ""},
            {"name": "slms-api", "health": "created", "status": "Created",
             "image": "x", "state": "created", "ports": "",
             "cpu_pct": None, "mem_pct": None, "mem_usage": ""},
        ])
        with app.app_context():
            assert containers.problems() == []
    finally:
        _cleanup(app)


def test_healthy_containers_are_not_problems():
    app = _app()
    try:
        _set_containers(app, [_ctr("fine", "healthy"), _ctr("also-fine", "running")])
        with app.app_context():
            assert containers.problems() == []
    finally:
        _cleanup(app)


def test_the_feature_can_be_switched_off(monkeypatch):
    app = _app()
    try:
        _set_containers(app, [_ctr("pct-api", "unhealthy")])
        monkeypatch.setattr(config, "ALERT_ON_CONTAINER_HEALTH", False)
        with app.app_context():
            assert containers.problems() == []
    finally:
        _cleanup(app)


def test_key_is_the_container_not_its_state():
    """unhealthy -> restarting is one ongoing problem, not a new one.

    With the state in the key, every flip would read as a fresh problem and
    re-alert, which is precisely what the hysteresis exists to prevent.
    """
    a = containers.problem_key("i-x", "pct-api")
    assert a == "ctr:i-x:pct-api"
    app = _app()
    try:
        _set_containers(app, [_ctr("pct-api", "unhealthy")])
        with app.app_context():
            first = containers.problems()[0]["key"]
        _set_containers(app, [_ctr("pct-api", "restarting")])
        with app.app_context():
            second = containers.problems()[0]["key"]
        assert first == second
    finally:
        _cleanup(app)


# --- it reaches the decision, and the email ----------------------------------
def test_a_broken_container_blocks_the_all_clear():
    """The actual failure: "All systems healthy" sent over a dead API."""
    import alerting
    from datetime import datetime, timezone

    key = containers.problem_key("i-be", "pct-api")
    # Two consecutive scans in which the only problem is the broken container.
    sets = [{key}, {key}, {key}]
    reason = alerting.decide(sets, (datetime.now(timezone.utc), "new"),
                             datetime.now(timezone.utc), 60, True, 2)
    assert reason != "recovered", "an all-clear went out with a problem outstanding"


def test_a_broken_container_alerts_once_confirmed():
    import alerting
    from datetime import datetime, timezone

    key = containers.problem_key("i-be", "pct-api")
    sets = [{key}, {key}, set()]          # newly sustained across two scans
    assert alerting.decide(sets, None, datetime.now(timezone.utc), 60, True, 2) == "new"


def test_one_cycle_of_restarting_does_not_alert():
    """A deploy shows `restarting` for seconds; the flap guard must absorb it."""
    import alerting
    from datetime import datetime, timezone

    key = containers.problem_key("i-be", "deploying")
    sets = [{key}, set(), set()]
    assert alerting.decide(sets, None, datetime.now(timezone.utc), 60, True, 2) is None


def test_the_email_names_the_container():
    scan = {"scanned_at": "2026-11-25T09:51:31+00:00", "mode": "live", "total": 3,
            "breach_count": 0, "results": [], "breaches": [], "services": [],
            "services_down": [], "services_down_count": 0, "services_total": 0,
            "container_problems": [
                {"key": "ctr:i-be:pct-api", "instance_id": "i-be",
                 "instance_name": "Backend Production", "name": "pct-api",
                 "image": "pct-api:staging", "health": "unhealthy",
                 "status": "Up 2 hours (unhealthy)"}]}
    email = agent2_alerter.build_email(scan)
    assert "All systems healthy" not in email["subject"]
    assert "1 container broken" in email["subject"]
    assert "pct-api" in email["html_body"]
    assert "Backend Production" in email["html_body"]
    assert "unhealthy" in email["html_body"]


def test_no_broken_containers_leaves_the_email_alone():
    scan = {"scanned_at": "2026-11-25T09:51:31+00:00", "mode": "live", "total": 3,
            "breach_count": 0, "results": [], "breaches": [], "services": [],
            "services_down": [], "services_down_count": 0, "services_total": 0}
    email = agent2_alerter.build_email(scan)
    assert "All systems healthy" in email["subject"]
    assert "docker reports as broken" not in email["html_body"]


# --- persistence -------------------------------------------------------------
def test_problems_are_recorded_with_the_scan_run():
    """Instance.containers_json is current-only; decide() needs history."""
    import state
    from models import ScanRun

    app = _app()
    try:
        _set_containers(app, [_ctr("pct-api", "unhealthy")])
        with app.app_context():
            state.set_last_scan({"scanned_at": "2026-11-25T09:51:31+00:00",
                                 "mode": "mock", "total": 0, "breach_count": 0,
                                 "services_total": 0, "services_down_count": 0,
                                 "results": [], "services": []})
            run = ScanRun.query.order_by(ScanRun.id.desc()).first()
            try:
                assert run.container_problems, "nothing recorded on the run"
                assert any(c["name"] == "pct-api" for c in run.container_problems)
                # And it comes back out as a problem key.
                assert containers.problem_key(INST, "pct-api") in \
                    state.recent_problem_key_sets(1)[0]
            finally:
                db.session.delete(run)
                db.session.commit()
    finally:
        _cleanup(app)
