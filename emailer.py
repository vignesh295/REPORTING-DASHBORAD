"""
Sends the per-lane order-count report over Gmail SMTP (SSL) using an App Password.
Also emails the generated Amazon shipment-confirmation file as an attachment.
"""
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import config


def send_amazon_file(awb, project, file_text, filename, summary):
    """Email the Amazon shipment-confirmation file (tab-separated) as an attachment.
    Returns 'sent' | 'disabled' | 'not-configured'; raises on SMTP failure."""
    if not config.EMAIL_ENABLED:
        return "disabled"
    if not (config.SMTP_SENDER and config.SMTP_APP_PASSWORD and config.EMAIL_RECIPIENTS):
        return "not-configured"

    cancelled = summary.get("cancelled_orders", [])
    body = (
        f"Amazon shipment-confirmation file for AWB {awb} ({project}).\n\n"
        f"Confirmed orders: {summary.get('confirmed_orders', 0)}\n"
        f"Cancelled (not in the unshipped report): {len(cancelled)}\n"
    )
    if cancelled:
        body += "Cancelled order IDs: " + ", ".join(cancelled) + "\n"
    body += "\nUpload the attached file to Amazon (Shipping Confirmation)."

    msg = MIMEMultipart()
    msg["Subject"] = (f"Amazon shipment file — {project or 'shipment'} — AWB {awb} "
                      f"({summary.get('confirmed_orders', 0)} orders)")
    msg["From"] = config.SMTP_SENDER
    msg["To"] = ", ".join(config.EMAIL_RECIPIENTS)
    msg.attach(MIMEText(body, "plain"))
    attachment = MIMEText(file_text, "plain", "utf-8")
    attachment.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(attachment)

    with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT) as server:
        server.login(config.SMTP_SENDER, config.SMTP_APP_PASSWORD)
        server.sendmail(config.SMTP_SENDER, config.EMAIL_RECIPIENTS, msg.as_string())
    return "sent"


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
                      red_history_total=None, red_history_added=None,
                      assignee=None):
    """
    Returns a short status string: 'sent', 'disabled', or 'not-configured'.
    Raises on an actual SMTP failure so the caller can show the error.

    red_history_total / red_history_added (optional): the lane's cumulative
    overdue-on-record count and how many were newly added this upload.
    assignee (optional): the username of whoever uploaded the report.
    """
    if not config.EMAIL_ENABLED:
        return "disabled"
    if not (config.SMTP_SENDER and config.SMTP_APP_PASSWORD and config.EMAIL_RECIPIENTS):
        return "not-configured"

    rows, total_red, total_yellow = _table(red_lane_counts, yellow_lane_counts)

    assignee_html = ""
    if assignee:
        assignee_html = (
            f'<p style="margin:0 0 4px;color:#555">'
            f'Lane <b>{uploaded_lane}</b> &nbsp;·&nbsp; '
            f'Assignee <b>{assignee}</b></p>'
        )

    history_line = ""
    if red_history_total is not None:
        added = f" (+{red_history_added} new)" if red_history_added else ""
        history_line = (
            f'<p style="margin:0 0 16px;color:#555">'
            f'On record for this lane (all overdue ever): '
            f'<b>{red_history_total}</b>{added}.</p>'
        )

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
  <p style="margin:16px 0 0;color:#999;font-size:12px">
     Counts reflect the current contents of each lane tab.
  </p>
</div>"""

    text_history = ""
    if red_history_total is not None:
        added = f" (+{red_history_added} new)" if red_history_added else ""
        text_history = (
            f" On record for this lane (all overdue ever): "
            f"{red_history_total}{added}."
        )
    assignee_text = f" Assignee: {assignee}." if assignee else ""
    text = (
        f"Shipping Queue update — {uploaded_lane}:{assignee_text} "
        f"{red_count} red (overdue, still not shipped), "
        f"{yellow_count} yellow (due today, not shipped)."
        f"{text_history}"
        f" Totals across all lanes: {total_red} red, {total_yellow} yellow."
    )

    msg = MIMEMultipart("alternative")
    who = f" · {assignee}" if assignee else ""
    msg["Subject"] = (
        f"Shipping Queue — {uploaded_lane}{who}: "
        f"{red_count} red / {yellow_count} yellow"
    )
    msg["From"] = config.SMTP_SENDER
    msg["To"] = ", ".join(config.EMAIL_RECIPIENTS)
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT) as server:
        server.login(config.SMTP_SENDER, config.SMTP_APP_PASSWORD)
        server.sendmail(config.SMTP_SENDER, config.EMAIL_RECIPIENTS, msg.as_string())

    return "sent"
