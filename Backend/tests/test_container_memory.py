"""Container memory must be comparable between containers, and to the host.

`docker stats` MemPerc is measured against the container's own ``mem_limit``
when one is set, and against host RAM when one is not. Mixing both in a single
column inverted the ranking on Backend Production during the 2026-11-25 memory
breach: `aastar-price-api` showed 42.48% (435 MiB of a 1 GiB cap) while
`plantation-rainfall-backend-production` showed 6.06% for a LARGER 449 MiB. The
real leader, `pct-api` at 2.3 GiB, was buried - and `top_consumers()` ranked on
the same field, so the alert email would have named the wrong service.

These are the real figures from that host.
"""
import containers


def _out(stats, hostname="ECS-DB"):
    """Build cam-diag `containers` output from ``[(name, cpu, memperc, usage)]``."""
    lines = ["HOSTNAME\t" + hostname]
    for name, _cpu, _pct, _usage in stats:
        lines.append("CTR\t%s\timg\trunning\tUp 5 minutes (healthy)\t" % name)
    for name, cpu, pct, usage in stats:
        lines.append("STAT\t%s\t%s\t%s\t%s" % (name, cpu, pct, usage))
    return "\n".join(lines)


# The shape that caused the misreading: two capped, two uncapped, 16 GB host.
REAL = [
    ("pct-api", "0.2%", "15.2%", "2.275GiB / 14.96GiB"),
    ("plantation-rainfall-backend-production", "0.1%", "6.06%", "449MiB / 14.96GiB"),
    ("aastar-price-api", "0.15%", "42.48%", "435MiB / 1GiB"),
    ("kmaps-api-api-1", "0.02%", "10.96%", "56.14MiB / 512MiB"),
    ("slms-redis-prod", "0.18%", "6.87%", "17.59MiB / 256MiB"),
]


def _by_name(rows):
    return {r["name"]: r for r in rows}


# --- host RAM, read back out of docker's own output -------------------------
def test_host_ram_derived_from_uncapped_containers():
    """Two containers sharing a ceiling means that ceiling IS the machine."""
    rows, err = containers.parse(_out(REAL))
    assert err is None
    assert containers.host_mem_bytes(rows) == int(14.96 * 1024 ** 3)


def test_host_ram_unknown_when_every_container_is_capped():
    """No uncapped container means no host total - and we must not invent one."""
    capped = [("a", "0%", "50%", "512MiB / 1GiB"),
              ("b", "0%", "25%", "128MiB / 512MiB")]
    rows, _ = containers.parse(_out(capped))
    assert containers.host_mem_bytes(rows) is None
    assert all(r["mem_pct_host"] is None for r in rows)
    # Absolute usage still parses, so a ranking is still possible.
    assert _by_name(rows)["a"]["mem_bytes"] == 512 * 1024 ** 2


def test_two_services_sharing_a_mem_limit_is_not_the_host():
    """A coincidence of equal caps must not be mistaken for host RAM.

    Guarded by taking the LARGEST shared ceiling: two 1 GiB-capped services plus
    uncapped containers at 14.96 GiB must still resolve to 14.96 GiB.
    """
    rows, _ = containers.parse(_out([
        ("cap1", "0%", "40%", "400MiB / 1GiB"),
        ("cap2", "0%", "40%", "400MiB / 1GiB"),
        ("free1", "0%", "3%", "460MiB / 14.96GiB"),
        ("free2", "0%", "1%", "160MiB / 14.96GiB"),
    ]))
    assert containers.host_mem_bytes(rows) == int(14.96 * 1024 ** 3)


# --- the inverted ranking ---------------------------------------------------
def test_host_share_corrects_the_inverted_ranking():
    rows = _by_name(containers.parse(_out(REAL))[0])
    capped = rows["aastar-price-api"]
    uncapped = rows["plantation-rainfall-backend-production"]

    # What docker said, and why it misled.
    assert capped["mem_pct"] > uncapped["mem_pct"]
    # What is actually true: the uncapped one uses MORE memory.
    assert uncapped["mem_bytes"] > capped["mem_bytes"]
    # And the host share now says so.
    assert uncapped["mem_pct_host"] > capped["mem_pct_host"]
    assert capped["mem_pct_host"] < 3.0           # 435 MiB of 16 GB, not 42%


def test_top_consumers_ranks_by_real_usage():
    rows, _ = containers.parse(_out(REAL))
    names = [r["name"] for r in containers.top_consumers(rows, "mem", 3)]
    assert names[0] == "pct-api", "the 2.3 GiB container must lead"
    assert "aastar-price-api" not in names[:2], "42.48% of a 1 GiB cap is not heavy"


def test_a_capped_container_at_its_limit_is_still_negligible():
    """90% of a 256 MiB cap explains nothing about a 16 GB host."""
    rows, _ = containers.parse(_out([
        ("tiny", "0%", "90%", "230MiB / 256MiB"),
        ("free1", "0%", "1%", "160MiB / 14.96GiB"),
        ("free2", "0%", "1%", "160MiB / 14.96GiB"),
    ]))
    assert _by_name(rows)["tiny"]["mem_pct"] == 90.0
    assert _by_name(rows)["tiny"]["mem_pct_host"] < 2.0
    assert containers.stands_out(rows, "mem") is False


def test_a_genuinely_heavy_container_still_stands_out():
    rows, _ = containers.parse(_out(REAL))
    assert containers.stands_out(rows, "mem") is True      # pct-api at 15.2%


# --- byte parsing -----------------------------------------------------------
def test_usage_pair_units():
    assert containers._usage_pair("435MiB / 1GiB") == (
        435 * 1024 ** 2, 1024 ** 3)
    assert containers._usage_pair("2.275GiB / 14.96GiB")[0] == int(2.275 * 1024 ** 3)
    assert containers._usage_pair("17.59MiB / 256MiB")[0] == int(17.59 * 1024 ** 2)


def test_unparseable_usage_does_not_crash_or_become_zero():
    """A container just starting prints "--"; that is unknown, not 0 bytes."""
    rows, _ = containers.parse(_out([("starting", "--", "--", "-- / --")]))
    row = _by_name(rows)["starting"]
    assert row["mem_bytes"] is None
    assert row["mem_pct_host"] is None
    assert row["cpu_pct"] is None


def test_old_host_script_without_usage_still_ranks():
    """Fallback to MemPerc rather than returning an empty list."""
    rows, _ = containers.parse(_out([
        ("a", "0%", "40%", ""),
        ("b", "0%", "70%", ""),
    ]))
    assert all(r["mem_bytes"] is None for r in rows)
    assert [r["name"] for r in containers.top_consumers(rows, "mem", 2)] == ["b", "a"]
