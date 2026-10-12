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


# --- reaching a managed instance that is not our own ------------------------
def test_explicit_dsn_lets_one_monitor_reach_another_instance(monkeypatch):
    """Production alerts for the whole estate, so it needs evidence from
    ApsaraDB Staging too - which is not the database it connects to."""
    monkeypatch.setattr(config, "DATABASE_URL", DSN)
    monkeypatch.setattr(config, "rdsdiag_dsns",
                        lambda: {"pgm-other": "postgresql://u:p@other:5432/db"})
    assert rdsdiag.dsn_for({"id": "pgm-other", "host": "other"}) == \
        "postgresql://u:p@other:5432/db"


def test_own_database_dsn_drops_the_sqlalchemy_driver_suffix(monkeypatch):
    """`postgresql+psycopg2://` is SQLAlchemy's spelling; libpq rejects it."""
    monkeypatch.setattr(config, "DATABASE_URL", DSN)
    monkeypatch.setattr(config, "rdsdiag_dsns", dict)
    out = rdsdiag.dsn_for({"id": "pgm-x", "host": OWN})
    assert out.startswith("postgresql://") and "+psycopg2" not in out


def test_no_dsn_means_no_credentials(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", DSN)
    monkeypatch.setattr(config, "rdsdiag_dsns", dict)
    assert rdsdiag.dsn_for({"id": "pgm-other", "host": "other"}) is None


def test_dsn_map_parsing(monkeypatch):
    monkeypatch.setenv("RDSDIAG_DSNS", '{"pgm-a":"postgresql://u:p,x@h:5432/d"}')
    # a password containing a comma survives, which is why this is JSON and not
    # the `id:value,id:value` form the PROBE_* overrides use
    assert config.rdsdiag_dsns() == {"pgm-a": "postgresql://u:p,x@h:5432/d"}


def test_malformed_dsn_map_disables_rather_than_crashes(monkeypatch):
    """Evidence is a nice-to-have; alerting is not. Bad config must not take a
    scan cycle down with it."""
    monkeypatch.setenv("RDSDIAG_DSNS", "not json at all")
    assert config.rdsdiag_dsns() == {}
    monkeypatch.setenv("RDSDIAG_DSNS", '["a","b"]')
    assert config.rdsdiag_dsns() == {}


# --- SQL that only fails against a real server ------------------------------
def test_no_positional_group_by():
    """`GROUP BY 1` refers to the OUTPUT column, which in these queries is a
    concatenated string containing count(*) - PostgreSQL rejects it with
    GroupingError. Caught only at runtime, so guard it here."""
    import re
    for title, sql, _f in rdsdiag._SECTIONS:
        for clause in re.findall(r"group\s+by\s+([^\n]+)", sql, re.I):
            assert not re.match(r"^\s*\d", clause), f"{title}: GROUP BY {clause.strip()}"


def test_password_with_a_hash_survives(monkeypatch):
    """_clean() strips from the first '#'; a DSN must not go through it."""
    monkeypatch.setenv("RDSDIAG_DSNS", '{"pgm-a":"postgresql://u:pa#ss@h:5432/d"}')
    assert config.rdsdiag_dsns() == {"pgm-a": "postgresql://u:pa#ss@h:5432/d"}
