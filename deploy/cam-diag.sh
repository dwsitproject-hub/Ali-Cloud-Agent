#!/bin/bash
# cam-diag — read-only diagnostic snapshot for Cloud Agent Monitoring.
#
# Installed on each MONITORED host at /usr/local/bin/cam-diag and run by the
# monitor over SSH as:   sudo -n /usr/local/bin/cam-diag <focus>
#
# It is deliberately the ONLY command the monitoring user may run via sudo, so
# the monitor cannot execute anything else. Everything here is read-only: it
# inspects processes, containers and database activity, and changes nothing.
#
# <focus> is one of: cpu | memory | disk | service | all   (anything else => all)
#
# Review this script before installing it — it is meant to be auditable.
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

FOCUS="${1:-all}"
case "$FOCUS" in cpu|memory|disk|service|all) ;; *) FOCUS=all ;; esac

MAXQ=${CAM_DIAG_MAX_QUERIES:-5}     # live queries reported per database
hr() { printf '\n== %s ==\n' "$1"; }

hr "HOST"
printf 'hostname : %s\n' "$(hostname)"
printf 'time     : %s\n' "$(date -Is)"
printf 'uptime   : %s\n' "$(uptime | sed 's/^ *//')"
printf 'cpus     : %s\n' "$(nproc 2>/dev/null || echo '?')"

if [ "$FOCUS" = cpu ] || [ "$FOCUS" = all ]; then
  hr "CPU SNAPSHOT"
  top -bn1 2>/dev/null | head -5 | tail -3

  hr "TOP PROCESSES BY CPU (ELAPSED = how long it has been running)"
  ps -eo pcpu,pmem,etime,user,comm,args --sort=-pcpu 2>/dev/null \
    | head -9 | cut -c1-200
fi

if [ "$FOCUS" = memory ] || [ "$FOCUS" = all ]; then
  hr "MEMORY"
  free -m 2>/dev/null

  hr "TOP PROCESSES BY MEMORY"
  ps -eo pmem,pcpu,rss,etime,user,comm --sort=-pmem 2>/dev/null | head -6

  hr "OOM KILLER (last 5)"
  dmesg -T 2>/dev/null | grep -iE 'out of memory|killed process' | tail -5 \
    || echo "(none, or dmesg not readable)"
fi

if [ "$FOCUS" = disk ] || [ "$FOCUS" = all ]; then
  hr "DISK"
  df -h -x tmpfs -x devtmpfs 2>/dev/null | head -8
  hr "LARGEST DIRECTORIES (/)"
  du -xh --max-depth=1 / 2>/dev/null | sort -h | tail -6
fi

# --- Containers: which service is responsible -------------------------------
if command -v docker >/dev/null 2>&1; then
  hr "CONTAINERS BY CPU/MEM"
  timeout 12 docker stats --no-stream \
    --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}' 2>/dev/null \
    | head -12 || echo "(docker stats unavailable)"

  hr "CONTAINER STATUS"
  timeout 8 docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null \
    | head -14 || echo "(docker ps unavailable)"

  # --- Databases: which query is responsible --------------------------------
  # For every running PostgreSQL container, report the live non-idle queries.
  # This is what actually names the offending activity.
  PGC=$(timeout 8 docker ps --filter ancestor=postgres --format '{{.Names}}' 2>/dev/null)
  PGC="$PGC $(timeout 8 docker ps --format '{{.Names}}' 2>/dev/null | grep -iE 'postgres|pg' || true)"
  for c in $(echo "$PGC" | tr ' ' '\n' | sort -u | sed '/^$/d'); do
    out=$(timeout 12 docker exec "$c" psql -U postgres -X -q -A -F ' | ' -t -c \
      "SELECT datname, usename, client_addr, state, wait_event_type,
              round(extract(epoch from now()-query_start)) AS dur_s,
              left(regexp_replace(query,'\s+',' ','g'),240)
         FROM pg_stat_activity
        WHERE state <> 'idle' AND pid <> pg_backend_pid()
        ORDER BY dur_s DESC NULLS LAST LIMIT ${MAXQ};" 2>/dev/null)
    if [ -n "${out// /}" ]; then
      hr "ACTIVE QUERIES — container: $c  (db | user | client | state | wait | secs | query)"
      printf '%s\n' "$out"
    fi
  done

  hr "RECENT DATABASE ERRORS / CRASHES"
  for c in $(echo "$PGC" | tr ' ' '\n' | sort -u | sed '/^$/d'); do
    errs=$(timeout 10 docker logs "$c" --since 30m 2>&1 \
      | grep -iE 'ERROR|FATAL|terminated by signal|recovery mode|out of memory' \
      | tail -4)
    [ -n "$errs" ] && { printf -- '-- %s --\n' "$c"; printf '%s\n' "$errs"; }
  done
fi

if [ "$FOCUS" = service ] || [ "$FOCUS" = all ]; then
  hr "LISTENING PORTS"
  (ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null) | head -14
fi

exit 0
