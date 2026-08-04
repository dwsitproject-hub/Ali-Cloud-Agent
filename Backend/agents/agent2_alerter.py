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


def build_email(scan: dict) -> dict:
    """Return {subject, html_body} for a given scan result document."""
    n_breach = len(scan.get("breaches", []))
    services_down = scan.get("services_down", [])
    n_down = len(services_down)
    when = scan.get("scanned_at", datetime.now(timezone.utc).isoformat())

    if n_breach or n_down:
        parts = []
        if n_breach:
            parts.append(f"{n_breach} threshold breach{'es' if n_breach != 1 else ''}")
        if n_down:
            parts.append(f"{n_down} service{'s' if n_down != 1 else ''} down")
        subject = "[CloudMonitor] " + ", ".join(parts)
        status_color = "#c0392b"
        status_text = " & ".join(parts) + " detected"
    else:
        subject = "[CloudMonitor] All systems healthy"
        status_color = "#27ae60"
        status_text = "All clear - metrics within thresholds, services up"

    metric_rows = _metrics_rows(scan.get("results", []))
    services_html = _services_section(services_down)

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
    <p style="color:#aaa;font-size:11px;margin-bottom:0">Sent by Agent 2 via Alibaba DirectMail.</p>
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
    else:                       # STARTTLS (e.g. :587), with a plain fallback
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as srv:
            srv.ehlo()
            if srv.has_extn("starttls"):
                srv.starttls(context=ctx)
                srv.ehlo()
            else:
                # Server doesn't offer STARTTLS. Don't hard-fail (that silently
                # dropped every alert); log it and continue unencrypted.
                log.warning("SMTP %s:%s does not advertise STARTTLS — sending "
                            "without encryption", config.SMTP_HOST, config.SMTP_PORT)
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
