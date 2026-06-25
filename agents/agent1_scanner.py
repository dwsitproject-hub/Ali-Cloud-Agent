"""Agent 1 — CloudMonitor scanner.

Queries the latest value of each configured metric using the real Alibaba
CloudMonitor ``DescribeMetricLast`` API, then evaluates it against its alert
threshold. In MOCK_MODE it produces synthetic values so the app runs end-to-end
without credentials.

API reference:
  https://www.alibabacloud.com/help/en/cms/cloudmonitor-1-0/developer-reference/api-cms-2019-01-01-describemetriclast
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone

import config
from cloud_client import do_rpc

CMS_VERSION = "2019-01-01"


def _cms_domain() -> str:
    return f"metrics.{config.CLOUDMONITOR_REGION}.aliyuncs.com"


def _breached(value, threshold, comparison) -> bool:
    if value is None:
        return False
    return {
        ">": value > threshold,
        ">=": value >= threshold,
        "<": value < threshold,
        "<=": value <= threshold,
    }.get(comparison, value > threshold)


def _query_metric_last(metric: dict) -> dict:
    """Real CloudMonitor call -> latest datapoint dict (or {} if none)."""
    params = {
        "Namespace": metric["namespace"],
        "MetricName": metric["metric_name"],
        "Period": metric.get("period", "60"),
        "Length": "1",
    }
    if metric.get("dimensions"):
        params["Dimensions"] = metric["dimensions"]

    resp = do_rpc(_cms_domain(), CMS_VERSION, "DescribeMetricLast", params,
                  region=config.CLOUDMONITOR_REGION)

    # Datapoints is a JSON-encoded *string* per the API contract.
    raw_points = resp.get("Datapoints") or "[]"
    try:
        points = json.loads(raw_points)
    except (TypeError, json.JSONDecodeError):
        points = []
    if not points:
        return {}
    # Most recent datapoint is last by timestamp.
    return sorted(points, key=lambda p: p.get("timestamp", 0))[-1]


def _mock_value(metric: dict) -> float:
    """Synthetic value that occasionally breaches, for demo/dev."""
    t = metric["threshold"]
    # 25% of the time, push the value past the threshold so alerts can be seen.
    if random.random() < 0.25:
        return round(t + random.uniform(1, 12), 2)
    return round(max(0.0, t - random.uniform(5, 35)), 2)


def scan_metric(metric: dict) -> dict:
    stat = metric.get("stat", "Average")
    error = None
    value = None

    if config.MOCK_MODE or not config.credentials_present():
        value = _mock_value(metric)
        source = "mock"
    else:
        source = "cloudmonitor"
        try:
            point = _query_metric_last(metric)
            if point:
                value = point.get(stat)
                if value is not None:
                    value = round(float(value), 2)
            else:
                error = "no datapoints returned"
        except Exception as exc:  # surface API/auth errors per-metric, keep scan alive
            error = f"{type(exc).__name__}: {exc}"

    breached = _breached(value, metric["threshold"], metric["comparison"]) if value is not None else False

    return {
        "key": metric["key"],
        "label": metric["label"],
        "group": metric.get("group", "Ungrouped"),
        "instance_id": metric.get("instance_id", ""),
        "instance_name": metric.get("instance_name", ""),
        "namespace": metric["namespace"],
        "metric_name": metric["metric_name"],
        "stat": stat,
        "value": value,
        "unit": metric.get("unit", ""),
        "threshold": metric["threshold"],
        "comparison": metric["comparison"],
        "agent_required": metric.get("agent_required", False),
        "breached": breached,
        "source": source,
        "error": error,
    }


def run_scan() -> dict:
    """Scan every configured metric. Returns a structured result document."""
    results = [scan_metric(m) for m in config.METRICS]
    breaches = [r for r in results if r["breached"]]
    return {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "mode": "mock" if (config.MOCK_MODE or not config.credentials_present()) else "live",
        "total": len(results),
        "breach_count": len(breaches),
        "results": results,
        "breaches": breaches,
    }


if __name__ == "__main__":
    print(json.dumps(run_scan(), indent=2))
