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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

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
        "Length": "1",
    }
    # Period is optional: asking for a granularity the metric is not published at
    # returns an empty result rather than an error, so a blank period lets
    # CloudMonitor choose (useful for RDS, where basic monitoring is 300s).
    if metric.get("period"):
        params["Period"] = metric["period"]
    if metric.get("dimensions"):
        params["Dimensions"] = metric["dimensions"]

    resp = do_rpc(_cms_domain(), CMS_VERSION, "DescribeMetricLast", params,
                  region=config.CLOUDMONITOR_REGION)

    return _pick_point(_datapoints(resp), metric)


def _datapoints(resp: dict) -> list:
    """Datapoints is a JSON-encoded *string* per the API contract."""
    try:
        return json.loads(resp.get("Datapoints") or "[]") or []
    except (TypeError, json.JSONDecodeError):
        return []


def _pick_point(points: list, metric: dict) -> dict:
    """The datapoint that matters: newest timestamp, worst value at that instant.

    Some metrics return one series *per device* - `diskusage_utilization` reports
    every mounted filesystem. Taking whichever happens to sort last means a root
    filesystem at 100% can be masked by a small partition at 3%, so among the
    newest points we keep the one closest to breaching. Single-series metrics
    (CPU, memory) are unaffected.
    """
    if not points:
        return {}
    newest = max(p.get("timestamp", 0) for p in points)
    tied = [p for p in points if p.get("timestamp", 0) == newest]
    if len(tied) == 1:
        return tied[0]
    stat = metric.get("stat", "Average")
    worst = min if metric.get("comparison", ">") in ("<", "<=") else max
    return worst(tied, key=lambda p: p.get(stat) if p.get(stat) is not None else 0)


def _query_metric_recent(metric: dict) -> dict:
    """Fallback: newest datapoint from an explicit recent window.

    DescribeMetricLast intermittently returns an empty list for agent-reported
    metrics (memory/disk) - it has a narrow lookback, so a datapoint that has not
    landed yet reads as no data at all. That empty result became value=None, and
    a None value is not compared against the threshold, so it counted as healthy:
    Backend Staging sat at 95% memory for an hour without alerting. Querying an
    explicit window does not have that blind spot.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=config.METRIC_LOOKBACK_MINUTES)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    params = {
        "Namespace": metric["namespace"],
        "MetricName": metric["metric_name"],
        "StartTime": start.strftime(fmt),
        "EndTime": end.strftime(fmt),
        "Length": "1000",
    }
    if metric.get("period"):
        params["Period"] = metric["period"]
    if metric.get("dimensions"):
        params["Dimensions"] = metric["dimensions"]

    resp = do_rpc(_cms_domain(), CMS_VERSION, "DescribeMetricList", params,
                  region=config.CLOUDMONITOR_REGION)
    return _pick_point(_datapoints(resp), metric)


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
            if not point:
                # Narrow-lookback blind spot: ask for an explicit window instead
                # of concluding "no data" (and therefore "healthy").
                point = _query_metric_recent(metric)
                if point:
                    source = "cloudmonitor-lookback"
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
    """Scan every configured metric. Returns a structured result document.

    The inventory is read from the DB at scan time, so instances added via the
    register page are picked up on the next cycle.
    """
    from models import enabled_instance_dicts

    metrics = config.build_metrics(enabled_instance_dicts())
    # Each scan_metric is an independent network call (or a cheap mock); run
    # them concurrently so a full cycle isn't the sum of every request's
    # latency. ThreadPoolExecutor.map preserves input order.
    if metrics:
        workers = min(config.SCAN_CONCURRENCY, len(metrics))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(scan_metric, metrics))
    else:
        results = []
    breaches = [r for r in results if r["breached"]]
    # Metrics that produced no value at all. NOT the same as "healthy": whether
    # one is worth alerting on depends on whether it was reporting before (see
    # state.recent_problem_key_sets), which needs history this function lacks.
    no_data = [r for r in results if r["value"] is None]
    return {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "mode": "mock" if (config.MOCK_MODE or not config.credentials_present()) else "live",
        "total": len(results),
        "breach_count": len(breaches),
        "results": results,
        "breaches": breaches,
        "no_data": no_data,
        "no_data_count": len(no_data),
    }


if __name__ == "__main__":
    print(json.dumps(run_scan(), indent=2))
