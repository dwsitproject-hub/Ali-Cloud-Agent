"""In-memory shared state for the latest scan + alert results.

Thread-safe enough for the single-process Flask + APScheduler setup. For a
multi-process / multi-instance deployment, swap this for Redis or a database.
"""
from __future__ import annotations

import threading
from collections import deque

_lock = threading.Lock()
_state = {
    "last_scan": None,     # most recent scan result document
    "last_alert": None,    # most recent alert send summary
    "history": deque(maxlen=50),  # recent scans (newest first)
}


def set_last_scan(scan: dict) -> None:
    with _lock:
        _state["last_scan"] = scan
        _state["history"].appendleft({
            "scanned_at": scan.get("scanned_at"),
            "mode": scan.get("mode"),
            "breach_count": scan.get("breach_count", 0),
            "total": scan.get("total", 0),
        })


def set_last_alert(alert: dict) -> None:
    with _lock:
        _state["last_alert"] = alert


def snapshot() -> dict:
    with _lock:
        return {
            "last_scan": _state["last_scan"],
            "last_alert": _state["last_alert"],
            "history": list(_state["history"]),
        }
