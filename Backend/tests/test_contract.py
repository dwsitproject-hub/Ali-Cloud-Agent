"""Golden contract test — the dashboard JS contract must not drift.

`Frontend/templates/dashboard.html` (currently `templates/dashboard.html`) reads
specific field names off three JSON shapes:
  * the direct return of POST /api/scan and GET /api/status -> scan document
  * the alert object (POST /api/send / status.last_alert)
  * status.history[] rows

This test boots the app in MOCK_MODE and asserts every field the frontend reads
is present with an acceptable type. It is the regression guard for the whole
production refactor: each phase must keep these shapes byte-stable.

The authenticated `client` fixture (conftest) logs in via the dev-login bypass.
"""


# --- Field contracts the dashboard JS depends on ---------------------------
SCAN_TOP = {
    "scanned_at", "mode", "total", "breach_count",
    "results", "services", "services_down_count", "services_total",
}
RESULT_FIELDS = {
    "group", "instance_id", "instance_name", "label", "value", "unit",
    "breached", "comparison", "threshold", "source", "error",
}
SERVICE_FIELDS = {
    "instance_id", "name", "up", "latency_ms", "error", "detail", "target",
}
ALERT_FIELDS = {"subject", "recipients", "sent", "sent_at"}  # trigger/reason/preview_html optional
HISTORY_FIELDS = {"scanned_at", "mode", "breach_count", "total"}


def _assert_scan_shape(scan):
    assert SCAN_TOP <= set(scan), f"missing scan keys: {SCAN_TOP - set(scan)}"
    assert isinstance(scan["results"], list) and scan["results"], "results empty"
    assert isinstance(scan["services"], list)
    for r in scan["results"]:
        assert RESULT_FIELDS <= set(r), f"missing result keys: {RESULT_FIELDS - set(r)}"
        assert r["value"] is None or isinstance(r["value"], (int, float))
        assert isinstance(r["breached"], bool)
    for s in scan["services"]:
        assert SERVICE_FIELDS <= set(s), f"missing service keys: {SERVICE_FIELDS - set(s)}"
        assert s["up"] in (True, False, None)


def test_scan_endpoint_shape(client):
    resp = client.post("/api/scan")
    assert resp.status_code == 200
    _assert_scan_shape(resp.get_json())


def test_status_endpoint_shape(client):
    client.post("/api/scan")  # ensure there is a last_scan
    resp = client.get("/api/status")
    assert resp.status_code == 200
    snap = resp.get_json()
    assert {"last_scan", "last_alert", "history", "config"} <= set(snap)
    _assert_scan_shape(snap["last_scan"])
    assert {"mode", "interval_minutes", "auto_alert_on_breach", "recipients"} <= set(snap["config"])
    for h in snap["history"]:
        assert HISTORY_FIELDS <= set(h), f"missing history keys: {HISTORY_FIELDS - set(h)}"


def test_send_endpoint_shape(client):
    client.post("/api/scan")
    resp = client.post("/api/send")
    assert resp.status_code == 200
    alert = resp.get_json()
    assert ALERT_FIELDS <= set(alert), f"missing alert keys: {ALERT_FIELDS - set(alert)}"
    assert isinstance(alert["recipients"], list)
