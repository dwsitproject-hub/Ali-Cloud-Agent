"""SQLAlchemy models for Cloud Agent Monitoring.

Three concerns live in Postgres now:
  * Inventory   - InstanceGroup + Instance (what to monitor; edited via the
                  register page). Replaces the hardcoded config.INSTANCE_GROUPS.
  * History     - ScanRun + MetricResult + ServiceResult + Alert (replaces the
                  in-memory state.py; survives restarts).
  * Identity    - User (SSO users from the Hub; see auth/sso.py).

`group`/`type` are SQL-reserved-ish, so columns are named `group_name`/`svc_type`
and the serializers (state.py) map them back to the `group`/`type` keys the
dashboard JS expects.
"""
from __future__ import annotations

from datetime import datetime, timezone

from db import db


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(db.Model):
    __tablename__ = "users"
    # Hub user_id (UUID string) is the stable primary key.
    id = db.Column(db.String(36), primary_key=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)
    last_login_at = db.Column(db.DateTime(timezone=True))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.email}>"


class InstanceGroup(db.Model):
    __tablename__ = "instance_groups"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    sort_order = db.Column(db.Integer, default=0, nullable=False)

    instances = db.relationship(
        "Instance", back_populates="group",
        cascade="all, delete-orphan", order_by="Instance.name",
    )


class Instance(db.Model):
    __tablename__ = "instances"
    # ECS instance id (e.g. i-k1a5irk321vnht0kec3j) is the natural primary key.
    id = db.Column(db.String(64), primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(40), nullable=False)
    # Empty host == "skip service probes" (see healthcheck.py). Allowed blank.
    host = db.Column(db.String(255), nullable=False, default="")
    group_id = db.Column(db.Integer, db.ForeignKey("instance_groups.id"), nullable=False)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)

    group = db.relationship("InstanceGroup", back_populates="instances")


class ScanRun(db.Model):
    __tablename__ = "scan_runs"
    id = db.Column(db.Integer, primary_key=True)
    scanned_at = db.Column(db.String(40), nullable=False)  # ISO-8601 UTC string
    mode = db.Column(db.String(10), nullable=False)        # "mock" | "live"
    total = db.Column(db.Integer, default=0, nullable=False)
    breach_count = db.Column(db.Integer, default=0, nullable=False)
    services_total = db.Column(db.Integer, default=0, nullable=False)
    services_down_count = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    metric_results = db.relationship(
        "MetricResult", backref="scan_run", cascade="all, delete-orphan")
    service_results = db.relationship(
        "ServiceResult", backref="scan_run", cascade="all, delete-orphan")
    alerts = db.relationship(
        "Alert", backref="scan_run", cascade="all, delete-orphan")


class MetricResult(db.Model):
    __tablename__ = "metric_results"
    id = db.Column(db.Integer, primary_key=True)
    scan_run_id = db.Column(db.Integer, db.ForeignKey("scan_runs.id"), nullable=False, index=True)
    key = db.Column(db.String(120))
    group_name = db.Column(db.String(120))      # -> "group" in JSON
    instance_id = db.Column(db.String(64))
    instance_name = db.Column(db.String(120))
    label = db.Column(db.String(60))
    namespace = db.Column(db.String(120))
    metric_name = db.Column(db.String(120))
    stat = db.Column(db.String(40))
    value = db.Column(db.Float)                  # nullable: None == no data (not 0)
    unit = db.Column(db.String(20))
    threshold = db.Column(db.Float)
    comparison = db.Column(db.String(4))
    agent_required = db.Column(db.Boolean)
    breached = db.Column(db.Boolean)
    source = db.Column(db.String(20))
    error = db.Column(db.Text)


class ServiceResult(db.Model):
    __tablename__ = "service_results"
    id = db.Column(db.Integer, primary_key=True)
    scan_run_id = db.Column(db.Integer, db.ForeignKey("scan_runs.id"), nullable=False, index=True)
    key = db.Column(db.String(120))
    group_name = db.Column(db.String(120))      # -> "group" in JSON
    instance_id = db.Column(db.String(64))
    instance_name = db.Column(db.String(120))
    name = db.Column(db.String(60))
    svc_type = db.Column(db.String(10))          # -> "type" in JSON
    target = db.Column(db.String(255))
    up = db.Column(db.Boolean)                   # nullable: None == unknown/unconfigured
    latency_ms = db.Column(db.Float)
    detail = db.Column(db.Text)
    error = db.Column(db.Text)
    source = db.Column(db.String(20))


class Alert(db.Model):
    __tablename__ = "alerts"
    id = db.Column(db.Integer, primary_key=True)
    scan_run_id = db.Column(db.Integer, db.ForeignKey("scan_runs.id"))
    subject = db.Column(db.String(255))
    recipients = db.Column(db.JSON)              # list[str]
    sent = db.Column(db.Boolean, default=False)
    mode = db.Column(db.String(10))
    trigger = db.Column(db.String(10))           # "auto" | "manual"
    reason = db.Column(db.Text)
    sent_at = db.Column(db.String(40))           # ISO-8601 UTC string
    preview_html = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
