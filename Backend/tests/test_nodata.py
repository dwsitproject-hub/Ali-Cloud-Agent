"""A metric with no value must never read as healthy.

Backend Staging sat at 95% memory for an hour without an alert: CloudMonitor's
DescribeMetricLast intermittently returned an empty datapoint list, that became
value=None, and None is not compared against a threshold - so the breach was
invisible. Two defences are tested here: re-asking over an explicit window, and
treating a metric that STOPS reporting as a problem in its own right.
"""
import config
from agents import agent1_scanner


def _metric(**kw):
    base = {"key": "i-x_mem", "label": "Memory", "namespace": "acs_ecs_dashboard",
            "metric_name": "memory_usedutilization", "period": "60",
            "dimensions": '[{"instanceId":"i-x"}]', "stat": "Average",
            "threshold": 85.0, "comparison": ">", "unit": "%",
            "instance_id": "i-x", "instance_name": "Backend Staging",
            "agent_required": True}
    base.update(kw)
    return base


# --- the lookback fallback --------------------------------------------------
def test_empty_last_falls_back_to_a_window(monkeypatch):
    calls = []

    def fake_rpc(domain, version, action, params, region=None):
        calls.append(action)
        if action == "DescribeMetricLast":
            return {"Datapoints": "[]"}
        return {"Datapoints": '[{"timestamp": 100, "Average": 94.96}]'}

    monkeypatch.setattr(agent1_scanner, "do_rpc", fake_rpc)
    monkeypatch.setattr(config, "MOCK_MODE", False)
    monkeypatch.setattr(config, "credentials_present", lambda: True)

    out = agent1_scanner.scan_metric(_metric())
    assert calls == ["DescribeMetricLast", "DescribeMetricList"]
    assert out["value"] == 94.96
    assert out["breached"] is True          # the whole point: 94.96 > 85
    assert out["source"] == "cloudmonitor-lookback"
    assert out["error"] is None


def test_both_empty_reports_no_data_not_healthy(monkeypatch):
    monkeypatch.setattr(agent1_scanner, "do_rpc",
                        lambda *a, **k: {"Datapoints": "[]"})
    monkeypatch.setattr(config, "MOCK_MODE", False)
    monkeypatch.setattr(config, "credentials_present", lambda: True)

    out = agent1_scanner.scan_metric(_metric())
    assert out["value"] is None
    assert out["breached"] is False         # cannot claim a breach without a value
    assert out["error"] == "no datapoints returned"


# --- multi-device metrics ---------------------------------------------------
def test_disk_picks_the_fullest_filesystem():
    """diskusage_utilization returns one series per device. Taking whichever
    sorted last could hide a root filesystem at 99% behind a 3% partition."""
    points = [{"timestamp": 200, "Average": 3.10, "device": "/dev/vdb"},
              {"timestamp": 200, "Average": 99.20, "device": "/dev/vda1"},
              {"timestamp": 100, "Average": 40.0, "device": "/dev/vda1"}]
    picked = agent1_scanner._pick_point(points, _metric(comparison=">"))
    assert picked["Average"] == 99.20


def test_pick_point_honours_a_less_than_rule():
    points = [{"timestamp": 5, "Average": 80.0}, {"timestamp": 5, "Average": 12.0}]
    assert agent1_scanner._pick_point(points, _metric(comparison="<"))["Average"] == 12.0


def test_pick_point_prefers_the_newest_timestamp():
    points = [{"timestamp": 1, "Average": 99.0}, {"timestamp": 9, "Average": 5.0}]
    assert agent1_scanner._pick_point(points, _metric())["Average"] == 5.0


def test_pick_point_empty():
    assert agent1_scanner._pick_point([], _metric()) == {}


# --- run_scan surfaces no-data ---------------------------------------------
def test_scan_reports_no_data_count(monkeypatch):
    monkeypatch.setattr(agent1_scanner, "do_rpc",
                        lambda *a, **k: {"Datapoints": "[]"})
    monkeypatch.setattr(config, "MOCK_MODE", False)
    monkeypatch.setattr(config, "credentials_present", lambda: True)
    monkeypatch.setattr("models.enabled_instance_dicts",
                        lambda: [{"id": "i-x", "name": "Backend Staging",
                                  "role": "backend", "group": "Staging"}])
    scan = agent1_scanner.run_scan()
    assert scan["no_data_count"] == 3
    assert scan["breach_count"] == 0
    assert {m["label"] for m in scan["no_data"]} == {"CPU", "Memory", "Disk"}
