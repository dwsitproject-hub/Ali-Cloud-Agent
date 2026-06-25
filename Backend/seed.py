"""Idempotent database seeding.

`flask seed` populates InstanceGroup + Instance from config.INSTANCE_GROUPS (the
original hardcoded inventory) and creates the local dev user. Safe to re-run:
existing rows are left untouched so edits made via the register page survive.
"""
from __future__ import annotations

import logging

import config
from db import db
from models import Instance, InstanceGroup, User

log = logging.getLogger("seed")

# Stable id for the dev-login user (see auth/sso.py).
DEV_USER_ID = "00000000-0000-0000-0000-000000000000"


def seed_data() -> dict:
    """Insert missing groups/instances/dev-user. Returns a small summary."""
    created_groups = created_instances = 0

    for sort_order, grp in enumerate(config.INSTANCE_GROUPS):
        group = InstanceGroup.query.filter_by(name=grp["group"]).first()
        if group is None:
            group = InstanceGroup(name=grp["group"], sort_order=sort_order)
            db.session.add(group)
            db.session.flush()  # assign group.id
            created_groups += 1

        for inst in grp["instances"]:
            if db.session.get(Instance, inst["id"]) is None:
                db.session.add(Instance(
                    id=inst["id"],
                    name=inst["name"],
                    role=inst.get("role", ""),
                    host=(inst.get("host") or ""),
                    group_id=group.id,
                    enabled=True,
                ))
                created_instances += 1

    if db.session.get(User, DEV_USER_ID) is None:
        db.session.add(User(id=DEV_USER_ID, email=config.DEV_LOGIN_EMAIL))

    db.session.commit()
    summary = {"groups_created": created_groups, "instances_created": created_instances}
    log.info("seed complete: %s", summary)
    return summary


def register_cli(app) -> None:
    @app.cli.command("seed")
    def seed_command():  # pragma: no cover - thin CLI wrapper
        """Populate groups/instances/dev-user (idempotent)."""
        seed_data()
