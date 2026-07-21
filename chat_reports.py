"""
Pull order-report attachments from Google Chat (user OAuth — no bot).

Lists the Chat spaces the authorised user belongs to whose name contains
"ORDER REPORT", finds the newest .xlsx/.xlsm order-report attachment per lane,
downloads it, and runs the splitter to refresh the RED/YELLOW sheets.

Uses the same OAuth refresh token as Gmail send — it must have been consented
with the chat.spaces.readonly + chat.messages.readonly scopes (and drive.readonly
for reports attached from Drive).
"""
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import config
import splitter

CHAT = "https://chat.googleapis.com/v1"


def configured():
    return bool(config.GMAIL_CLIENT_ID and config.GMAIL_CLIENT_SECRET
                and config.GMAIL_REFRESH_TOKEN)


def _access_token():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    # No `scopes=` — on a refresh_token grant, passing scopes the token wasn't
    # granted returns invalid_scope. The access token inherits the token's own
    # granted scopes (chat.*, drive.readonly, gmail.send from the consent).
    creds = Credentials(
        token=None, refresh_token=config.GMAIL_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=config.GMAIL_CLIENT_ID, client_secret=config.GMAIL_CLIENT_SECRET)
    creds.refresh(Request())
    return creds.token


def _get(url, token, raw=False):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    return data if raw else json.loads(data)


def _list_spaces(token, name_has="ORDER REPORT"):
    out, page = [], ""
    while True:
        url = CHAT + "/spaces?pageSize=100" + (f"&pageToken={page}" if page else "")
        data = _get(url, token)
        for s in data.get("spaces", []):
            if name_has.upper() in (s.get("displayName", "") or "").upper():
                out.append({"name": s["name"], "displayName": s.get("displayName", "")})
        page = data.get("nextPageToken")
        if not page:
            return out


def _list_messages(token, space, max_pages=5):
    out, page, n = [], "", 0
    while n < max_pages:
        url = f"{CHAT}/{space}/messages?pageSize=100" + (f"&pageToken={page}" if page else "")
        data = _get(url, token)
        out.extend(data.get("messages", []))
        page = data.get("nextPageToken")
        n += 1
        if not page:
            break
    return out


def _attachments(msg):
    return msg.get("attachment") or msg.get("attachments") or []


def _download(att, token):
    name = att.get("contentName") or att.get("name") or ""
    adr = att.get("attachmentDataRef") or {}
    ddr = att.get("driveDataRef") or {}
    if adr.get("resourceName"):
        url = f"{CHAT}/media/{urllib.parse.quote(adr['resourceName'])}?alt=media"
        return name, _get(url, token, raw=True)
    if ddr.get("driveFileId"):
        url = f"https://www.googleapis.com/drive/v3/files/{ddr['driveFileId']}?alt=media"
        return name, _get(url, token, raw=True)
    return name, None


def sync(report):
    """Find + process the newest order-report file per lane across the ORDER REPORT
    Chat spaces. Fills `report` with diagnostics so a no-op run explains itself."""
    if not configured():
        report.setdefault("errors", []).append("chat: Gmail OAuth not configured")
        return
    try:
        token = _access_token()
    except Exception as e:  # noqa: BLE001
        report.setdefault("errors", []).append(f"chat token: {e}")
        return

    try:
        spaces = _list_spaces(token)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        report.setdefault("errors", []).append(f"chat spaces {e.code}: {body[:200]}")
        return
    report["chat_spaces"] = [s["displayName"] for s in spaces]

    seen_files, newest = [], {}   # lane -> (createTime, name, attachment)
    for sp in spaces:
        try:
            msgs = _list_messages(token, sp["name"])
        except urllib.error.HTTPError as e:
            report.setdefault("errors", []).append(
                f"chat msgs {sp['displayName']} {e.code}: {e.read().decode('utf-8','replace')[:150]}")
            continue
        for msg in msgs:
            ct = msg.get("createTime", "")
            for att in _attachments(msg):
                name = att.get("contentName") or att.get("name") or ""
                if not name.lower().endswith((".xlsx", ".xlsm")):
                    continue
                if "ORDER REPORT" not in name.upper():
                    continue
                seen_files.append(name)
                lane = splitter.lane_from_filename(name, config.LANES)
                if not lane:
                    continue
                if lane not in newest or ct > newest[lane][0]:
                    newest[lane] = (ct, name, att)
    report["chat_files_seen"] = seen_files

    for lane, (_ct, name, att) in newest.items():
        try:
            _n, data = _download(att, token)
            if not data:
                report.setdefault("errors", []).append(f"chat {name}: no downloadable data")
                continue
            ext = os.path.splitext(name)[1].lower() or ".xlsx"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            tmp.write(data)
            tmp.close()
            try:
                res = splitter.process_order_report(tmp.name)
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
            report.setdefault("chat_processed", []).append({
                "lane": lane, "file": name,
                "red": res.get("red"), "yellow": res.get("yellow"),
                "history": res.get("history_added")})
        except Exception as e:  # noqa: BLE001
            report.setdefault("errors", []).append(f"chat {name}: {e}")
