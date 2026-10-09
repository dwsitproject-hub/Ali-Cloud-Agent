"""Evidence for a managed database, which has no shell to SSH into.

diagnostics.py answers "which service caused this" over SSH. ApsaraDB cannot
answer that way, so a database breach used to alert with generic guidance only -
precisely the breach where the offending statement matters most.
"""
import config
import rdsdiag

OWN = "pgm-d9jn3khh0b3907w4.pgsql.ap-southeast-5.rds.aliyuncs.com"
DSN = f"postgresql+psycopg2://u:p@{OWN}:5432/cloudagent"


def test_matches_the_monitors_own_database_by_host(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", DSN)
    assert rdsdiag.targets_our_own_database({"id": "pgm-x", "host": OWN}) is True


def test_matches_by_instance_id_when_host_is_blank(monkeypatch):
    """ApsaraDB endpoints are `<instance-id>.pgsql.<region>.rds.aliyuncs.com`, so
    an inventory row with no host is still recognisable."""
    monkeypatch.setattr(config, "DATABASE_URL", DSN)
    assert rdsdiag.targets_our_own_database(
        {"id": "pgm-d9jn3khh0b3907w4", "host": ""}) is True


def test_a_different_managed_instance_is_not_ours(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", DSN)
    assert rdsdiag.targets_our_own_database(
        {"id": "pgm-other", "host": "pgm-other.pgsql.ap-southeast-5.rds.aliyuncs.com"}) is False


def test_id_prefix_must_be_a_whole_label(monkeypatch):
    """`pgm-d9` must not match `pgm-d9jn3khh0b3907w4...` - a partial id would
    attribute another instance's queries to this one."""
    monkeypatch.setattr(config, "DATABASE_URL", DSN)
    assert rdsdiag.targets_our_own_database({"id": "pgm-d9", "host": ""}) is False


def test_no_database_url_matches_nothing(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    assert rdsdiag.targets_our_own_database({"id": "pgm-x", "host": OWN}) is False


# --- behaviour when we cannot help ------------------------------------------
def test_other_instances_get_an_explanation_not_silence(monkeypatch):
    """Returning None would read as "collection failed". Saying why is the
    difference between a gap someone can act on and one they cannot."""
    monkeypatch.setattr(config, "DATABASE_URL", DSN)
    monkeypatch.setattr(config, "RDSDIAG_ENABLED", True)
    out = rdsdiag.collect({"id": "pgm-other", "host": "pgm-other.rds.aliyuncs.com"})
    assert out and "no credentials" in out


def test_disabled_collects_nothing(monkeypatch):
    monkeypatch.setattr(config, "RDSDIAG_ENABLED", False)
    assert rdsdiag.collect({"id": "pgm-x", "host": OWN}) is None


# --- the queries are scoped to what breached --------------------------------
def test_disk_focus_asks_about_size_not_about_queries():
    titles = {t for t, _sql, focuses in rdsdiag._SECTIONS if "disk" in focuses}
    assert "LARGEST TABLES" in titles
    assert "ACTIVE QUERIES (oldest first)" not in titles


def test_cpu_focus_asks_what_is_running_now():
    titles = {t for t, _sql, focuses in rdsdiag._SECTIONS if "cpu" in focuses}
    assert "ACTIVE QUERIES (oldest first)" in titles
    assert "CONNECTIONS BY CLIENT" in titles


def test_every_section_is_read_only():
    """A diagnostic must never be able to change the database it is inspecting."""
    banned = ("insert ", "update ", "delete ", "drop ", "alter ", "truncate ", "create ")
    for _title, sql, _f in rdsdiag._SECTIONS:
        low = sql.lower()
        assert low.strip().startswith("select"), sql[:40]
        assert not any(b in low for b in banned), sql[:40]
