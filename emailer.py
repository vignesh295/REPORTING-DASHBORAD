"""
Email delivery over HTTP (works where outbound SMTP is blocked, e.g. Render).
Transport is chosen in this order:
  1. Gmail API (GMAIL_CLIENT_ID + GMAIL_CLIENT_SECRET + GMAIL_REFRESH_TOKEN) —
     sends as your own Google/Workspace address over HTTPS with top deliverability.
  2. Brevo   (BREVO_API_KEY + EMAIL_FROM) — single-sender verification, so it can
     send to ANY recipient without owning/verifying a whole domain.
  3. Resend  (RESEND_API_KEY + EMAIL_FROM) — needs a verified domain to send to
     recipients other than the account owner.
  4. Gmail SMTP (SMTP_SENDER + SMTP_APP_PASSWORD) — fallback; blocked on Render.
Sends: the per-lane count report, the Amazon shipment-confirmation file, the
end-of-day summary, and a test email.
"""
import base64
import json
import smtplib
import ssl
import urllib.error
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import config

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover
    _SSL = ssl.create_default_context()


# ---------------------------------------------------------------------------
# Transport — Resend (HTTP) preferred, SMTP fallback
# ---------------------------------------------------------------------------
def _deliver(subject, text, html=None, attachments=None, recipients=None):
    """Send an email. attachments: list of {'filename','content'} (content=str).
    `recipients` overrides config.EMAIL_RECIPIENTS for this send.
    Returns 'sent' | 'disabled' | 'not-configured'; raises on a send failure."""
    if not config.EMAIL_ENABLED:
        return "disabled"
    recipients = list(recipients) if recipients else list(config.EMAIL_RECIPIENTS)
    if not recipients:
        return "not-configured"
    if config.GMAIL_CLIENT_ID and config.GMAIL_CLIENT_SECRET and config.GMAIL_REFRESH_TOKEN:
        return _deliver_gmail_api(subject, text, html, attachments, recipients)
    if config.BREVO_API_KEY and config.EMAIL_FROM:
        return _deliver_brevo(subject, text, html, attachments, recipients)
    if config.RESEND_API_KEY and config.EMAIL_FROM:
        return _deliver_resend(subject, text, html, attachments, recipients)
    if config.SMTP_SENDER and config.SMTP_APP_PASSWORD:
        return _deliver_smtp(subject, text, html, attachments, recipients)
    return "not-configured"


def _gmail_access_token():
    """Mint a fresh 1-hour access token from the stored OAuth2 refresh token.
    Uses google-auth (already a dependency) so token refresh, clock-skew and
    retries are handled correctly. Raises RuntimeError with the reason on failure
    (e.g. an expired/revoked refresh token -> invalid_grant -> re-run consent)."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    creds = Credentials(
        token=None,
        refresh_token=config.GMAIL_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=config.GMAIL_CLIENT_ID,
        client_secret=config.GMAIL_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    try:
        creds.refresh(Request())
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "Gmail OAuth token refresh failed (re-check the client ID/secret and "
            f"refresh token, or re-authorise): {e}")
    return creds.token


def _deliver_gmail_api(subject, text, html, attachments, recipients):
    token = _gmail_access_token()
    outer = MIMEMultipart("mixed")
    alt = MIMEMultipart("alternative")
    if text:
        alt.attach(MIMEText(text, "plain", "utf-8"))
    if html:
        alt.attach(MIMEText(html, "html", "utf-8"))
    if not (text or html):
        alt.attach(MIMEText(subject, "plain", "utf-8"))
    outer.attach(alt)
    for a in (attachments or []):
        part = MIMEText(a["content"], "plain", "utf-8")
        part.add_header("Content-Disposition", "attachment", filename=a["filename"])
        outer.attach(part)
    outer["Subject"] = subject
    if config.EMAIL_FROM:            # else Gmail uses the authorised account's address
        outer["From"] = config.EMAIL_FROM
    outer["To"] = ", ".join(recipients)
    # RFC 5322 bytes -> base64url in the Gmail Message.raw field (use as_bytes so
    # UTF-8 content encodes correctly).
    raw = base64.urlsafe_b64encode(outer.as_bytes()).decode("ascii")
    req = urllib.request.Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        data=json.dumps({"raw": raw}).encode("utf-8"),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_SSL) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Gmail API {e.code}: {e.read().decode('utf-8', 'replace')}")
    if resp.get("id"):
        return "sent"
    raise RuntimeError("Gmail API: " + json.dumps(resp))


def _deliver_brevo(subject, text, html, attachments, recipients):
    sender = {"email": config.EMAIL_FROM, "name": "Trishoolin Ops"}
    payload = {"sender": sender,
               "to": [{"email": r} for r in recipients],
               "subject": subject}
    if html:
        payload["htmlContent"] = html
    if text:
        payload["textContent"] = text
    if not (html or text):
        payload["textContent"] = subject
    if attachments:
        payload["attachment"] = [
            {"name": a["filename"],
             "content": base64.b64encode(a["content"].encode("utf-8")).decode("ascii")}
            for a in attachments]
    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email", data=json.dumps(payload).encode("utf-8"),
        headers={"api-key": config.BREVO_API_KEY,
                 "Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "trishoolin-ops/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=_SSL) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Brevo {e.code}: {e.read().decode('utf-8', 'replace')}")
    if resp.get("messageId") or resp.get("messageIds"):
        return "sent"
    raise RuntimeError("Brevo: " + json.dumps(resp))


def _deliver_resend(subject, text, html, attachments, recipients):
    payload = {"from": config.EMAIL_FROM, "to": list(recipients),
               "subject": subject}
    if text:
        payload["text"] = text
    if html:
        payload["html"] = html
    if attachments:
        payload["attachments"] = [
            {"filename": a["filename"],
             "content": base64.b64encode(a["content"].encode("utf-8")).decode("ascii")}
            for a in attachments]
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + config.RESEND_API_KEY,
                 "Content-Type": "application/json",
                 # Resend is behind Cloudflare, which blocks the default Python
                 # User-Agent with a 403 (error 1010). Any real UA gets through.
                 "User-Agent": "trishoolin-ops/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=_SSL) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Resend {e.code}: {e.read().decode('utf-8', 'replace')}")
    if resp.get("id"):
        return "sent"
    raise RuntimeError("Resend: " + json.dumps(resp))


def _deliver_smtp(subject, text, html, attachments, recipients):
    outer = MIMEMultipart("mixed")
    alt = MIMEMultipart("alternative")
    if text:
        alt.attach(MIMEText(text, "plain"))
    if html:
        alt.attach(MIMEText(html, "html"))
    outer.attach(alt)
    for a in (attachments or []):
        part = MIMEText(a["content"], "plain", "utf-8")
        part.add_header("Content-Disposition", "attachment", filename=a["filename"])
        outer.attach(part)
    outer["Subject"] = subject
    outer["From"] = config.SMTP_SENDER
    outer["To"] = ", ".join(recipients)
    with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as server:
        server.login(config.SMTP_SENDER, config.SMTP_APP_PASSWORD)
        server.sendmail(config.SMTP_SENDER, recipients, outer.as_string())
    return "sent"


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------
def send_test_email():
    """Send a simple 'system is working' email to the configured recipients."""
    text = ("Trishoolin Ops — test email.\n\n"
            "The system is working. If you're reading this, email delivery is set up "
            "correctly.\n")
    html = ("""<div style="font-family:Arial,Helvetica,sans-serif;color:#222">
      <h2 style="margin:0 0 6px">Trishoolin Ops — the system is working ✅</h2>
      <p style="color:#555;margin:0">This is a test email. If you're reading it,
         email delivery is set up correctly.</p>
    </div>""")
    return _deliver("Trishoolin Ops — test email (system is working)", text, html)


def render_daily_summary(rows, totals, date_label=""):
    """Build (subject, text, html) for the daily summary — no send, so it can be
    previewed before sending. Mapping:
      Orders Not Shipped Today = yellow (due today, not shipped)
      Buy Ship Orders Pending  = red   (overdue / buy-ship pending)"""
    cell = "padding:8px 14px;border:1px solid #e2e2e2"
    ncell = cell + ";text-align:right;font-variant-numeric:tabular-nums"
    trs = ""
    for r in rows:
        not_shipped = r.get("yellow_today", 0)
        buy_ship = r.get("red_today", 0)
        trs += (f"<tr><td style='{cell}'>{r.get('lane','')}</td>"
                f"<td style='{ncell}'>{not_shipped}</td>"
                f"<td style='{ncell}'>{buy_ship}</td></tr>")
    total_ns = totals.get("yellow_today", 0)
    total_bs = totals.get("red_today", 0)
    note = ("This is an automatically generated message from Trishoolin Ops "
            "— please do not reply.")
    html = f"""<div style="font-family:Arial,Helvetica,sans-serif;color:#1f2937;font-size:14px">
  <h2 style="margin:0 0 10px">Daily Pending Shipment Status by Lane</h2>
  <p style="margin:0 0 4px">Hi Team,</p>
  <p style="margin:0 0 14px">Please find today's pending shipment status by lane below:</p>
  <table style="border-collapse:collapse;font-size:14px">
    <thead><tr style="background:#f3f4f6">
      <th style="{cell};text-align:left">Lane</th>
      <th style="{cell};text-align:right">Orders Not Shipped Today</th>
      <th style="{cell};text-align:right">Buy Ship Orders Pending</th>
    </tr></thead>
    <tbody>{trs}
      <tr style="font-weight:bold;background:#fafafa">
        <td style="{cell}">TOTAL</td>
        <td style="{ncell}">{total_ns}</td>
        <td style="{ncell}">{total_bs}</td>
      </tr>
    </tbody>
  </table>
  <p style="margin:16px 0 4px">Kindly review the pending orders for your respective lanes and ensure they
     are processed at the earliest. If there are any blockers or delays, please inform the team immediately.</p>
  <p style="margin:10px 0 0">Thank you.</p>
  <p style="margin:16px 0 0;color:#9ca3af;font-size:12px;font-style:italic">{note}</p>
</div>"""
    lines = "\n".join(f"  {r.get('lane',''):<12} not shipped: {r.get('yellow_today',0):>4}   "
                      f"buy ship pending: {r.get('red_today',0):>4}" for r in rows)
    text = ("Daily Pending Shipment Status by Lane\n\nHi Team,\n\n"
            "Please find today's pending shipment status by lane below:\n\n"
            f"{lines}\n  {'TOTAL':<12} not shipped: {total_ns:>4}   buy ship pending: {total_bs:>4}\n\n"
            "Kindly review the pending orders for your respective lanes and ensure they are processed "
            "at the earliest. If there are any blockers or delays, please inform the team immediately.\n\n"
            f"Thank you.\n\n— {note}")
    subject = (f"Daily Pending Shipment Status by Lane{(' — ' + date_label) if date_label else ''}: "
               f"{total_ns} not shipped / {total_bs} buy-ship pending")
    return subject, text, html


def send_daily_summary(rows, totals, date_label="", recipients=None):
    """Render + send the daily summary. Returns 'sent' | 'disabled' | 'not-configured'."""
    subject, text, html = render_daily_summary(rows, totals, date_label)
    return _deliver(subject, text, html, recipients=recipients)


def send_amazon_file(awb, project, file_text, filename, summary):
    """Email the Amazon shipment-confirmation file (tab-separated) as an attachment."""
    cancelled = summary.get("cancelled_orders", [])
    body = (
        f"Amazon shipment-confirmation file for AWB {awb} ({project}).\n\n"
        f"Confirmed orders: {summary.get('confirmed_orders', 0)}\n"
        f"Cancelled (not in the unshipped report): {len(cancelled)}\n"
    )
    if cancelled:
        body += "Cancelled order IDs: " + ", ".join(cancelled) + "\n"
    body += "\nUpload the attached file to Amazon (Shipping Confirmation)."
    subject = (f"Amazon shipment file — {project or 'shipment'} — AWB {awb} "
               f"({summary.get('confirmed_orders', 0)} orders)")
    return _deliver(subject, body, None, [{"filename": filename, "content": file_text}])


def _table(red_counts, yellow_counts):
    rows = ""
    for lane in config.LANES:
        r = red_counts.get(lane, 0)
        y = yellow_counts.get(lane, 0)
        rows += (
            f"<tr>"
            f"<td style='padding:6px 12px;border:1px solid #ddd'>{lane}</td>"
            f"<td style='padding:6px 12px;border:1px solid #ddd;text-align:center;"
            f"background:#ffd9d9'>{r}</td>"
            f"<td style='padding:6px 12px;border:1px solid #ddd;text-align:center;"
            f"background:#fff2c2'>{y}</td>"
            f"</tr>"
        )
    total_red = sum(red_counts.get(l, 0) for l in config.LANES)
    total_yellow = sum(yellow_counts.get(l, 0) for l in config.LANES)
    return rows, total_red, total_yellow


def send_counts_email(uploaded_lane, red_count, yellow_count,
                      red_lane_counts, yellow_lane_counts,
                      red_history_total=None, red_history_added=None, assignee=None):
    """Per-lane count report. Returns 'sent' | 'disabled' | 'not-configured'."""
    rows, total_red, total_yellow = _table(red_lane_counts, yellow_lane_counts)

    assignee_html = ""
    if assignee:
        assignee_html = (f'<p style="margin:0 0 4px;color:#555">Lane <b>{uploaded_lane}</b>'
                         f' &nbsp;·&nbsp; Assignee <b>{assignee}</b></p>')
    history_line = ""
    if red_history_total is not None:
        added = f" (+{red_history_added} new)" if red_history_added else ""
        history_line = (f'<p style="margin:0 0 16px;color:#555">On record for this lane '
                        f'(all overdue ever): <b>{red_history_total}</b>{added}.</p>')

    html = f"""\
<div style="font-family:Arial,Helvetica,sans-serif;color:#222">
  <h2 style="margin:0 0 4px">Shipping Queue update</h2>
  {assignee_html}
  <p style="margin:0 0 4px;color:#555">
     Just processed: <b>{uploaded_lane}</b> &mdash;
     <span style="color:#c0392b"><b>{red_count}</b> red (should have shipped
     yesterday, still not shipped)</span>,
     <span style="color:#b7950b"><b>{yellow_count}</b> yellow (due to ship
     today, not shipped)</span>.
  </p>
  {history_line}
  <table style="border-collapse:collapse;font-size:14px">
    <thead>
      <tr style="background:#f2f2f2">
        <th style="padding:6px 12px;border:1px solid #ddd;text-align:left">Lane</th>
        <th style="padding:6px 12px;border:1px solid #ddd">Red / OVERDUE</th>
        <th style="padding:6px 12px;border:1px solid #ddd">Yellow / TODAY</th>
      </tr>
    </thead>
    <tbody>
      {rows}
      <tr style="font-weight:bold;background:#fafafa">
        <td style="padding:6px 12px;border:1px solid #ddd">TOTAL</td>
        <td style="padding:6px 12px;border:1px solid #ddd;text-align:center">{total_red}</td>
        <td style="padding:6px 12px;border:1px solid #ddd;text-align:center">{total_yellow}</td>
      </tr>
    </tbody>
  </table>
</div>"""

    text_history = ""
    if red_history_total is not None:
        added = f" (+{red_history_added} new)" if red_history_added else ""
        text_history = f" On record for this lane (all overdue ever): {red_history_total}{added}."
    assignee_text = f" Assignee: {assignee}." if assignee else ""
    text = (f"Shipping Queue update — {uploaded_lane}:{assignee_text} "
            f"{red_count} red (overdue, still not shipped), "
            f"{yellow_count} yellow (due today, not shipped).{text_history} "
            f"Totals across all lanes: {total_red} red, {total_yellow} yellow.")

    who = f" · {assignee}" if assignee else ""
    subject = f"Shipping Queue — {uploaded_lane}{who}: {red_count} red / {yellow_count} yellow"
    return _deliver(subject, text, html)
