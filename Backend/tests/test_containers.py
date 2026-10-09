"""Docker workload parsing and status classification.

The point of this feature is catching states a metric scan and a port probe both
miss: a container docker calls unhealthy, or one stuck restarting, while CPU is
fine and the port still answers.
"""
import containers


def _line(name, image, state, status, ports=""):
    return "\t".join(["CTR", name, image, state, status, ports])


# --- classify ---------------------------------------------------------------
def test_healthy_and_running_are_distinct():
    assert containers.classify("running", "Up 4 hours (healthy)") == "healthy"
    assert containers.classify("running", "Up 4 hours") == "running"


def test_unhealthy_wins_over_running():
    """The case a port probe cannot see: up, listening, failing its healthcheck."""
    assert containers.classify("running", "Up 4 hours (unhealthy)") == "unhealthy"


def test_restarting_and_stopped():
    assert containers.classify("restarting", "Restarting (1) 42 seconds ago") == "restarting"
    assert containers.classify("exited", "Exited (137) 2 hours ago") == "stopped"
    assert containers.classify("dead", "Dead") == "stopped"


def test_health_starting_is_not_healthy():
    assert containers.classify("running", "Up 5 seconds (health: starting)") == "starting"


def test_unknown_state():
    assert containers.classify("", "") == "unknown"


# --- parse ------------------------------------------------------------------
def test_parse_reads_tab_separated_rows():
    out = "HOSTNAME\tECS-App\n" + "\n".join([
        _line("jps-fe", "jetty-planning-system-jps-fe", "running", "Up About an hour",
              "0.0.0.0:3080->80/tcp"),
        _line("tas_backend", "tas-production-backend", "restarting",
              "Restarting (1) 42 seconds ago"),
    ])
    rows, error = containers.parse(out)
    assert error is None
    assert {r["name"] for r in rows} == {"jps-fe", "tas_backend"}
    by_name = {r["name"]: r for r in rows}
    assert by_name["jps-fe"]["health"] == "running"
    assert by_name["jps-fe"]["ports"] == "0.0.0.0:3080->80/tcp"
    assert by_name["tas_backend"]["health"] == "restarting"


def test_parse_surfaces_remote_error():
    rows, error = containers.parse("HOSTNAME\tDB-Production\nERR\tdocker not installed on this host")
    assert rows == []
    assert error == "docker not installed on this host"


def test_parse_keeps_image_names_with_colons_intact():
    """Tab separation exists precisely because ":" and "/" appear in images."""
    rows, _ = containers.parse(_line("db", "postgres:16-alpine", "running", "Up 7 days (healthy)"))
    assert rows[0]["image"] == "postgres:16-alpine"
    assert rows[0]["health"] == "healthy"


def test_problem_containers_sort_first():
    out = "\n".join([
        _line("aaa-healthy", "img", "running", "Up 1 hour (healthy)"),
        _line("zzz-broken", "img", "running", "Up 1 hour (unhealthy)"),
    ])
    rows, _ = containers.parse(out)
    assert rows[0]["name"] == "zzz-broken"   # attention first, not alphabetical


def test_stale_host_script_is_reported_not_silently_empty():
    """An old cam-diag does not know the `containers` argument, falls back to its
    full report, and would leave the panel blank with no explanation - while making
    the host run the whole diagnostic sweep every scan cycle."""
    rows, error = containers.parse("== HOST ==\nhostname : ECS-App\n== CPU SNAPSHOT ==\n")
    assert rows == []
    assert error and "re-install" in error


def test_empty_output_is_not_a_stale_script():
    assert containers.parse("") == ([], None)
    assert containers.parse("   \n") == ([], None)


def test_host_with_docker_but_no_containers_is_not_an_error():
    rows, error = containers.parse("HOSTNAME\tDB-Production\n")
    assert rows == [] and error is None


def test_parse_tolerates_missing_ports_column():
    rows, _ = containers.parse("CTR\tn\timg\trunning\tUp 2 days")
    assert rows[0]["ports"] == ""


# --- per-container CPU / memory ---------------------------------------------
# "the host is at 95%" is not actionable; "klip-backend is holding 880 MiB" is.
def _stat(name, cpu, mem_pct, mem_usage):
    return "\t".join(["STAT", name, cpu, mem_pct, mem_usage])


def test_stats_are_merged_onto_the_matching_container():
    out = "\n".join([
        "HOSTNAME\tECS-DB",
        _line("klip-backend", "klip-backend", "running", "Up 3 hours (healthy)"),
        _stat("klip-backend", "0.03%", "15.13%", "116.2MiB / 768MiB"),
    ])
    rows, error = containers.parse(out)
    assert error is None
    assert rows[0]["cpu_pct"] == 0.03
    assert rows[0]["mem_pct"] == 15.13
    assert rows[0]["mem_usage"] == "116.2MiB / 768MiB"


def test_container_without_stats_keeps_nulls_not_zeros():
    """A stopped container gets no `docker stats` row. Zero would read as
    'idle'; None reads as 'unknown', which is the truth."""
    rows, _ = containers.parse(_line("old", "img", "exited", "Exited (0) 2 days ago"))
    assert rows[0]["cpu_pct"] is None and rows[0]["mem_pct"] is None


def test_docker_stats_dashes_are_not_numbers():
    rows, _ = containers.parse("\n".join([
        _line("starting", "img", "running", "Up 2 seconds"),
        _stat("starting", "--", "--", "-- / --"),
    ]))
    assert rows[0]["cpu_pct"] is None


def test_stats_failure_downgrades_rather_than_blanks():
    """`docker ps` can succeed while `docker stats` times out. The list is still
    worth showing; the reason the numbers are missing is still worth recording."""
    rows, error = containers.parse("\n".join([
        "HOSTNAME\tECS-DB",
        _line("app", "img", "running", "Up 1 hour"),
        "WARN\tdocker stats unavailable or timed out",
    ]))
    assert len(rows) == 1
    assert rows[0]["cpu_pct"] is None
    assert error and "docker stats" in error


def test_heaviest_consumer_sorts_above_lighter_healthy_ones():
    rows, _ = containers.parse("\n".join([
        _line("idle-svc", "img", "running", "Up 1 hour (healthy)"),
        _stat("idle-svc", "0.01%", "1.00%", "10MiB / 1GiB"),
        _line("hot-svc", "img", "running", "Up 1 hour (healthy)"),
        _stat("hot-svc", "92.40%", "3.00%", "30MiB / 1GiB"),
    ]))
    assert [r["name"] for r in rows] == ["hot-svc", "idle-svc"]


def test_problem_containers_still_outrank_heavy_healthy_ones():
    """A restart loop matters more than a busy-but-fine container."""
    rows, _ = containers.parse("\n".join([
        _line("hot-svc", "img", "running", "Up 1 hour (healthy)"),
        _stat("hot-svc", "99.00%", "80.00%", "800MiB / 1GiB"),
        _line("broken", "img", "restarting", "Restarting (1) 5 seconds ago"),
    ]))
    assert rows[0]["name"] == "broken"


def test_top_consumers_by_cpu_and_by_memory():
    rows, _ = containers.parse("\n".join([
        _line("a", "img", "running", "Up 1h"), _stat("a", "5.00%", "60.00%", "600MiB / 1GiB"),
        _line("b", "img", "running", "Up 1h"), _stat("b", "80.00%", "2.00%", "20MiB / 1GiB"),
        _line("c", "img", "running", "Up 1h"), _stat("c", "1.00%", "10.00%", "100MiB / 1GiB"),
    ]))
    assert [r["name"] for r in containers.top_consumers(rows, "cpu", 2)] == ["b", "a"]
    assert [r["name"] for r in containers.top_consumers(rows, "mem", 2)] == ["a", "c"]


def test_top_consumers_skips_containers_with_no_figures():
    rows, _ = containers.parse(_line("stopped", "img", "exited", "Exited (0) 1 day ago"))
    assert containers.top_consumers(rows, "cpu") == []
