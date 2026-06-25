"""Register / manage monitored instances.

A page (GET /instances) plus a small JSON CRUD API. Instances written here land
in PostgreSQL and are picked up by the next scan (the agents read the inventory
per cycle - see config.build_metrics / models.enabled_instance_dicts).

  GET    /instances              management page (form + list)
  GET    /api/instances          list all instances
  POST   /api/instances          create an instance
  PATCH  /api/instances/<id>     update fields / toggle enabled
  DELETE /api/instances/<id>     remove an instance
"""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

import config
from db import db
from models import Instance, InstanceGroup

instances_bp = Blueprint("instances", __name__)


def _roles() -> list:
    return sorted(config.SERVICE_CHECKS_BY_ROLE.keys())


def _instance_to_dict(i: Instance) -> dict:
    return {
        "id": i.id, "name": i.name, "role": i.role, "host": i.host or "",
        "enabled": i.enabled,
        "group_id": i.group_id, "group": i.group.name if i.group else None,
    }


def _resolve_group(data: dict) -> InstanceGroup | None:
    """Find the group by id, or get-or-create by name. None if unspecified."""
    gid = data.get("group_id")
    if gid:
        return db.session.get(InstanceGroup, int(gid))
    name = (data.get("group_name") or data.get("group") or "").strip()
    if not name:
        return None
    group = InstanceGroup.query.filter_by(name=name).first()
    if group is None:
        max_order = db.session.query(db.func.max(InstanceGroup.sort_order)).scalar() or 0
        group = InstanceGroup(name=name, sort_order=max_order + 1)
        db.session.add(group)
        db.session.flush()
    return group


@instances_bp.route("/instances")
def manage_page():
    groups = InstanceGroup.query.order_by(InstanceGroup.sort_order, InstanceGroup.name).all()
    instances = (
        Instance.query.join(InstanceGroup)
        .order_by(InstanceGroup.sort_order, Instance.name).all()
    )
    return render_template(
        "register_instance.html",
        roles=_roles(),
        groups=[{"id": g.id, "name": g.name} for g in groups],
        instances=[_instance_to_dict(i) for i in instances],
    )


@instances_bp.route("/api/instances", methods=["GET"])
def list_instances():
    instances = (
        Instance.query.join(InstanceGroup)
        .order_by(InstanceGroup.sort_order, Instance.name).all()
    )
    return jsonify([_instance_to_dict(i) for i in instances])


@instances_bp.route("/api/instances", methods=["POST"])
def create_instance():
    data = request.get_json(silent=True) or {}
    inst_id = (data.get("id") or "").strip()
    name = (data.get("name") or "").strip()
    role = (data.get("role") or "").strip()
    host = (data.get("host") or "").strip()

    if not inst_id or not name or not role:
        return jsonify(error="id, name and role are required"), 400
    if db.session.get(Instance, inst_id) is not None:
        return jsonify(error=f"instance '{inst_id}' already exists"), 409

    group = _resolve_group(data)
    if group is None:
        return jsonify(error="a group (group_id or group_name) is required"), 400

    inst = Instance(id=inst_id, name=name, role=role, host=host,
                    group_id=group.id, enabled=bool(data.get("enabled", True)))
    db.session.add(inst)
    db.session.commit()
    return jsonify(_instance_to_dict(inst)), 201


@instances_bp.route("/api/instances/<inst_id>", methods=["PATCH"])
def update_instance(inst_id):
    inst = db.session.get(Instance, inst_id)
    if inst is None:
        return jsonify(error="not found"), 404

    data = request.get_json(silent=True) or {}
    if "enabled" in data:
        inst.enabled = bool(data["enabled"])
    if data.get("name"):
        inst.name = data["name"].strip()
    if data.get("role"):
        inst.role = data["role"].strip()
    if "host" in data:
        inst.host = (data.get("host") or "").strip()
    if data.get("group_id") or data.get("group_name") or data.get("group"):
        group = _resolve_group(data)
        if group is not None:
            inst.group_id = group.id

    db.session.commit()
    return jsonify(_instance_to_dict(inst))


@instances_bp.route("/api/instances/<inst_id>", methods=["DELETE"])
def delete_instance(inst_id):
    inst = db.session.get(Instance, inst_id)
    if inst is None:
        return jsonify(error="not found"), 404
    db.session.delete(inst)
    db.session.commit()
    return jsonify(deleted=inst_id)
