"""Service health checks (Datadog-style liveness probes).

Runs lightweight network probes against each configured service:
  * tcp  - open a socket to host:port (is the service accepting connections?)
  * http - GET scheme://host:port`path` and check the status code + latency

These confirm a service (often a docker container's published port) is up and
responding. They do NOT enumerate docker containers/images - that needs SSH or
the Docker API, which can be added as a separate tier.

In MOCK_MODE values are synthesised so the UI works without network access.
"""
from __future__ import annotations

import random
import socket
import ssl
import time
import urllib.request
from datetime import datetime, timezone

import config


def _tcp_probe(host: str, port: int, timeout: float):
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ms = round((time.monotonic() - start) * 1000, 1)
            return True, ms, f"connected to {host}:{port}", None
    except Exception as exc:
        ms = round((time.monotonic() - start) * 1000, 1)
        return False, ms, None, f"{type(exc).__name__}: {exc}"


def _http_probe(url: str, expect, timeout: float):
    start = time.monotonic()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # tolerate self-signed certs on internal hosts
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "CloudAgentMonitoring/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            code = resp.getcode()
            ms = round((time.monotonic() - start) * 1000, 1)
            ok = code in expect
            return ok, ms, f"HTTP {code}", None if ok else f"unexpected status {code}"
    except urllib.error.HTTPError as exc:
        ms = round((time.monotonic() - start) * 1000, 1)
        ok = exc.code in expect
        return ok, ms, f"HTTP {exc.code}", None if ok else f"unexpected status {exc.code}"
    except Exception as exc:
        ms = round((time.monotonic() - start) * 1000, 1)
        return False, ms, None, f"{type(exc).__name__}: {exc}"


def _mock(check):
    up = random.random() > 0.18  # ~18% down, so the UI shows some red
    ms = round(random.uniform(2, 120), 1)
    detail = "mock up" if up else "mock down"
    return up, (ms if up else None), detail, (None if up else "mock: connection refused")


def check_one(check: dict) -> dict:
    host = check.get("host", "")
    result = {
        "key": check["key"], "group": check["group"],
        "instance_id": check["instance_id"], "instance_name": check["instance_name"],
        "name": check["name"], "type": check["type"],
        "target": "", "up": None, "latency_ms": None,
        "detail": None, "error": None, "source": "probe",
    }

    if config.MOCK_MODE:
        up, ms, detail, err = _mock(check)
        result.update(up=up, latency_ms=ms, detail=detail, error=err, source="mock",
                      target=f"{check['type']}://{check['name']}")
        return result

    if not host:
        result.update(up=None, detail="no host configured", source="unconfigured",
                      target="(set host in config)")
        return result

    if check["type"] == "http":
        url = f"{check['scheme']}://{host}:{check['port']}{check['path']}"
        up, ms, detail, err = _http_probe(url, check["expect"], config.HEALTHCHECK_TIMEOUT)
        result["target"] = url
    else:
        up, ms, detail, err = _tcp_probe(host, check["port"], config.HEALTHCHECK_TIMEOUT)
        result["target"] = f"{host}:{check['port']}"

    result.update(up=up, latency_ms=ms, detail=detail, error=err)
    return result


def run_health_checks() -> dict:
    """Probe every configured service. Returns a structured document.

    Service checks are derived from the DB-backed instance inventory at probe
    time (by each instance's role)."""
    from models import enabled_instance_dicts

    checks = config.build_service_checks(enabled_instance_dicts())
    results = [check_one(c) for c in checks]
    down = [r for r in results if r["up"] is False]
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "mode": "mock" if config.MOCK_MODE else "live",
        "total": len(results),
        "down_count": len(down),
        "results": results,
        "down": down,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run_health_checks(), indent=2))
