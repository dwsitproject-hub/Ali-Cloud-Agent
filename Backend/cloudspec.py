"""Provider-reported instance specification (vCPU / RAM / disk / status).

CloudMonitor tells us how *loaded* an instance is; it never says how big it is.
Without the denominator a metric is hard to act on - "95% memory" means one thing
on a 4 GB box and another on a 64 GB one. This module fills that in from the
owning product's own describe API:

  * ECS      -> DescribeInstances (Cpu, Memory, InstanceType, Status, OSName)
                DescribeDisks     (attached disk sizes, summed)
  * ApsaraDB -> DescribeDBInstanceAttribute (DBInstanceCPU/Memory/Storage,
                DBInstanceStatus, Engine + EngineVersion)

Specs are cached on the Instance row and refreshed on a slow cadence
(SPECS_REFRESH_HOURS): hardware does not change every five minutes, and these are
extra API calls on the same rate limit the metric scan uses.

Read-only: every call here is a Describe.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import config
from cloud_client import do_rpc

log = logging.getLogger("cloudspec")

ECS_VERSION = "2014-05-26"
RDS_VERSION = "2014-08-15"


def _ecs_domain() -> str:
    return f"ecs.{config.CLOUDMONITOR_REGION}.aliyuncs.com"


def _rds_domain() -> str:
    return f"rds.{config.CLOUDMONITOR_REGION}.aliyuncs.com"


def _text(value):
    """Collapse the provider's padded strings ("Ubuntu  24.04 64位")."""
    return " ".join((value or "").split()) or None


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- ECS ---------------------------------------------------------------------
def fetch_ecs_specs() -> dict:
    """``{instance_id: spec_dict}`` for every ECS instance in the region.

    One paged call for the whole region rather than one per instance: the estate
    is small and this keeps the API cost flat as the inventory grows.
    """
    specs: dict = {}
    page = 1
    while True:
        resp = do_rpc(_ecs_domain(), ECS_VERSION, "DescribeInstances",
                      {"RegionId": config.CLOUDMONITOR_REGION,
                       "PageSize": "100", "PageNumber": str(page)},
                      region=config.CLOUDMONITOR_REGION)
        rows = (resp.get("Instances") or {}).get("Instance") or []
        for i in rows:
            iid = i.get("InstanceId")
            if not iid:
                continue
            specs[iid] = {
                "instance_type": i.get("InstanceType"),
                "vcpu": _int(i.get("Cpu")),
                "memory_mb": _int(i.get("Memory")),
                "platform": _text(i.get("OSName") or i.get("OSType")),
                "provider_status": i.get("Status"),
                "disk_gb": None,          # filled in by _attach_disk_sizes
            }
        if len(rows) < 100:
            break
        page += 1
        if page > 20:                     # defensive: never loop forever
            break
    _attach_disk_sizes(specs)
    return specs


def _attach_disk_sizes(specs: dict) -> None:
    """Sum every attached disk per instance.

    A box can have several disks (the mail server has a 100 GB system disk plus
    ~10 TB of data disks), so the useful number is the total, not the first one
    the API happens to return.
    """
    page = 1
    totals: dict = {}
    while True:
        resp = do_rpc(_ecs_domain(), ECS_VERSION, "DescribeDisks",
                      {"RegionId": config.CLOUDMONITOR_REGION,
                       "PageSize": "100", "PageNumber": str(page)},
                      region=config.CLOUDMONITOR_REGION)
        rows = (resp.get("Disks") or {}).get("Disk") or []
        for d in rows:
            iid = d.get("InstanceId")
            size = _int(d.get("Size"))
            if iid and size:
                totals[iid] = totals.get(iid, 0) + size
        if len(rows) < 100:
            break
        page += 1
        if page > 20:
            break
    for iid, total in totals.items():
        if iid in specs:
            specs[iid]["disk_gb"] = total


# --- ApsaraDB RDS ------------------------------------------------------------
def fetch_rds_spec(instance_id: str) -> dict:
    """Spec + lifecycle status for one ApsaraDB instance.

    ``provider_status`` here IS the database status the dashboard shows - RDS is a
    managed service with no shell, so its own API is the only source.
    """
    resp = do_rpc(_rds_domain(), RDS_VERSION, "DescribeDBInstanceAttribute",
                  {"DBInstanceId": instance_id},
                  region=config.CLOUDMONITOR_REGION)
    rows = (resp.get("Items") or {}).get("DBInstanceAttribute") or []
    if not rows:
        return {}
    a = rows[0]
    engine = _text(" ".join(x for x in (a.get("Engine"), a.get("EngineVersion")) if x))
    return {
        "instance_type": a.get("DBInstanceClass"),
        "vcpu": _int(a.get("DBInstanceCPU")),
        "memory_mb": _int(a.get("DBInstanceMemory")),
        "disk_gb": _int(a.get("DBInstanceStorage")),
        "platform": engine,
        "provider_status": a.get("DBInstanceStatus"),
    }


# --- refresh -----------------------------------------------------------------
def _is_rds(inst) -> bool:
    role = (getattr(inst, "role", "") or "").lower()
    return role == "rds" or (inst.id or "").lower().startswith(("pgm-", "rm-"))


def _stale(inst, now) -> bool:
    if inst.specs_updated_at is None:
        return True
    last = inst.specs_updated_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) >= timedelta(hours=config.SPECS_REFRESH_HOURS)


def refresh_specs(force: bool = False) -> int:
    """Update cached specs for instances whose copy is missing or stale.

    Returns the number of instances updated. Must run inside a Flask app context.
    Never raises: a describe failure leaves the previous (or empty) spec in place
    rather than breaking the scan cycle that called it.
    """
    if config.MOCK_MODE or not config.credentials_present():
        return 0

    from db import db
    from models import Instance

    now = datetime.now(timezone.utc)
    pending = [i for i in Instance.query.all() if force or _stale(i, now)]
    if not pending:
        return 0

    ecs_specs: dict = {}
    if any(not _is_rds(i) for i in pending):
        try:
            ecs_specs = fetch_ecs_specs()
        except Exception as exc:
            log.warning("cloudspec: ECS describe failed: %s: %s", type(exc).__name__, exc)

    updated = 0
    for inst in pending:
        try:
            spec = fetch_rds_spec(inst.id) if _is_rds(inst) else ecs_specs.get(inst.id)
        except Exception as exc:
            log.warning("cloudspec: %s describe failed: %s: %s",
                        inst.id, type(exc).__name__, exc)
            continue
        if not spec:
            continue
        for field, value in spec.items():
            setattr(inst, field, value)
        inst.specs_updated_at = now
        updated += 1

    if updated:
        db.session.commit()
        log.info("cloudspec: refreshed specs for %s instance(s)", updated)
    return updated
