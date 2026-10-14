"""Root cause and remediation steps, in one place.

The alert email and the dashboard both have to answer "what is this and what do I
do about it". Written twice they drift, and the version you are reading at 2am is
the one that is out of date. So the text lives here, is rendered into the email by
agent2_alerter, and is served to the frontend through /api/status.

Each entry has:
  cause    - why this usually happens, in a sentence an operator can act on
  variants - a more specific cause when the live evidence can distinguish one
             (e.g. a refused connection means something very different from a
             timeout), keyed by a token the caller detects
  steps    - ordered (what, command) pairs; the command is literal and runnable

Nothing here is derived from the live scan. The specifics - which container is
heaviest, which port timed out - are composed on top by whoever renders it.
"""

GUIDANCE = {
    "CPU": {
        "cause": (
            "Sustained CPU on these hosts is almost always one container, and "
            "inside it one statement: an unindexed or runaway query, a retry loop "
            "re-issuing something that always fails, or several heavy jobs landing "
            "together. A recent deploy is the usual trigger."
        ),
        "steps": [
            ("Confirm which service", "docker stats --no-stream | sort -k3 -hr | head"),
            ("Find the process inside it",
             "sudo -n /usr/local/bin/cam-diag cpu"),
            ("If it is a database, find the statement",
             "SELECT now()-query_start AS dur, state, left(query,120) "
             "FROM pg_stat_activity WHERE state <> 'idle' ORDER BY 1 DESC LIMIT 10;"),
            ("Spike or sustained? (a spike needs no action)",
             "uptime; sar -u 1 5"),
        ],
    },
    "Memory": {
        "cause": (
            "A container growing without a limit, or several sharing a small host. "
            "These boxes mostly run containers with no mem_limit, so one leak can "
            "take the whole instance. Past 95% the OOM killer starts reaping "
            "processes - often sshd, which is why the host then looks 'down'."
        ),
        "steps": [
            ("Confirm which service",
             "docker stats --no-stream --format '{{.Name}} {{.MemUsage}} {{.MemPerc}}' | sort -k3 -hr | head"),
            ("Check whether the kernel has already killed something",
             "dmesg -T | grep -iE 'killed process|out of memory' | tail"),
            ("Check the safety net exists", "free -h; swapon --show"),
            ("Cap the offender so it cannot take the host",
             "set mem_limit on that service in its compose file, then re-create it"),
        ],
    },
    "Disk": {
        "cause": (
            "Usually logs or docker, not application data: unrotated container "
            "logs, old images and build cache, or a database that has grown past "
            "what the volume was sized for."
        ),
        "steps": [
            ("Find the growth", "du -xh / 2>/dev/null | sort -rh | head -20"),
            ("Docker is the usual answer", "docker system df"),
            ("Reclaim safely (does not touch running containers or named volumes)",
             "docker system prune -f"),
            ("Check container log sizes",
             "du -ch /var/lib/docker/containers/*/*-json.log | sort -h | tail"),
        ],
    },
    "service": {
        "cause": (
            "The monitor could not complete a connection to this port. What that "
            "means depends on HOW it failed, and the two causes are opposites."
        ),
        "variants": {
            "refused": (
                "Connection REFUSED: the packet reached the host and was actively "
                "rejected, so the box is up and the network path works - nothing is "
                "listening on that port. Almost always a wrong port or a stopped "
                "service, NOT a firewall."
            ),
            "timeout": (
                "TIMED OUT: no response at all, so the packets went nowhere. The "
                "instance is stopped or unreachable, or a security group is dropping "
                "them. A firewall drops silently; a closed port refuses."
            ),
            "status": (
                "The server answered with an unexpected HTTP status. It is up - it "
                "just did not serve what was asked. On a shared nginx this is usually "
                "the request hitting the DEFAULT vhost because the Host header does "
                "not match any server_name."
            ),
        },
        "steps": [
            ("Is anything listening there?", "sudo ss -tlnp | grep ':<port> '"),
            ("Is the instance itself running?",
             "check the ECS console - a stopped instance times out on every port"),
            ("For HTTP, test the vhost rather than the IP",
             "curl -s -o /dev/null -w '%{http_code}\\n' -H 'Host: <hostname>' http://<ip>/"),
        ],
    },
    "container": {
        "cause": (
            "Docker itself says this container is wrong: either its healthcheck "
            "keeps failing while the process stays up, or it is exiting and being "
            "restarted. A port probe cannot see either - the port may still answer."
        ),
        "steps": [
            ("What it is saying", "docker logs --tail 100 <name>"),
            ("Why docker calls it unhealthy",
             "docker inspect --format '{{json .State.Health}}' <name>"),
            ("Why it exited (137 = OOM-killed, 143 = terminated)",
             "docker inspect --format '{{.State.ExitCode}} {{.State.Error}}' <name>"),
        ],
    },
    "stopped": {
        "cause": (
            "The provider reports this instance is not running. Every metric and "
            "probe for it will stay dark until it starts - they are symptoms, not "
            "separate faults. Note that CPU comes from the hypervisor: if CPU is "
            "also blank, the instance is genuinely stopped rather than merely wedged."
        ),
        "steps": [
            ("Confirm from the provider, not from a probe",
             "ECS console, or DescribeInstances for this instance id"),
            ("Find out whether it was deliberate before starting it",
             "check with whoever owns the box - a shared host may be mid-maintenance"),
            ("After it starts, containers with restart:unless-stopped return on their own",
             "docker ps -a"),
        ],
    },
    "nodata": {
        "cause": (
            "This metric was reporting and has stopped. Memory and disk come from "
            "the in-guest CloudMonitor agent, so the usual cause is the agent dying "
            "or the instance stopping. CPU is hypervisor-level and needs no agent - "
            "if CPU stopped too, the instance is not running."
        ),
        "steps": [
            ("Is the instance running?", "ECS console, or check whether CPU still reports"),
            ("Is the agent alive?",
             "systemctl status cloudmonitor || systemctl status aliyun-service"),
            ("Restart it", "systemctl restart cloudmonitor"),
        ],
    },
}


def for_api() -> dict:
    """The guidance map as plain JSON for the dashboard to render."""
    return {
        key: {
            "cause": spec["cause"],
            "variants": spec.get("variants", {}),
            "steps": [{"what": what, "cmd": cmd} for what, cmd in spec["steps"]],
        }
        for key, spec in GUIDANCE.items()
    }
