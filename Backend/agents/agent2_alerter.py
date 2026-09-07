"""Agent 2 - DirectMail alerter.

Composes an alert email from a scan result (metrics + service health) and sends
it through the real Alibaba DirectMail ``SingleSendMail`` API. In MOCK_MODE it
renders the email but does not transmit, returning a preview instead.

API reference:
  https://www.alibabacloud.com/help/en/directmail/latest/singlesendmail
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timezone

import config
from cloud_client import do_rpc

log = logging.getLogger("alerter")

DM_VERSION = "2015-11-23"


def _dm_domain() -> str:
    return f"dm.{config.DIRECTMAIL_REGION}.aliyuncs.com"


def _metrics_rows(results):
    groups = {}
    for r in results:
        g = groups.setdefault(r.get("group", "Ungrouped"), {})
        g.setdefault(r.get("instance_name") or r.get("instance_id", "-"), []).append(r)
    rows = []
    for gname, instances in groups.items():
        rows.append(
            f"<tr><td colspan='5' style='padding:10px 10px 4px;font-size:12px;"
            f"text-transform:uppercase;letter-spacing:.5px;color:#888;font-weight:700'>"
            f"{html.escape(gname)}</td></tr>"
        )
        for iname, metrics in instances.items():
            for idx, r in enumerate(metrics):
                val = "-" if r["value"] is None else f"{r['value']}{r['unit']}"
                thr = f"{r['comparison']} {r['threshold']}{r['unit']}"
                color = "#c0392b" if r["breached"] else "#2c3e50"
                flag = "BREACH" if r["breached"] else "ok"
                note = html.escape(r["error"]) if r.get("error") else ""
                inst_cell = html.escape(iname) if idx == 0 else ""
                rows.append(
                    f"<tr>"
                    f"<td style='padding:6px 10px;border-bottom:1px solid #eee;font-weight:600'>{inst_cell}</td>"
                    f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{html.escape(r['label'])}</td>"
                    f"<td style='padding:6px 10px;border-bottom:1px solid #eee;color:{color};font-weight:600'>{val}</td>"
                    f"<td style='padding:6px 10px;border-bottom:1px solid #eee;color:#777'>{thr}</td>"
                    f"<td style='padding:6px 10px;border-bottom:1px solid #eee;color:{color}'>{flag}"
                    + (f"<div style='color:#c0392b;font-size:11px'>{note}</div>" if note else "")
                    + f"</td></tr>"
                )
    return "".join(rows)


def _stopped_section(stopped):
    """Metrics that were reporting and now are not.

    Worth its own section because it is NOT the same as a breach and not the same
    as healthy: a metric with no value is never compared against its threshold,
    so an instance whose agent dies looks fine on every dashboard while it burns.
    """
    if not stopped:
        return ""
    rows = []
    for m in stopped:
        why = html.escape(m.get("error") or "no datapoints returned")
        rows.append(
            f"<tr>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;font-weight:600'>{html.escape(m.get('instance_name') or '')}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{html.escape(m.get('label') or '')}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;color:#b8860b'>STOPPED REPORTING"
            f"<div style='color:#b8860b;font-size:11px'>{why}</div></td>"
            f"</tr>"
        )
    return (
        "<h3 style='font-size:14px;color:#b8860b;margin:18px 0 6px'>Metrics that stopped reporting</h3>"
        "<p style='color:#555;font-size:13px;margin:0 0 6px'>These were reporting on the previous "
        "scan and returned nothing on this one, so their thresholds could not be "
        "evaluated. Usual causes: the CloudMonitor agent stopped (check "
        "<code>systemctl status cloudmonitor</code> / <code>aliyun-service</code>), "
        "or the host is under enough memory pressure that the agent cannot run — "
        "which is itself the incident. Treat a value of “none” as unknown, never as OK.</p>"
        "<table style='border-collapse:collapse;width:100%;font-size:14px'>"
        "<thead><tr style='text-align:left;color:#888;font-size:12px;text-transform:uppercase'>"
        "<th style='padding:6px 10px'>Instance</th><th style='padding:6px 10px'>Metric</th>"
        "<th style='padding:6px 10px'>Status</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _services_section(services_down):
    if not services_down:
        return ("<p style='color:#27ae60;font-size:13px;margin:14px 0 4px'>"
                "&#10003; All monitored services are up.</p>")
    rows = []
    for s in services_down:
        err = html.escape(s.get("error") or s.get("detail") or "down")
        rows.append(
            f"<tr>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;font-weight:600'>{html.escape(s['instance_name'])}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{html.escape(s['name'])}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;color:#777'>{html.escape(str(s.get('target','')))}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;color:#c0392b'>DOWN"
            f"<div style='color:#c0392b;font-size:11px'>{err}</div></td>"
            f"</tr>"
        )
    return (
        "<h3 style='font-size:14px;color:#c0392b;margin:18px 0 6px'>Services down</h3>"
        "<table style='border-collapse:collapse;width:100%;font-size:14px'>"
        "<thead><tr style='text-align:left;color:#888;font-size:12px;text-transform:uppercase'>"
        "<th style='padding:6px 10px'>Instance</th><th style='padding:6px 10px'>Service</th>"
        "<th style='padding:6px 10px'>Target</th><th style='padding:6px 10px'>Status</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


# --- Diagnosis guidance -----------------------------------------------------
# Per-breach-type likely causes + the commands that actually diagnose them.
# Distilled from real incidents on this estate (see Docs/findings-*.md), so the
# person paged at 2am has the first five minutes of investigation in hand.
_GUIDANCE = {
    "CPU": {
        "causes": [
            "an unoptimised or runaway database query (by far the most common cause here)",
            "a recent deploy that changed a query or added a background job",
            "a retry loop &mdash; an app re-issuing a statement that always fails",
            "several heavy reports running concurrently on a small instance",
        ],
        "cmds": [
            ("Spike or sustained?", "uptime; sar -u | tail -20"),
            ("Top consumers", "top -bn1 -o %CPU | head -15"),
            ("If it is a database host",
             "ps -C postgres -o pcpu,etime,args --sort=-pcpu | head -10"),
            ("Live queries (replace &lt;pg&gt;/&lt;db&gt;)",
             "docker exec &lt;pg&gt; psql -U postgres -d &lt;db&gt; -c \"SELECT pid,state,"
             "wait_event_type,round(extract(epoch from now()-query_start)) dur_s,"
             "left(query,200) q FROM pg_stat_activity WHERE state&lt;&gt;'idle' "
             "ORDER BY dur_s DESC;\""),
        ],
    },
    "Memory": {
        "causes": [
            "a query or process with a very large working set",
            "per-operation DB memory set too high (e.g. PostgreSQL <code>work_mem</code> "
            "is allocated <em>per sort/hash node</em>, not per connection)",
            "no swap configured, so there is no cushion before the OOM killer fires",
            "too many services sharing one undersized host",
        ],
        "cmds": [
            ("Memory and swap headroom", "free -m"),
            ("Did the OOM killer fire?",
             "dmesg -T | grep -i 'out of memory' | tail -5"),
            ("Per-container usage",
             "docker stats --no-stream --format 'table {{.Name}}\\t{{.MemUsage}}\\t{{.MemPerc}}'"),
            ("Did a database crash and recover?",
             "docker logs &lt;pg&gt; --since 1h 2>&amp;1 | grep -iE "
             "'terminated by signal|recovery mode|not properly shut down'"),
        ],
    },
    "Disk": {
        "causes": [
            "log files or container logs growing without rotation",
            "unused Docker images, volumes and build cache",
            "database growth or unpruned history tables",
        ],
        "cmds": [
            ("What is full", "df -h"),
            ("Biggest directories",
             "du -xh --max-depth=1 / 2>/dev/null | sort -h | tail -10"),
            ("Docker reclaimable space", "docker system df"),
        ],
    },
    "_services": {
        "causes": [
            "the container or service stopped, crashed, or is restarting",
            "the service is listening on a different port than expected",
            "a security-group / firewall rule blocks the monitor from reaching it",
            "the host is overloaded and not accepting new connections",
        ],
        "cmds": [
            ("Is it running?",
             "docker ps --format 'table {{.Names}}\\t{{.Status}}\\t{{.Ports}}'"),
            ("Is the port listening?", "ss -tlnp | grep ':&lt;port&gt;'"),
            ("Why did it stop?", "docker logs &lt;container&gt; --tail 50"),
            ("Reachable from the monitor host?",
             "timeout 5 bash -c '</dev/null >/dev/tcp/&lt;host&gt;/&lt;port&gt;' &amp;&amp; echo OK || echo BLOCKED"),
        ],
    },
}


def _guidance_block(title: str, spec: dict) -> str:
    causes = "".join(f"<li style='margin:2px 0'>{c}</li>" for c in spec["causes"])
    cmds = "".join(
        f"<div style='margin:6px 0 0'>"
        f"<div style='font-size:12px;color:#555'>{label}</div>"
        f"<code style='display:block;background:#f6f8fa;border:1px solid #e1e4e8;"
        f"border-radius:4px;padding:6px 8px;font-size:11px;white-space:pre-wrap;"
        f"word-break:break-all;color:#24292e'>{cmd}</code></div>"
        for label, cmd in spec["cmds"]
    )
    return (
        f"<div style='margin:14px 0 0;padding:12px 14px;background:#fff;"
        f"border:1px solid #e6e6e6;border-left:3px solid #c0392b;border-radius:4px'>"
        f"<div style='font-weight:700;font-size:13px;color:#2c3e50'>{title}</div>"
        f"<div style='font-size:12px;color:#555;margin:6px 0 2px'>Likely causes</div>"
        f"<ul style='margin:0 0 4px 18px;padding:0;font-size:12px;color:#444'>{causes}</ul>"
        f"<div style='font-size:12px;color:#555;margin:8px 0 2px'>Diagnose (run on the affected host)</div>"
        f"{cmds}</div>"
    )


def _evidence_section(scan: dict) -> str:
    """Live on-host evidence: which service/process/query caused the breach.

    Rendered from ``scan['diagnostics']`` (collected over SSH by
    Backend/diagnostics.py). This is the real culprit, not a guess — so when it
    is present it appears first and the generic guidance is reduced to a footnote.
    """
    diags = scan.get("diagnostics") or {}
    if not diags:
        return ""

    blocks = []
    for name, d in diags.items():
        blocks.append(
            "<div style='margin:12px 0 0;padding:12px 14px;background:#fff;"
            "border:1px solid #e6e6e6;border-left:3px solid #2c7be5;border-radius:4px'>"
            f"<div style='font-weight:700;font-size:13px;color:#2c3e50'>"
            f"{html.escape(str(name))} "
            f"<span style='font-weight:400;color:#888'>&mdash; {html.escape(d.get('host',''))}"
            f" ({html.escape(d.get('focus','all'))})</span></div>"
            "<pre style='margin:8px 0 0;background:#0f1419;color:#d7e0ea;"
            "border-radius:4px;padding:10px;font-size:10.5px;line-height:1.45;"
            "white-space:pre-wrap;word-break:break-word;overflow-x:auto'>"
            f"{html.escape(d.get('output',''))}</pre></div>"
        )

    return (
        "<h3 style='font-size:14px;color:#2c7be5;margin:20px 0 6px'>"
        "What caused this &mdash; live evidence from the affected host</h3>"
        "<div style='font-size:12px;color:#555;margin:0 0 2px'>Captured at alert time "
        "over SSH. Look for the top process / container by CPU or memory, and for "
        "database hosts the longest-running query &mdash; that is the activity "
        "responsible. <b>ELAPSED / secs</b> shows how long it had been running.</div>"
        + "".join(blocks)
    )


def _diagnosis_section(scan: dict) -> str:
    """Root-cause guidance for exactly what breached in this scan."""
    breaches = scan.get("breaches", [])
    services_down = scan.get("services_down", [])
    if not breaches and not services_down:
        return ""

    # Which instances need attention, and which guidance blocks apply.
    affected, kinds = {}, []
    for b in breaches:
        name = b.get("instance_name") or b.get("instance_id") or "-"
        affected.setdefault(name, set()).add(b.get("label") or "metric")
        label = b.get("label")
        if label in _GUIDANCE and label not in kinds:
            kinds.append(label)
    for s in services_down:
        name = s.get("instance_name") or s.get("instance_id") or "-"
        affected.setdefault(name, set()).add(f"{s.get('name','service')} down")
    if services_down:
        kinds.append("_services")

    who = "".join(
        f"<li style='margin:2px 0'><b>{html.escape(str(n))}</b> &mdash; "
        f"{html.escape(', '.join(sorted(v)))}</li>"
        for n, v in affected.items()
    )

    blocks = "".join(
        _guidance_block("Service unreachable" if k == "_services"
                        else f"{k} threshold breached", _GUIDANCE[k])
        for k in kinds
    )

    # When live evidence was collected, the guidance is a fallback footnote only.
    has_evidence = bool(scan.get("diagnostics"))
    heading = ("Further checks" if has_evidence else "How to investigate this")
    if has_evidence:
        blocks = ""      # evidence already names the culprit; don't repeat guesses

    return (
        f"<h3 style='font-size:14px;color:#c0392b;margin:20px 0 6px'>{heading}</h3>"
        "<div style='font-size:12px;color:#555;margin:0 0 4px'>Needs attention</div>"
        f"<ul style='margin:0 0 4px 18px;padding:0;font-size:12px;color:#444'>{who}</ul>"
        f"{blocks}"
        "<div style='margin:14px 0 0;padding:10px 12px;background:#fff8e1;"
        "border:1px solid #ffe0a3;border-radius:4px;font-size:12px;color:#6b5200'>"
        "<b>Capture evidence before restarting or rebooting.</b> A reboot clears the "
        "symptom but destroys the running-query and process state that identifies the "
        "cause &mdash; several past incidents could not be diagnosed for this reason. "
        "Save the output of the commands above first, then prefer cancelling the single "
        "offending query or restarting just the one container over rebooting the host."
        "</div>"
    )


def build_email(scan: dict) -> dict:
    """Return {subject, html_body} for a given scan result document."""
    n_breach = len(scan.get("breaches", []))
    services_down = scan.get("services_down", [])
    n_down = len(services_down)
    stopped = scan.get("stopped_reporting", [])
    n_stopped = len(stopped)
    when = scan.get("scanned_at", datetime.now(timezone.utc).isoformat())

    if n_breach or n_down or n_stopped:
        parts = []
        if n_breach:
            parts.append(f"{n_breach} threshold breach{'es' if n_breach != 1 else ''}")
        if n_down:
            parts.append(f"{n_down} service{'s' if n_down != 1 else ''} down")
        if n_stopped:
            parts.append(f"{n_stopped} metric{'s' if n_stopped != 1 else ''} stopped reporting")
        subject = "[CloudMonitor] " + ", ".join(parts)
        status_color = "#c0392b"
        status_text = " & ".join(parts) + " detected"
    else:
        subject = "[CloudMonitor] All systems healthy"
        status_color = "#27ae60"
        status_text = "All clear - metrics within thresholds, services up"

    metric_rows = _metrics_rows(scan.get("results", []))
    services_html = _services_section(services_down) + _stopped_section(stopped)
    diagnosis_html = _evidence_section(scan) + _diagnosis_section(scan)

    body = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:680px;margin:auto">
  <div style="background:{status_color};color:#fff;padding:16px 20px;border-radius:8px 8px 0 0">
    <h2 style="margin:0;font-size:18px">Cloud Agent Monitoring - Alert</h2>
    <div style="opacity:.9;font-size:13px">{status_text}</div>
  </div>
  <div style="border:1px solid #eee;border-top:none;border-radius:0 0 8px 8px;padding:16px 20px">
    <p style="color:#555;font-size:13px;margin-top:0">Scan time (UTC): {html.escape(when)} &nbsp;|&nbsp; Mode: {scan.get('mode','?')}</p>
    {services_html}
    <h3 style="font-size:14px;color:#2c3e50;margin:18px 0 6px">Resource metrics</h3>
    <table style="border-collapse:collapse;width:100%;font-size:14px">
      <thead>
        <tr style="text-align:left;color:#888;font-size:12px;text-transform:uppercase">
          <th style="padding:6px 10px">Instance</th><th style="padding:6px 10px">Metric</th>
          <th style="padding:6px 10px">Value</th><th style="padding:6px 10px">Rule</th>
          <th style="padding:6px 10px">Status</th>
        </tr>
      </thead>
      <tbody>{metric_rows}</tbody>
    </table>
    {diagnosis_html}
    <p style="color:#aaa;font-size:11px;margin-bottom:0">Sent by Cloud Agent Monitoring (Agent 2).</p>
  </div>
</div>"""
    return {"subject": subject, "html_body": body}


def _send_one(to_address: str, subject: str, html_body: str) -> dict:
    """Send one email via Alibaba DirectMail (SingleSendMail)."""
    params = {
        "AccountName": config.DM_ACCOUNT_NAME,
        "AddressType": "1",          # 1 = use the configured sender address
        "ReplyToAddress": "false",
        "ToAddress": to_address,
        "FromAlias": config.DM_FROM_ALIAS,
        "Subject": subject,
        "HtmlBody": html_body,
    }
    resp = do_rpc(_dm_domain(), DM_VERSION, "SingleSendMail", params,
                  region=config.DIRECTMAIL_REGION)
    return {"to": to_address, "ok": True, "request_id": resp.get("RequestId"), "error": None}


def _send_via_smtp(recipients: list, subject: str, html_body: str) -> None:
    """Send the HTML alert to all recipients via SMTP (SSL or STARTTLS).

    Raises on failure (the caller records the error). SMTP_SECURE=true uses
    implicit SSL (e.g. port 465); false uses STARTTLS (e.g. 587)."""
    import smtplib
    import ssl
    from email.mime.text import MIMEText
    from email.utils import formataddr

    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((config.DM_FROM_ALIAS, config.SMTP_FROM))
    msg["To"] = ", ".join(recipients)

    ctx = ssl.create_default_context()
    if not config.SMTP_REJECT_UNAUTHORIZED:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    if config.smtp_use_ssl():   # implicit SSL (e.g. :465)
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT,
                              context=ctx, timeout=20) as srv:
            srv.login(config.SMTP_USER, config.SMTP_PASSWORD)
            srv.sendmail(config.SMTP_FROM, recipients, msg.as_string())
    else:                       # STARTTLS (e.g. :587)
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as srv:
            srv.ehlo()
            if srv.has_extn("starttls"):
                srv.starttls(context=ctx)
                srv.ehlo()
            elif config.SMTP_ALLOW_INSECURE:
                log.warning("SMTP %s:%s does not advertise STARTTLS — sending "
                            "unencrypted because SMTP_ALLOW_INSECURE=true",
                            config.SMTP_HOST, config.SMTP_PORT)
            else:
                # Never transmit credentials in cleartext by default.
                raise RuntimeError(
                    f"{config.SMTP_HOST}:{config.SMTP_PORT} does not advertise "
                    "STARTTLS, so credentials cannot be sent securely. Use the "
                    "implicit-TLS port instead (SMTP_PORT=465, SMTP_SECURE=true), "
                    "or set SMTP_ALLOW_INSECURE=true to accept an unencrypted send.")
            srv.login(config.SMTP_USER, config.SMTP_PASSWORD)
            srv.sendmail(config.SMTP_FROM, recipients, msg.as_string())


def send_alert(scan: dict, recipients=None) -> dict:
    """Send the alert email for ``scan`` to each recipient.

    Transport: SMTP when configured, else Alibaba DirectMail. In mock mode the
    email is rendered but not transmitted. Returns a summary dict.
    """
    recipients = recipients or config.ALERT_RECIPIENTS
    email = build_email(scan)
    sent_at = datetime.now(timezone.utc).isoformat()

    if not recipients:
        return {"sent": False, "reason": "no recipients configured",
                "subject": email["subject"], "results": [], "sent_at": sent_at,
                "preview_html": email["html_body"]}

    if config.MOCK_MODE:
        return {
            "sent": False, "mode": "mock",
            "reason": "MOCK_MODE - email rendered but not transmitted",
            "subject": email["subject"], "recipients": recipients,
            "results": [{"to": r, "ok": True, "mock": True} for r in recipients],
            "preview_html": email["html_body"], "sent_at": sent_at,
        }

    # --- live: pick a transport ---
    if config.smtp_configured():
        try:
            _send_via_smtp(recipients, email["subject"], email["html_body"])
            return {"sent": True, "mode": "live", "transport": "smtp",
                    "subject": email["subject"], "recipients": recipients,
                    "results": [{"to": r, "ok": True, "error": None} for r in recipients],
                    "sent_at": sent_at}
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            log.error("SMTP send failed: %s", err)
            return {"sent": False, "mode": "live", "transport": "smtp", "reason": err,
                    "subject": email["subject"], "recipients": recipients,
                    "results": [{"to": r, "ok": False, "error": err} for r in recipients],
                    "sent_at": sent_at}

    if config.credentials_present() and config.DM_ACCOUNT_NAME:
        results = []
        for r in recipients:
            try:
                results.append(_send_one(r, email["subject"], email["html_body"]))
            except Exception as exc:
                results.append({"to": r, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return {"sent": any(x["ok"] for x in results), "mode": "live",
                "transport": "directmail", "subject": email["subject"],
                "recipients": recipients, "results": results, "sent_at": sent_at}

    return {"sent": False, "mode": "live",
            "reason": "no mail transport configured (set SMTP_* or DM_ACCOUNT_NAME)",
            "subject": email["subject"], "recipients": recipients,
            "results": [], "sent_at": sent_at}
