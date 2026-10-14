"""Which CloudMonitor metric set an instance is scanned with.

ECS and ApsaraDB RDS publish to different namespaces under different metric
names. Sending an RDS instance to `acs_ecs_dashboard` is not an error — the API
just returns no datapoints — so the failure is silent: the dashboard shows "-"
forever and no threshold is ever evaluated. These tests pin the routing.
"""
import config


def _inst(**kw):
    base = {"id": "i-abc123", "name": "Box", "role": "backend", "group": "Staging"}
    base.update(kw)
    return base


def _by_suffix(metrics):
    return {m["key"].rsplit("_", 1)[1]: m for m in metrics}


# --- routing ----------------------------------------------------------------
def test_ecs_instance_uses_the_ecs_namespace():
    m = _by_suffix(config.build_metrics([_inst()]))
    assert m["cpu"]["namespace"] == "acs_ecs_dashboard"
    assert m["cpu"]["metric_name"] == "CPUUtilization"


def test_rds_role_uses_the_rds_namespace():
    m = _by_suffix(config.build_metrics([_inst(id="pgm-d9jx", role="rds")]))
    assert {x["namespace"] for x in m.values()} == {"acs_rds_dashboard"}
    assert [m[s]["metric_name"] for s in ("cpu", "mem", "disk")] == [
        "CpuUsage", "MemoryUsage", "DiskUsage"]


def test_apsaradb_id_is_detected_without_the_rds_role():
    # Registered under a generic role — the id prefix still routes it correctly,
    # so an already-registered instance starts reporting without being edited.
    m = _by_suffix(config.build_metrics([_inst(id="pgm-d9jx", role="db")]))
    assert m["cpu"]["namespace"] == "acs_rds_dashboard"
    m = _by_suffix(config.build_metrics([_inst(id="rm-abc", role="")]))
    assert m["cpu"]["namespace"] == "acs_rds_dashboard"


def test_ecs_id_is_never_treated_as_rds():
    assert config.metric_templates_for(_inst(id="i-pgm-lookalike")) is config.METRIC_TEMPLATES


# --- shape the rest of the app depends on -----------------------------------
def test_rds_metrics_keep_the_common_descriptor_shape():
    metrics = config.build_metrics([_inst(id="pgm-d9jx", role="rds", name="ApsaraDB")])
    assert len(metrics) == 3
    for m in metrics:
        assert m["dimensions"] == '[{"instanceId":"pgm-d9jx"}]'
        assert m["instance_name"] == "ApsaraDB"
        assert m["group"] == "Staging"
        # RDS metrics come from the managed service — no CloudMonitor agent, so
        # a missing value here means a real problem, not an uninstalled agent.
        assert m["agent_required"] is False


def test_rds_thresholds_match_the_ecs_ones():
    ecs = {t["suffix"]: (t["threshold"], t["comparison"]) for t in config.METRIC_TEMPLATES}
    rds = {t["suffix"]: (t["threshold"], t["comparison"]) for t in config.METRIC_TEMPLATES_RDS}
    assert ecs == rds


def test_rds_role_is_offered_in_the_register_form():
    # instances_api._roles() derives the dropdown from this map.
    assert "rds" in config.SERVICE_CHECKS_BY_ROLE


# --- the request actually sent to CloudMonitor ------------------------------
def test_blank_period_is_omitted_from_the_api_call(monkeypatch):
    """A Period the metric is not published at returns an empty datapoint list
    (not an error), so RDS omits it and lets CloudMonitor choose."""
    from agents import agent1_scanner

    sent = {}

    def fake_rpc(domain, version, action, params, region=None):
        sent.update(params)
        return {"Datapoints": '[{"timestamp": 1, "Average": 12.5}]'}

    monkeypatch.setattr(agent1_scanner, "do_rpc", fake_rpc)

    metric = config.build_metrics([_inst(id="pgm-d9jx", role="rds")])[0]
    assert metric["period"] == ""            # config default
    agent1_scanner._query_metric_last(metric)
    assert "Period" not in sent
    assert sent["Namespace"] == "acs_rds_dashboard"

    sent.clear()
    ecs = config.build_metrics([_inst()])[0]
    agent1_scanner._query_metric_last(ecs)
    assert sent["Period"] == "60"            # ECS still pins its granularity


# --- SSH probe port resolution ----------------------------------------------
def test_ssh_port_defaults_to_22(monkeypatch):
    monkeypatch.delenv("PROBE_SSH_PORT", raising=False)
    monkeypatch.delenv("PROBE_FRONTEND_SSH_PORT", raising=False)
    assert config.ssh_port_for_role("frontend") == 22
    assert config.ssh_port_for_role("") == 22


def test_ssh_port_global_override(monkeypatch):
    monkeypatch.setenv("PROBE_SSH_PORT", "1818")
    monkeypatch.delenv("PROBE_DB_SSH_PORT", raising=False)
    assert config.ssh_port_for_role("db") == 1818


def test_ssh_port_per_role_beats_global(monkeypatch):
    """The real estate case: FE/BE moved sshd to 1818 while the DB box kept 22."""
    monkeypatch.setenv("PROBE_FRONTEND_SSH_PORT", "1818")
    monkeypatch.setenv("PROBE_BACKEND_SSH_PORT", "1818")
    monkeypatch.delenv("PROBE_SSH_PORT", raising=False)
    monkeypatch.delenv("PROBE_DB_SSH_PORT", raising=False)
    assert config.ssh_port_for_role("frontend") == 1818
    assert config.ssh_port_for_role("backend") == 1818
    assert config.ssh_port_for_role("db") == 22


def test_ssh_port_ignores_garbage(monkeypatch):
    monkeypatch.setenv("PROBE_FRONTEND_SSH_PORT", "not-a-port")
    assert config.ssh_port_for_role("frontend") == 22


def test_diagnostics_port_follows_the_role_unless_pinned(monkeypatch):
    monkeypatch.setenv("PROBE_FRONTEND_SSH_PORT", "1818")
    monkeypatch.setattr(config, "DIAG_SSH_PORT_RAW", "")
    assert config.diag_ssh_port_for_role("frontend") == 1818
    # explicit DIAG_SSH_PORT still wins
    monkeypatch.setattr(config, "DIAG_SSH_PORT_RAW", "2222")
    assert config.diag_ssh_port_for_role("frontend") == 2222
