"""Seeding must not resurrect an instance someone deleted.

The compose `command` runs `flask seed` on every container start. It used to
insert any seed instance whose id was absent - and "absent" is precisely what a
deletion produces, so DB Staging and DB Production reappeared after each restart
long after being retired to ApsaraDB, each time bringing back two timed-out
probes and a failed diagnostics SSH every five minutes.
"""
import pytest

import config
import seed as seed_mod
from db import db
from models import Instance, InstanceGroup
from tests.conftest import make_app

SEED_IDS = [i["id"] for g in config.INSTANCE_GROUPS for i in g["instances"]]
KEEPER = "i-seed-test-keeper"


def _wipe(app):
    with app.app_context():
        for inst in Instance.query.filter(
                Instance.id.in_(SEED_IDS + [KEEPER])).all():
            db.session.delete(inst)
        db.session.commit()


def _app():
    app = make_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return app


@pytest.fixture(autouse=True)
def restore_inventory():
    """Put the stock inventory back after every test in this module.

    These tests necessarily empty the instance table, and the suite shares one
    Postgres database - test_contract.py asserts a scan returns a non-empty
    `results`, which an empty inventory cannot produce. Leaving the table bare
    made this module's presence break two unrelated tests depending on ordering.
    """
    yield
    app = _app()
    with app.app_context():
        keeper = db.session.get(Instance, KEEPER)
        if keeper is not None:
            db.session.delete(keeper)
            db.session.commit()
        seed_mod.seed_data(force=True)


def _ids(app):
    with app.app_context():
        return {i.id for i in Instance.query.all()}


def test_a_deleted_instance_stays_deleted_across_restarts():
    """The bug, stated as a test: seed on a managed inventory changes nothing."""
    app = _app()
    _wipe(app)
    try:
        with app.app_context():
            group = InstanceGroup.query.first()
            if group is None:
                group = InstanceGroup(name="SeedTest", sort_order=98)
                db.session.add(group)
                db.session.flush()
            db.session.add(Instance(id=KEEPER, name="Keeper", role="backend",
                                    host="", group_id=group.id, enabled=False))
            db.session.commit()

        before = _ids(app)
        assert KEEPER in before

        # Three "restarts".
        with app.app_context():
            for _ in range(3):
                summary = seed_mod.seed_data()
                assert summary["instances_created"] == 0
                assert summary["instances_seeded"] is False

        assert _ids(app) == before, "seeding added instances to a managed inventory"
    finally:
        _wipe(app)


def test_empty_inventory_is_still_bootstrapped():
    """A fresh database must come up populated, as it always did."""
    app = _app()
    _wipe(app)
    with app.app_context():
        # Only meaningful when the table really is empty.
        if Instance.query.count() == 0:
            summary = seed_mod.seed_data()
            assert summary["instances_created"] == len(SEED_IDS)
            assert summary["instances_seeded"] is True
            assert set(SEED_IDS).issubset({i.id for i in Instance.query.all()})
    _wipe(app)


def test_force_restores_the_stock_inventory():
    """The escape hatch still tops up by id, for deliberate re-seeding."""
    app = _app()
    _wipe(app)
    try:
        with app.app_context():
            group = InstanceGroup.query.first()
            db.session.add(Instance(id=KEEPER, name="Keeper", role="backend",
                                    host="", group_id=group.id, enabled=False))
            db.session.commit()
            summary = seed_mod.seed_data(force=True)
            assert summary["instances_seeded"] is True
            assert summary["instances_created"] >= 1
            assert set(SEED_IDS).issubset({i.id for i in Instance.query.all()})
    finally:
        _wipe(app)


def test_groups_are_still_created_on_a_managed_inventory():
    """A missing group would break the register page's form, so these stay idempotent."""
    app = _app()
    _wipe(app)
    try:
        with app.app_context():
            group = InstanceGroup.query.first()
            db.session.add(Instance(id=KEEPER, name="Keeper", role="backend",
                                    host="", group_id=group.id, enabled=False))
            db.session.commit()
            names = {g["group"] for g in config.INSTANCE_GROUPS}
            for g in InstanceGroup.query.filter(InstanceGroup.name.in_(names)).all():
                db.session.delete(g)
            db.session.commit()

            seed_mod.seed_data()
            have = {g.name for g in InstanceGroup.query.all()}
            assert names.issubset(have)
    finally:
        _wipe(app)
