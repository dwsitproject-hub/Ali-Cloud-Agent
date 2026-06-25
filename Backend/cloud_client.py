"""Thin factory around the Alibaba Cloud RPC SDK (AcsClient).

We use ``aliyun-python-sdk-core`` with ``CommonRequest`` so the same client can
talk to any Alibaba product (CloudMonitor, DirectMail, ...) without needing a
separate per-product SDK package. The signing, retries and HMAC are handled by
the core SDK.
"""
from __future__ import annotations

import config

_client = None


def get_client():
    """Return a cached AcsClient, or None when running in mock mode / no creds."""
    global _client
    if config.MOCK_MODE or not config.credentials_present():
        return None
    if _client is None:
        from aliyunsdkcore.client import AcsClient

        _client = AcsClient(
            config.ACCESS_KEY_ID,
            config.ACCESS_KEY_SECRET,
            config.CLOUDMONITOR_REGION,
        )
    return _client


def do_rpc(domain: str, version: str, action: str, params: dict,
           region: str | None = None) -> dict:
    """Execute a signed RPC request and return the parsed JSON response.

    domain : service endpoint, e.g. "metrics.cn-hangzhou.aliyuncs.com"
    """
    import json

    from aliyunsdkcore.client import AcsClient
    from aliyunsdkcore.request import CommonRequest

    # DirectMail and CloudMonitor live in (potentially) different regions, so
    # build a request-scoped client when a region override is supplied.
    if region and region != config.CLOUDMONITOR_REGION:
        client = AcsClient(config.ACCESS_KEY_ID, config.ACCESS_KEY_SECRET, region)
    else:
        client = get_client()
        if client is None:  # defensive — callers should gate on MOCK_MODE first
            raise RuntimeError("No Alibaba Cloud client available (mock mode / missing creds)")

    req = CommonRequest()
    req.set_accept_format("json")
    req.set_domain(domain)
    req.set_method("POST")
    req.set_protocol_type("https")
    req.set_version(version)
    req.set_action_name(action)
    for k, v in params.items():
        if v not in (None, ""):
            req.add_query_param(k, v)

    raw = client.do_action_with_exception(req)
    return json.loads(raw)
