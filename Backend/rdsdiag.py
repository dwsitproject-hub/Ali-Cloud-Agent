"""On-breach evidence for a managed database, collected over SQL.

``diagnostics.py`` answers "which service caused this" by SSHing to the host and
running a read-only script. A managed ApsaraDB instance has **no shell**, so that
path cannot work: the connection fails, the alert falls back to generic guidance,
and the one breach where you most want the offending statement is the one that
arrives with none.

This closes the gap for instances the monitor already holds a connection to -
namely its own database, which is what both ApsaraDB instances now are. For any
other managed instance we have no credentials, and this reports that plainly
rather than pretending.

Safety: every statement is read-only, capped by ``RDSDIAG_TIMEOUT`` via a
server-side ``statement_timeout``, and limited to a handful of rows. A diagnostic
query must never become part of the incident it is diagnosing.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

import config

log = logging.getLogger("rdsdiag")

MAX_ROWS = 8
QUERY_CHARS = 180


def _dsn_host() -> str:
    """Host of the monitor's own DATABASE_URL."""
    try:
        return (urlsplit(config.DATABASE_URL).hostname or "").lower()
    except Exception:
        return ""


def targets_our_own_database(inst: dict) -> bool:
    """Is this instance the database the monitor itself is connected to?

    Matched on the endpoint host, or on the instance id appearing in it -
    ApsaraDB endpoints are `<instance-id>.pgsql.<region>.rds.aliyuncs.com`, so a
    row whose `host` was never filled in is still recognisable.
    """
    dsn_host = _dsn_host()
    if not dsn_host:
        return False
    host = (inst.get("host") or "").strip().lower()
    inst_id = (inst.get("id") or "").strip().lower()
    return bool((host and host == dsn_host)
                or (inst_id and dsn_host.startswith(inst_id + ".")))


def enabled() -> bool:
    return bool(config.RDSDIAG_ENABLED and config.DATABASE_URL)


# --- the queries -------------------------------------------------------------
# Each is (title, sql, focuses). "service" covers a down probe, where the useful
# question is who is connected rather than what is slow.
_SECTIONS = [
    ("CONNECTIONS BY STATE",
     """SELECT coalesce(state,'(none)') || '  x' || count(*)::text
          FROM pg_stat_activity WHERE datname = current_database()
         GROUP BY 1 ORDER BY count(*) DESC""",
     {"cpu", "memory", "service", "all"}),

    ("CONNECTIONS BY CLIENT",
     """SELECT coalesce(client_addr::text,'local') || '  ' ||
               coalesce(nullif(application_name,''),'(no app name)') ||
               '  x' || count(*)::text
          FROM pg_stat_activity WHERE datname = current_database()
         GROUP BY 1,2 ORDER BY count(*) DESC""",
     {"cpu", "memory", "service", "all"}),

    # The money query: what is running right now, oldest first. A statement that
    # has been active for minutes is the thing holding the CPU.
    ("ACTIVE QUERIES (oldest first)",
     """SELECT coalesce(to_char(now() - query_start,'HH24:MI:SS'),'--') || '  ' ||
               state || '  ' || coalesce(client_addr::text,'local') || '  ' ||
               coalesce(nullif(application_name,''),'-') || '  ' ||
               left(regexp_replace(query, '\\s+', ' ', 'g'), %d)
          FROM pg_stat_activity
         WHERE datname = current_database() AND pid <> pg_backend_pid()
           AND state <> 'idle'
         ORDER BY query_start NULLS LAST""" % QUERY_CHARS,
     {"cpu", "memory", "service", "all"}),

    ("LONGEST TRANSACTIONS",
     """SELECT coalesce(to_char(now() - xact_start,'HH24:MI:SS'),'--') || '  ' ||
               state || '  ' || coalesce(client_addr::text,'local')
          FROM pg_stat_activity
         WHERE datname = current_database() AND xact_start IS NOT NULL
           AND pid <> pg_backend_pid()
         ORDER BY xact_start""",
     {"cpu", "memory", "all"}),

    ("LARGEST TABLES",
     """SELECT c.relname || '  ' || pg_size_pretty(pg_total_relation_size(c.oid))
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname NOT IN ('pg_catalog','information_schema')
           AND c.relkind = 'r'
         ORDER BY pg_total_relation_size(c.oid) DESC""",
     {"disk", "all"}),

    ("DATABASE SIZE",
     "SELECT current_database() || '  ' || pg_size_pretty(pg_database_size(current_database()))",
     {"disk", "memory", "all"}),
]

# Optional: needs the extension, which a managed instance may not have enabled.
_STATEMENTS_SQL = """
SELECT round(total_exec_time)::text || 'ms total  x' || calls::text ||
       '  avg ' || round(mean_exec_time,1)::text || 'ms  ' ||
       left(regexp_replace(query, '\\s+', ' ', 'g'), %d)
  FROM pg_stat_statements
 ORDER BY total_exec_time DESC
""" % QUERY_CHARS


def _rows(sql: str, limit: int = MAX_ROWS) -> list:
    from sqlalchemy import text

    from db import db

    result = db.session.execute(text(f"SELECT * FROM ({sql}) q LIMIT {int(limit)}"))
    return [str(r[0]) for r in result.fetchall()]


def collect(inst: dict, focus: str = "all") -> str | None:
    """Read-only evidence for one managed database, or None if unavailable.

    Returns text in the same shape ``cam-diag`` produces, so the alert email and
    the dashboard render it through the existing evidence path with no special
    casing.
    """
    if not enabled():
        return None
    if not targets_our_own_database(inst):
        # Honest about the limitation rather than silently empty: the operator
        # should know WHY there is no evidence for this instance.
        return ("(no evidence: this managed instance is not the database this "
                "monitor connects to, so there are no credentials for it. "
                "Set RDSDIAG_ENABLED=false to stop asking.)")

    from sqlalchemy import text

    from db import db

    blocks = [f"== MANAGED DATABASE ({inst.get('name') or inst.get('id')}) =="]
    try:
        # Server-side cap: a diagnostic must not queue behind the incident.
        db.session.execute(
            text(f"SET LOCAL statement_timeout = '{int(config.RDSDIAG_TIMEOUT)}s'"))

        for title, sql, focuses in _SECTIONS:
            if focus not in focuses:
                continue
            try:
                rows = _rows(sql)
            except Exception as exc:
                blocks.append(f"\n== {title} ==\n(failed: {type(exc).__name__})")
                continue
            body = "\n".join(rows) if rows else "(none)"
            blocks.append(f"\n== {title} ==\n{body}")

        if focus in ("cpu", "memory", "all"):
            try:
                has_ext = _rows("SELECT 1 FROM pg_extension WHERE extname='pg_stat_statements'", 1)
                if has_ext:
                    rows = _rows(_STATEMENTS_SQL)
                    blocks.append("\n== TOP STATEMENTS BY TOTAL TIME ==\n"
                                  + ("\n".join(rows) if rows else "(none)"))
                else:
                    blocks.append("\n== TOP STATEMENTS BY TOTAL TIME ==\n"
                                  "(pg_stat_statements not enabled - turn it on in the "
                                  "ApsaraDB parameter group to rank queries by cost)")
            except Exception as exc:
                blocks.append(f"\n== TOP STATEMENTS BY TOTAL TIME ==\n(failed: {type(exc).__name__})")

        return "\n".join(blocks)
    except Exception as exc:
        log.warning("rdsdiag: collection failed: %s: %s", type(exc).__name__, exc)
        return None
    finally:
        # SET LOCAL dies with the transaction; roll back so the scan's own
        # session is not left carrying a diagnostic timeout.
        try:
            db.session.rollback()
        except Exception:
            pass
