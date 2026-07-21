"""
Flask web app — Shipping Queue / lane reporting dashboard.

Roles:
  ops   -> only the Upload page; after uploading they get a plain confirmation
           (no stats).
  admin -> the dashboard (per-lane red/yellow overview), lane details, and upload.

Flow on upload:
  1. A logged-in user picks a lane and uploads the .xlsm (SHIPPING QUEUE workbook).
  2. Orders are split into red (OVERDUE) and yellow (TODAY).
  3. Red today -> RED sheet / lane tab; yellow -> YELLOW sheet / lane tab (replaced).
     Red also accumulates into the lane's "All Red" history tab (deduped).
  4. The upload is recorded locally (for the dashboard) with the uploader's name.
  5. A per-lane count email is sent via Gmail, showing lane + assignee + counts.

Run:  python app.py   ->  http://127.0.0.1:5000
"""
import datetime
import os
import tempfile
import traceback

from flask import (Flask, flash, jsonify, redirect, render_template, request,
                   session, url_for)

import auth
import awb_store
import config
import parser as xparser
import shipment_store
import store

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_MB * 1024 * 1024

ALLOWED_EXT = {".xlsm", ".xlsx"}


@app.context_processor
def inject_user():
    """Make the current user + auth flag available to every template (sidebar)."""
    return {"user": auth.current_user(), "auth_enabled": config.AUTH_ENABLED}


def _status_banner():
    return {
        "google_ready": config.google_ready(),
        "email_ready": config.email_ready(),
        "red_id_set": bool(config.RED_SPREADSHEET_ID),
        "yellow_id_set": bool(config.YELLOW_SPREADSHEET_ID),
        "sa_exists": config.has_service_account(),
    }


def _seed_admin_from_env():
    """On a fresh/ephemeral host (e.g. Render), create the admin login from
    ADMIN_USER / ADMIN_PASSWORD env vars if no users exist yet."""
    u, p = os.environ.get("ADMIN_USER"), os.environ.get("ADMIN_PASSWORD")
    try:
        if u and p and not auth.list_users():
            auth.set_user(u, p, "admin")
    except Exception:  # noqa: BLE001 - never block startup on a seed hiccup
        traceback.print_exc()


_seed_admin_from_env()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if auth.current_user() is not None:
        return redirect(url_for("home"))
    if request.method == "POST":
        user = auth.verify(request.form.get("username", ""),
                           request.form.get("password", ""))
        if user is None:
            flash("Wrong username or password.", "error")
            return redirect(url_for("login"))
        auth.login_user(user)
        return redirect(url_for("home"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    auth.logout_user()
    flash("Signed out.", "ok")
    return redirect(url_for("login"))


@app.route("/")
def home():
    """Landing hub: admins -> workflow cards, ops -> their upload page."""
    user = auth.current_user()
    if user is None:
        return redirect(url_for("login"))
    if user["role"] != "admin":
        return redirect(url_for("upload"))
    return render_template("home.html")


def _dest(project):
    """Destination (UAE / AUS) from a project/lane string."""
    p = (project or "").upper()
    if "UAE" in p:
        return "UAE"
    if "AUS" in p:
        return "AUS"
    return ""


@app.route("/shipment")
@auth.role_required("admin")
def shipment():
    rows = []
    for key, m in shipment_store.manifests().items():
        dest = _dest(m.get("project", ""))
        rows.append({
            "key": key, "awb": m.get("awb", ""), "project": m.get("project", ""),
            "dest": dest, "orders": len(m.get("rows", [])), "received": m.get("received"),
            "unshipped_ready": bool(shipment_store.get_unshipped(dest)),
            "generated": shipment_store.get_generated(key),
        })
    rows.sort(key=lambda r: r["received"] or "", reverse=True)
    return render_template("shipment.html", manifests=rows,
                           unshipped=shipment_store.unshipped_projects(),
                           status=_status_banner())


@app.route("/shipment/unshipped", methods=["POST"])
@auth.role_required("admin")
def shipment_unshipped():
    project = request.form.get("project", "").strip().upper()
    upload = request.files.get("file")
    if project not in ("UAE", "AUS"):
        flash("Pick a project (UAE or AUS).", "error")
        return redirect(url_for("shipment"))
    if not upload or not upload.filename:
        flash("Choose the unshipped-orders report file.", "error")
        return redirect(url_for("shipment"))
    ext = os.path.splitext(upload.filename)[1].lower() or ".txt"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        upload.save(tmp.name)
        tmp.close()
        import shipment_core
        _headers, rows = shipment_core.read_table(tmp.name)
        ids = shipment_core.unshipped_order_ids(rows)
        shipment_store.set_unshipped(project, ids, upload.filename)
        flash(f"Unshipped report for {project}: {len(ids)} orders on record.", "ok")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        flash(f"Couldn't read the report: {e}", "error")
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    return redirect(url_for("shipment"))


@app.route("/shipment/generate/<path:key>", methods=["POST"])
@auth.role_required("admin")
def shipment_generate(key):
    import emailer
    import shipment_core
    man = shipment_store.get_manifest(key)
    if not man:
        flash("No manifest for that AWB.", "error")
        return redirect(url_for("shipment"))
    dest = _dest(man.get("project", ""))
    uns = shipment_store.get_unshipped(dest)
    if not uns:
        flash(f"Upload the {dest or 'destination'} unshipped report first.", "error")
        return redirect(url_for("shipment"))

    text, summary = shipment_core.build_confirmation(man["rows"], set(uns["order_ids"]))
    awb = man.get("awb", "") or key
    filename = ("amazon-shipment-"
                + (man.get("project") or dest or "ship").replace(" ", "_")
                + f"-{awb}.txt")
    shipment_store.save_generated(key, text, summary, filename)

    try:
        email_status = emailer.send_amazon_file(awb, man.get("project", ""), text, filename, summary)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        email_status = f"ERROR: {e}"
    flash(f"Generated: {summary['confirmed_orders']} confirmed, "
          f"{len(summary['cancelled_orders'])} cancelled. Email: {email_status}.", "ok")
    return redirect(url_for("shipment"))


@app.route("/shipment/download/<path:key>")
@auth.role_required("admin")
def shipment_download(key):
    from flask import Response
    gen = shipment_store.get_generated(key)
    if not gen:
        flash("Nothing generated for that AWB yet.", "error")
        return redirect(url_for("shipment"))
    return Response(gen["text"], mimetype="text/plain",
                    headers={"Content-Disposition": f'attachment; filename="{gen["filename"]}"'})


@app.route("/shipment/sync", methods=["POST"])
@auth.role_required("admin")
def shipment_sync():
    """Manual trigger for the delivery sync (same job the cron runs): pull Drive
    files, read the AWB sheet, update the SHIPMENT LOGS sheet, generate any
    ready Amazon files. Lets you run it on demand and see the result."""
    import automation
    try:
        rep = automation.process_deliveries()
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        flash(f"Sync failed: {e}", "error")
        return redirect(url_for("shipment"))
    msg = (f"Sync done — pulled {rep.get('files_pulled', 0)} files "
           f"({rep.get('awbs_ingested', 0)} AWBs), {rep.get('delivered_seen', 0)} delivered, "
           f"log +{rep.get('log_added', 0)} new / {rep.get('log_updated', 0)} updated, "
           f"{len(rep.get('generated', []))} Amazon files generated.")
    if rep.get("errors"):
        flash(msg + " Errors: " + " | ".join(rep["errors"][:3]), "error")
    else:
        flash(msg, "success")
    return redirect(url_for("shipment"))


@app.route("/buy-ship-left/pull-reports", methods=["POST"])
@auth.role_required("admin")
def buy_ship_pull_reports():
    """Pull today's order reports from the Google Chat 'ORDER REPORT' spaces,
    split them, and refresh the RED/YELLOW sheets. Reports what it saw so a no-op
    run is self-explanatory, and flags lanes with no report today."""
    import chat_reports
    rep = {}
    try:
        chat_reports.sync(rep)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        flash(f"Order-report sync failed: {e}", "error")
        return redirect(url_for("buy_ship_left"))
    processed = rep.get("chat_processed", [])
    missing = rep.get("lanes_missing_today", [])
    unmatched = rep.get("chat_unmatched", [])
    if processed:
        lanes = ", ".join(f"{p['lane']} ({p['red']}R/{p['yellow']}Y)" for p in processed)
        msg = f"Today's order reports synced — {len(processed)} lane(s): {lanes}."
        if missing:
            msg += " No report today for: " + ", ".join(missing) + "."
        if unmatched:
            msg += " Unmapped files (tell me their lanes): " + ", ".join(unmatched[:8]) + "."
    else:
        msg = (f"No order reports mapped for today. Token client: {rep.get('token_client', '?')}. "
               f"Token scopes: {rep.get('token_scopes', '?')}. "
               f"Spaces: {rep.get('chat_spaces') or 'none'}. "
               f"Today's files: {rep.get('chat_files_seen') or 'none'}. "
               f"Unmapped: {unmatched[:12] or 'none'}.")
    if rep.get("errors"):
        flash(msg + " Errors: " + " | ".join(rep["errors"][:3]), "error")
    else:
        flash(msg, "success" if processed else "error")
    return redirect(url_for("buy_ship_left"))


_SUMMARY_RECIPIENTS = ["harshsinghmaurya@trishoolinhouse.com", "nikhil@trishoolinhouse.com",
                       "vijays@trishoolinhouse.com", "shubham@trishoolinhouse.com"]


@app.route("/buy-ship-left/send-summary", methods=["POST"])
@auth.role_required("admin")
def send_summary_now():
    """Email today's Pending Shipment Status by Lane to the team recipients now."""
    import automation
    import emailer
    import datetime
    label = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=5, minutes=30))).strftime("%d.%m.%Y")
    try:
        rows, totals = automation._lane_pending_counts()
        status = emailer.send_daily_summary(rows, totals, label, recipients=_SUMMARY_RECIPIENTS)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        flash(f"Send failed: {e}", "error")
        return redirect(url_for("buy_ship_left"))
    if status == "sent":
        flash("Summary email sent to: " + ", ".join(_SUMMARY_RECIPIENTS) + ".", "success")
    else:
        flash(f"Not sent (status: {status}). Check email is enabled + Gmail creds are set.", "error")
    return redirect(url_for("buy_ship_left"))


@app.route("/api/shipment/notify", methods=["POST"])
def api_shipment_notify():
    """Drive-folder Apps Script pings this with a new file's id; the app pulls
    and parses the shipment manifest."""
    err = _api_token_error()
    if err:
        return jsonify(ok=False, error=err[0]), err[1]
    payload = request.get_json(silent=True) or {}
    file_id = str(payload.get("fileId", "")).strip()
    if not file_id:
        return jsonify(ok=False, error="fileId is required"), 400
    if shipment_store.already_ingested(file_id):
        return jsonify(ok=True, skipped="already ingested")
    try:
        import drive
        import shipment_core
        data, ext = drive.download(file_id, payload.get("mimeType"), payload.get("name", ""))
        raw = data if isinstance(data, bytes) else str(data).encode("utf-8")
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext or ".xlsx")
        tmp.write(raw)
        tmp.close()
        try:
            _headers, rows = shipment_core.read_table(tmp.name)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
        key, n = shipment_store.store_manifest(payload.get("awb", ""), payload.get("project", ""),
                                               rows, file_id, payload.get("name", ""))
        return jsonify(ok=True, awb=payload.get("awb", ""), key=key, orders=n)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify(ok=False, error=str(e)), 500


@app.route("/cron/daily-summary", methods=["GET", "POST"])
def cron_daily_summary():
    """End-of-day: email the per-lane red/yellow summary. Schedule for ~7pm."""
    err = _api_token_error()
    if err:
        return jsonify(ok=False, error=err[0]), err[1]
    import automation
    import datetime
    return jsonify(ok=True, **automation.daily_summary(datetime.date.today().strftime("%d.%m.%Y")))


@app.route("/cron/process-deliveries", methods=["GET", "POST"])
def cron_process_deliveries():
    """Pull new Drive files, read the AWB sheet for delivered AWBs, and generate
    + email the Amazon file for any that are ready. Schedule every ~15-30 min."""
    err = _api_token_error()
    if err:
        return jsonify(ok=False, error=err[0]), err[1]
    import automation
    return jsonify(ok=True, **automation.process_deliveries())


@app.route("/buy-ship-left")
@auth.role_required("admin")
def buy_ship_left():
    return render_template("buy_ship_left.html")


# ---------------------------------------------------------------------------
# Dashboard (admin)
# ---------------------------------------------------------------------------
def _read_lane_tabs(lane):
    """(tabs, err) for a lane — reads the Sheet tabs when Google is configured."""
    tabs = {"red": ([], []), "yellow": ([], []), "allred": ([], [])}
    err = None
    if lane and config.google_ready():
        try:
            import sheets
            tabs["red"] = sheets.read_tab(config.RED_SPREADSHEET_ID, lane)
            tabs["yellow"] = sheets.read_tab(config.YELLOW_SPREADSHEET_ID, lane)
            tabs["allred"] = sheets.read_tab(config.RED_SPREADSHEET_ID,
                                             lane + config.RED_ALL_SUFFIX)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            err = str(e)
    return tabs, err


@app.route("/dashboard")
@auth.role_required("admin")
def dashboard():
    """Home: one lane at a time, with a dropdown to switch lanes."""
    lanes = config.LANES
    lane = request.args.get("lane") or (lanes[0] if lanes else None)
    if lane not in lanes:
        lane = lanes[0] if lanes else None
    rec = store.get_lane(lane) if lane else None
    tabs, err = _read_lane_tabs(lane)
    return render_template("dashboard.html", lane=lane, lanes=lanes, rec=rec,
                           google=config.google_ready(), tabs=tabs, err=err,
                           status=_status_banner())


@app.route("/refresh", methods=["POST"])
@auth.role_required("admin")
def refresh():
    """Reconcile the local lane counts against the live Google Sheets."""
    lane = request.form.get("lane", "")
    back = url_for("dashboard", lane=lane) if lane in config.LANES else url_for("dashboard")
    if not config.google_ready():
        flash("Google Sheets isn't configured yet — nothing to refresh from.", "error")
        return redirect(back)
    try:
        import sheets
        red_counts = sheets.lane_counts(config.RED_SPREADSHEET_ID)
        yellow_counts = sheets.lane_counts(config.YELLOW_SPREADSHEET_ID)
        red_all = sheets.all_red_counts(config.RED_SPREADSHEET_ID)
        store.reconcile_from_sheets(red_counts, yellow_counts, red_all)
        flash("Counts refreshed from Google Sheets.", "ok")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        flash(f"Refresh failed: {e}", "error")
    return redirect(back)


# The lane detail is now the dashboard itself (one lane + switcher). Keep this
# path as a redirect so older links still work.
@app.route("/lane/<lane>")
@auth.role_required("admin")
def lane_view(lane):
    return redirect(url_for("dashboard", lane=lane))


# ---------------------------------------------------------------------------
# AWB tracking — batches shipped out; delivery status fed in via Apps Script.
# ---------------------------------------------------------------------------
def _api_token_error():
    """None if the request carries the right token, else (message, http_status)."""
    if not config.AWB_API_TOKEN:
        return ("AWB API not configured — set a token on the Settings page.", 503)
    supplied = (request.headers.get("X-API-Key")
                or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                or request.args.get("token", ""))
    if supplied != config.AWB_API_TOKEN:
        return ("Unauthorized.", 401)
    return None


@app.route("/api/awb/ingest", methods=["POST"])
def api_awb_ingest():
    """Apps Script (Drive folder watcher) posts the new AWB file's rows here."""
    err = _api_token_error()
    if err:
        return jsonify(ok=False, error=err[0]), err[1]
    payload = request.get_json(silent=True)
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return jsonify(ok=False, error="Send a JSON list of rows, or {\"rows\": [...]}."), 400
    awbs_touched, orders_added = awb_store.ingest_rows(rows)
    return jsonify(ok=True, awbs=awbs_touched, orders_added=orders_added, **awb_store.summary())


@app.route("/api/awb/status", methods=["POST"])
def api_awb_status():
    """Apps Script (status sheet watcher) posts delivery updates here."""
    err = _api_token_error()
    if err:
        return jsonify(ok=False, error=err[0]), err[1]
    payload = request.get_json(silent=True) or {}
    updates = payload.get("updates") if isinstance(payload, dict) and "updates" in payload else [payload]
    matched = []
    for upd in updates:
        if not isinstance(upd, dict):
            continue
        status = str(upd.get("status", "")).strip().lower()
        if status.startswith("deliver"):
            key = awb_store.mark_delivered(awb=upd.get("awb"),
                                           tracking_id=upd.get("tracking_id"),
                                           when=upd.get("delivered_at"))
            if key:
                matched.append(key)
    return jsonify(ok=True, delivered=matched, matched=len(matched))


@app.route("/awb")
@auth.role_required("admin")
def awb():
    status_filter = request.args.get("status", "active")
    return render_template("awb.html", rows=awb_store.rows(status_filter),
                           summary=awb_store.summary(), status_filter=status_filter,
                           token_set=bool(config.AWB_API_TOKEN))


@app.route("/awb/<awb_no>")
@auth.role_required("admin")
def awb_detail(awb_no):
    rec = awb_store.get(awb_no)
    if not rec:
        flash("AWB not found.", "error")
        return redirect(url_for("awb"))
    return render_template("awb_detail.html", a=rec)


# ---------------------------------------------------------------------------
# Settings (admin) — configure app + manage users
# ---------------------------------------------------------------------------
@app.route("/settings")
@auth.role_required("admin")
def settings():
    return render_template("settings.html", cfg=config.current_settings(),
                           users=auth.list_users(), status=_status_banner(),
                           sa_email=config.service_account_email(),
                           me=auth.current_user()["username"])


@app.route("/settings/config", methods=["POST"])
@auth.role_required("admin")
def settings_config():
    f = request.form

    def _multi(name):
        return [x.strip() for x in f.get(name, "").replace(",", "\n").splitlines() if x.strip()]

    updates = {
        "RED_SPREADSHEET_ID": f.get("red_id", "").strip(),
        "YELLOW_SPREADSHEET_ID": f.get("yellow_id", "").strip(),
        "LANES": _multi("lanes") or config.LANES,
        "EMAIL_ENABLED": f.get("email_enabled") == "on",
        "SMTP_SENDER": f.get("sender", "").strip(),
        "EMAIL_RECIPIENTS": _multi("recipients"),
        "EMAIL_FROM": f.get("email_from", "").strip(),
        "DRIVE_FOLDER_ID": f.get("drive_folder_id", "").strip(),
        "AWB_SHEET_ID": f.get("awb_sheet_id", "").strip(),
        "SHIPMENT_LOG_SHEET_ID": f.get("shipment_log_sheet_id", "").strip(),
    }
    # Blank secret fields mean "keep the existing one".
    if f.get("app_password", "").strip():
        updates["SMTP_APP_PASSWORD"] = f.get("app_password").strip()
    if f.get("resend_api_key", "").strip():
        updates["RESEND_API_KEY"] = f.get("resend_api_key").strip()
    if f.get("brevo_api_key", "").strip():
        updates["BREVO_API_KEY"] = f.get("brevo_api_key").strip()
    updates["GMAIL_CLIENT_ID"] = f.get("gmail_client_id", "").strip()
    if f.get("gmail_client_secret", "").strip():
        updates["GMAIL_CLIENT_SECRET"] = f.get("gmail_client_secret").strip()
    if f.get("gmail_refresh_token", "").strip():
        updates["GMAIL_REFRESH_TOKEN"] = f.get("gmail_refresh_token").strip()
    # Clicking "send test" means you want email on — enable it regardless of the tick.
    if f.get("_action") == "test":
        updates["EMAIL_ENABLED"] = True
    try:
        config.save_settings(updates)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        flash(f"Couldn't save settings: {e}", "error")
        return redirect(url_for("settings"))

    # "Send test email" saves first (so the toggle + creds take effect), then sends.
    if f.get("_action") == "test":
        import emailer
        try:
            status = emailer.send_test_email()
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            status = f"ERROR: {e}"
        if status == "sent":
            flash(f"Saved. Test email sent to {', '.join(config.EMAIL_RECIPIENTS)}.", "ok")
        elif status == "disabled":
            flash("Saved — but email is off. Tick 'Send the count email' and try again.", "error")
        elif status == "not-configured":
            flash("Saved — but set the Gmail sender, app password and recipients to send email.", "error")
        else:
            flash(f"Saved. Test email failed: {status}", "error")
    else:
        flash("Settings saved.", "ok")
    return redirect(url_for("settings"))


@app.route("/settings/email/test", methods=["POST"])
@auth.role_required("admin")
def settings_email_test():
    import emailer
    try:
        status = emailer.send_test_email()
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        status = f"ERROR: {e}"
    if status == "sent":
        flash(f"Test email sent to {', '.join(config.EMAIL_RECIPIENTS)}.", "ok")
    elif status == "disabled":
        flash("Email is off — tick 'Send the count email', Save, then try again.", "error")
    elif status == "not-configured":
        flash("Set the Gmail sender, app password and recipients first, then Save.", "error")
    else:
        flash(f"Test email failed: {status}", "error")
    return redirect(url_for("settings"))


@app.route("/settings/awb/token", methods=["POST"])
@auth.role_required("admin")
def settings_awb_token():
    import secrets
    config.save_settings({"AWB_API_TOKEN": secrets.token_urlsafe(24)})
    flash("New AWB API token generated.", "ok")
    return redirect(url_for("settings"))


@app.route("/settings/users/add", methods=["POST"])
@auth.role_required("admin")
def settings_user_add():
    f = request.form
    try:
        auth.set_user(f.get("username", ""), f.get("password", ""), f.get("role", "ops"))
        flash(f"User '{f.get('username','').strip()}' saved.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("settings"))


@app.route("/settings/users/passwd", methods=["POST"])
@auth.role_required("admin")
def settings_user_passwd():
    f = request.form
    username = f.get("username", "").strip()
    role = auth.list_users().get(username)
    if role is None:
        flash("No such user.", "error")
    else:
        try:
            auth.set_user(username, f.get("password", ""), role)
            flash(f"Password updated for '{username}'.", "ok")
        except ValueError as e:
            flash(str(e), "error")
    return redirect(url_for("settings"))


@app.route("/settings/users/role", methods=["POST"])
@auth.role_required("admin")
def settings_user_role():
    f = request.form
    username = f.get("username", "").strip()
    role = f.get("role", "ops")
    users = auth.list_users()
    if username not in users:
        flash("No such user.", "error")
    elif users[username] == "admin" and role != "admin" and auth.admin_count() <= 1:
        flash("Can't change the role of the last admin.", "error")
    else:
        try:
            auth.set_role(username, role)
            flash(f"Role for '{username}' set to {role}.", "ok")
        except ValueError as e:
            flash(str(e), "error")
    return redirect(url_for("settings"))


@app.route("/settings/users/delete", methods=["POST"])
@auth.role_required("admin")
def settings_user_delete():
    username = request.form.get("username", "").strip()
    users = auth.list_users()
    if username == auth.current_user()["username"]:
        flash("You can't delete your own account.", "error")
    elif users.get(username) == "admin" and auth.admin_count() <= 1:
        flash("Can't delete the last admin.", "error")
    elif auth.delete_user(username):
        flash(f"Deleted '{username}'.", "ok")
    else:
        flash("No such user.", "error")
    return redirect(url_for("settings"))


# ---------------------------------------------------------------------------
# Upload (ops + admin)
# ---------------------------------------------------------------------------
@app.route("/upload", methods=["GET"])
@auth.login_required
def upload():
    return render_template("upload.html", lanes=config.LANES, status=_status_banner())


@app.route("/process", methods=["POST"])
@auth.login_required
def process():
    user = auth.current_user()
    lane = request.form.get("lane", "")
    upload_file = request.files.get("file")

    if lane not in config.LANES:
        flash("Please choose a valid lane.", "error")
        return redirect(url_for("upload"))
    if not upload_file or upload_file.filename == "":
        flash("Please choose a file to upload.", "error")
        return redirect(url_for("upload"))

    ext = os.path.splitext(upload_file.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        flash("File must be a .xlsm or .xlsx workbook.", "error")
        return redirect(url_for("upload"))

    # Save to a temp file, parse, then delete.
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        upload_file.save(tmp.name)
        tmp.close()
        parsed = xparser.parse_workbook(tmp.name)
    except xparser.ParseError as e:
        flash(str(e), "error")
        return redirect(url_for("upload"))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        flash(f"Unexpected error reading the file: {e}", "error")
        return redirect(url_for("upload"))
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    result = {
        "lane": lane,
        "filename": upload_file.filename,
        "assignee": user["username"],
        "headers": parsed["headers"],
        "red": parsed["red"],
        "yellow": parsed["yellow"],
        "red_count": parsed["red_count"],
        "yellow_count": parsed["yellow_count"],
        "sheets_status": "",
        "email_status": "",
        "red_lane_counts": {},
        "yellow_lane_counts": {},
        "red_history_total": None,   # cumulative overdue-on-record for this lane
        "red_history_added": None,   # brand-new overdue orders added this upload
    }

    hist_total = None
    # ---- Write to Google Sheets (only if configured) ----
    if not config.google_ready():
        result["sheets_status"] = (
            "skipped — Google not configured (set RED/YELLOW spreadsheet IDs "
            "and the service-account file in .env)"
        )
    else:
        try:
            import sheets  # imported lazily so the app still runs without creds
            # Today's red -> the lane tab (replaced each upload).
            sheets.write_lane(config.RED_SPREADSHEET_ID, lane,
                             parsed["headers"], parsed["red"], config.RED_MODE)
            # Yellow -> the lane tab (replaced each upload).
            sheets.write_lane(config.YELLOW_SPREADSHEET_ID, lane,
                             parsed["headers"], parsed["yellow"], config.YELLOW_MODE)
            # Red history -> the lane's "All Red" tab (accumulates, deduped by
            # Order ID, with a DATE FLAGGED column stamped today).
            flagged_date = datetime.date.today().strftime("%d.%m.%Y")
            hist_total, hist_added = sheets.accumulate_red_history(
                config.RED_SPREADSHEET_ID, lane,
                parsed["headers"], parsed["red"], flagged_date)
            result["red_history_total"] = hist_total
            result["red_history_added"] = hist_added
            result["sheets_status"] = "written to Google Sheets"

            red_counts = sheets.lane_counts(config.RED_SPREADSHEET_ID)
            yellow_counts = sheets.lane_counts(config.YELLOW_SPREADSHEET_ID)
            result["red_lane_counts"] = red_counts
            result["yellow_lane_counts"] = yellow_counts

            # ---- Email ----
            try:
                import emailer
                result["email_status"] = emailer.send_counts_email(
                    lane, parsed["red_count"], parsed["yellow_count"],
                    red_counts, yellow_counts,
                    red_history_total=hist_total, red_history_added=hist_added,
                    assignee=user["username"],
                )
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                result["email_status"] = f"ERROR: {e}"
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            result["sheets_status"] = f"ERROR: {e}"

    # ---- Record locally for the dashboard (works even without Google) ----
    store.record_upload(
        lane,
        red_today=parsed["red_count"],
        yellow_today=parsed["yellow_count"],
        red_all_time=hist_total if hist_total is not None else 0,
        uploaded_by=user["username"],
    )

    # ops don't see the stats — just a confirmation. admins get the full report.
    if user["role"] != "admin":
        session["last_upload"] = {"lane": lane, "filename": upload_file.filename}
        return redirect(url_for("uploaded"))
    return render_template("result.html", r=result, lanes=config.LANES)


@app.route("/uploaded")
@auth.login_required
def uploaded():
    info = session.pop("last_upload", None)
    if not info:
        return redirect(url_for("upload"))
    return render_template("uploaded.html", info=info)


@app.errorhandler(413)
def too_large(_e):
    flash(f"That file is larger than the {config.MAX_CONTENT_MB} MB limit.", "error")
    return redirect(url_for("upload"))


if __name__ == "__main__":
    # Local dev. In production (Render) the app is served by gunicorn (see Procfile),
    # which imports `app:app` and binds to $PORT itself.
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "5000")),
            debug=os.environ.get("FLASK_DEBUG", "true").lower() == "true")
