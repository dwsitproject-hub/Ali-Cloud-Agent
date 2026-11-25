"""Bootstrap seeding - populate an EMPTY inventory, never reconcile a managed one.

`flask seed` creates the instance groups, the local dev user, and - only when the
inventory is empty - the instances from config.INSTANCE_GROUPS.

The last part is the important one. The compose `command` runs `flask seed` on
every container start, and this used to insert any seed instance whose id was
absent. "Absent" is exactly what deleting an instance means, so every restart
resurrected the ones an operator had removed: DB Staging and DB Production came
back repeatedly after being retired to ApsaraDB, each reappearance bringing two
timed-out probes and a failed diagnostics SSH every five minutes. Deleting them
again did nothing, because the next restart undid it.

The register page is the live source of truth (see CLAUDE.md), so seeding must be
a bootstrap and nothing more: when any instance already exists, the inventory is
under management and this leaves it alone. `flask seed --force` restores the old
per-id top-up for the rare case of genuinely re-adding the stock inventory.

Groups and the dev user stay idempotent: a missing group would break the register
page's form, and neither can accumulate the way a deleted instance does.
"""
from __future__ import annotations

import logging

import click

import config
from db import db
from models import Instance, InstanceGroup, User

log = logging.getLogger("seed")

# Stable id for the dev-login user (see auth/sso.py).
DEV_USER_ID = "00000000-0000-0000-0000-000000000000"


def seed_data(force: bool = False) -> dict:
    """Insert missing groups/dev-user, and instances only on a fresh inventory.

    ``force`` re-enables the per-id top-up, which will also re-create anything
    deliberately deleted - that is the whole reason it is not the default.
    """
    created_groups = created_instances = 0

    existing = Instance.query.count()
    # An inventory with anything in it is being managed through the register page;
    # a missing id there is a decision, not a gap to fill.
    seed_instances = force or existing == 0

    for sort_order, grp in enumerate(config.INSTANCE_GROUPS):
        group = InstanceGroup.query.filter_by(name=grp["group"]).first()
        if group is None:
            group = InstanceGroup(name=grp["group"], sort_order=sort_order)
            db.session.add(group)
            db.session.flush()  # assign group.id
            created_groups += 1

        if not seed_instances:
            continue

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
    summary = {
        "groups_created": created_groups,
        "instances_created": created_instances,
        "instances_existing": existing,
        "instances_seeded": seed_instances,
    }
    log.info("seed complete: %s", summary)
    if not seed_instances:
        log.info("seed: inventory already has %s instance(s) - left untouched. "
                 "Use `flask seed --force` to re-add the stock inventory.", existing)
    return summary


def register_cli(app) -> None:
    @app.cli.command("seed")
    @click.option("--force", is_flag=True,
                  help="Also top up instances on a non-empty inventory. This "
                       "re-creates anything you deleted on purpose.")
    def seed_command(force):  # pragma: no cover - thin CLI wrapper
        """Populate groups/dev-user, and instances when the inventory is empty."""
        summary = seed_data(force=force)
        click.echo("seed: %s" % summary)
