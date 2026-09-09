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


def test_parse_ignores_noise_and_empty():
    rows, error = containers.parse("some banner text\n\n")
    assert rows == [] and error is None
    assert containers.parse("") == ([], None)


def test_parse_tolerates_missing_ports_column():
    rows, _ = containers.parse("CTR\tn\timg\trunning\tUp 2 days")
    assert rows[0]["ports"] == ""
