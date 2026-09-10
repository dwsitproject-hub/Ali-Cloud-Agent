"""On-breach SSH diagnostics — identify WHICH service caused a breach and WHAT
it was doing.

CloudMonitor tells us *that* a host is at 93% CPU; it cannot tell us that
`klip-backend` was running a 111 KB report query. This module closes that gap:
when a metric breaches, it SSHes to the affected host and runs a single
read-only script (``cam-diag``, installed by the host owner) which reports the
top processes, the busiest containers, and — on database hosts — the live
queries with their duration. The output is embedded in the alert email.

Security design:
  * connects as a dedicated **unprivileged** user with its own SSH key;
  * runs exactly ONE whitelisted command (``sudo -n <DIAG_REMOTE_SCRIPT> <focus>``),
    where ``focus`` is validated against a fixed allow-list — no app data is ever
    interpolated into a shell command;
  * the monitored host grants that single script via a sudoers entry, so the
    monitor cannot run anything else and needs no ``docker`` group membership;
  * hard timeouts, a cap on hosts per alert, and it never raises into the scan
    cycle — a failed collection just means the alert falls back to generic
    guidance.

Enable with DIAG_ENABLED=true plus DIAG_SSH_USER / DIAG_SSH_KEY.
See Docs/DIAGNOSTICS-SETUP.md for the per-host provisioning steps.
"""
from __future__ import annotations

import logging

import config

log = logging.getLogger("diagnostics")

# Metric label -> the focus argument passed to the remote script.
_FOCUS_BY_LABEL = {"CPU": "cpu", "Memory": "memory", "Disk": "disk"}
_ALLOWED_FOCUS = {"cpu", "memory", "disk", "service", "containers", "all"}


def enabled() -> bool:
    return bool(config.DIAG_ENABLED and config.DIAG_SSH_USER and config.DIAG_SSH_KEY)


def _run_remote(host: str, focus: str, role: str = "") -> str | None:
    """SSH to ``host`` and return the diagnostic text, or None on failure.

    ``role`` selects the SSH port via ``config.ssh_port_for_role`` so evidence
    collection reaches hosts whose sshd is not on 22 — the same setting that fixes
    the SSH service probe. DIAG_SSH_PORT still wins when set explicitly, for the
    case where diagnostics uses a different port than the probe.
    """
    if focus not in _ALLOWED_FOCUS:      # defensive: never pass through unknown input
        focus = "all"
    try:
        import paramiko
    except ImportError:                   # pragma: no cover - dependency missing
        log.warning("paramiko not installed; SSH diagnostics unavailable")
        return None

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if config.DIAG_STRICT_HOST_KEY:
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=config.diag_ssh_port_for_role(role),
            username=config.DIAG_SSH_USER,
            key_filename=config.DIAG_SSH_KEY,
            timeout=config.DIAG_TIMEOUT,
            banner_timeout=config.DIAG_TIMEOUT,
            auth_timeout=config.DIAG_TIMEOUT,
            allow_agent=False,
            look_for_keys=False,
        )
        cmd = f"sudo -n {config.DIAG_REMOTE_SCRIPT} {focus}"
        _in, out, err = client.exec_command(cmd, timeout=config.DIAG_TIMEOUT)
        text = out.read().decode("utf-8", "replace").strip()
        problem = err.read().decode("utf-8", "replace").strip()
        # Close the streams while the transport is still alive. Left to the
        # garbage collector, their __del__ can run after `finally` has closed the
        # client, and paramiko then raises inside __del__ ("'NoneType' object has
        # no attribute 'time'"). Python only prints that as an ignored exception,
        # so it breaks nothing - it just writes a traceback into the logs of a
        # long-running process for no reason.
        for stream in (_in, out, err):
            try:
                stream.close()
            except Exception:
                pass
        if not text:
            return f"(no output; stderr: {problem[:300]})" if problem else None
        if len(text) > config.DIAG_MAX_CHARS:
            text = text[:config.DIAG_MAX_CHARS] + "\n... (truncated)"
        return text
    except Exception as exc:
        log.warning("diagnostics: %s failed: %s: %s", host, type(exc).__name__, exc)
        return None
    finally:
        try:
            client.close()
        except Exception:
            pass


def run_focus(host: str, focus: str, role: str = "") -> str | None:
    """Public entry point for one remote focus.

    containers.py needs the same SSH path (same user, same single whitelisted
    sudo command, same per-role port) without reaching into a private helper, and
    without gaining any capability the breach-evidence path does not already have.
    """
    return _run_remote(host, focus, role)


def _hosts_for_instances(instance_ids: set) -> dict:
    """Map instance_id -> {host, role} from the DB inventory (blank hosts skipped).

    The role comes along because it decides which SSH port to use (see
    ``config.ssh_port_for_role``).
    """
    try:
        from models import Instance
        rows = Instance.query.filter(Instance.id.in_(list(instance_ids))).all()
        return {r.id: {"host": (r.host or "").strip(), "role": (r.role or "").strip()}
                for r in rows if (r.host or "").strip()}
    except Exception as exc:
        log.warning("diagnostics: could not resolve hosts: %s", exc)
        return {}


def collect_for_scan(scan: dict) -> dict:
    """Collect diagnostics for the instances implicated in this scan.

    Returns ``{instance_name: {"host":…, "focus":…, "output":…}}`` — empty when
    disabled, when no host is configured, or when collection fails. Must be
    called inside a Flask app context (it reads the inventory).
    """
    if not enabled():
        return {}

    # Which instance breached, and what kind of problem does it have?
    wanted: dict = {}
    for b in scan.get("breaches", []):
        iid = b.get("instance_id")
        if not iid:
            continue
        entry = wanted.setdefault(iid, {"name": b.get("instance_name") or iid,
                                        "focuses": set()})
        entry["focuses"].add(_FOCUS_BY_LABEL.get(b.get("label"), "all"))
    for s in scan.get("services_down", []):
        iid = s.get("instance_id")
        if not iid:
            continue
        entry = wanted.setdefault(iid, {"name": s.get("instance_name") or iid,
                                       "focuses": set()})
        entry["focuses"].add("service")
    if not wanted:
        return {}

    hosts = _hosts_for_instances(set(wanted))
    results: dict = {}
    for iid, meta in list(wanted.items())[:config.DIAG_MAX_HOSTS]:
        target = hosts.get(iid) or {}
        host = target.get("host")
        if not host:
            continue      # no probe host configured for this instance
        focuses = meta["focuses"]
        focus = focuses.pop() if len(focuses) == 1 else "all"
        output = _run_remote(host, focus, target.get("role", ""))
        if output:
            results[meta["name"]] = {"host": host, "focus": focus, "output": output}
            log.info("diagnostics collected from %s (%s)", host, focus)
    return results
