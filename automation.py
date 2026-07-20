"""
Scheduled automation (run by the /cron/* endpoints).

  daily_summary()      -> email the per-lane red/yellow counts (end of day).
  process_deliveries() -> pull new shipment files from Drive, read the AWB sheet
                          for delivered AWBs, and for any delivered AWB that has a
                          manifest + the project's unshipped report, generate the
                          Amazon shipment-confirmation file and email it (once).
"""
import os
import tempfile
import traceback

import config
import emailer
import shipment_core
import shipment_store
import store


def _dest(project):
    p = (project or "").upper()
    if "UAE" in p:
        return "UAE"
    if "AUS" in p:
        return "AUS"
    return ""


def _parse_filename(name):
    """Files saved by the mail script are named 'Project - AWB - original'."""
    parts = [p.strip() for p in str(name).split(" - ")]
    if len(parts) >= 3:
        return parts[0], parts[1]
    return "", ""


# ---------------------------------------------------------------------------
# Daily summary
# ---------------------------------------------------------------------------
def daily_summary(date_label=""):
    rows, totals, _refreshed = store.dashboard_rows()
    status = emailer.send_daily_summary(rows, totals, date_label)
    return {"lanes": len(rows), "red": totals.get("red_today", 0),
            "yellow": totals.get("yellow_today", 0), "email": status}


# ---------------------------------------------------------------------------
# Delivery processing
# ---------------------------------------------------------------------------
def _sync_drive(report):
    if not (config.DRIVE_FOLDER_ID and config.has_service_account()):
        return
    import drive
    for f in drive.list_data_files(config.DRIVE_FOLDER_ID):
        if shipment_store.already_ingested(f["id"]):
            continue
        try:
            data, ext = drive.download(f["id"], f["mimeType"], f["name"])
            raw = data if isinstance(data, bytes) else str(data).encode("utf-8")
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext or ".xlsx")
            tmp.write(raw)
            tmp.close()
            try:
                _h, rows = shipment_core.read_table(tmp.name)
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
            project, awb = _parse_filename(f["name"])
            shipment_store.store_manifest(awb, project, rows, f["id"], f["name"])
            report["files_pulled"] += 1
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            report["errors"].append(f"pull {f.get('name')}: {e}")


def _poll_awb_deliveries(report):
    if not (config.AWB_SHEET_ID and config.has_service_account()):
        return
    import sheets
    sh = sheets.client().open_by_key(config.AWB_SHEET_ID)
    for ws in sh.worksheets():
        title = ws.title.strip().upper()
        if any(s in title for s in ("ADMIN", "LANE CONFIG", "SHIPPED", "FBA")):
            continue
        try:
            vals = ws.get_all_values()
        except Exception:  # noqa: BLE001
            continue
        if len(vals) < 2:
            continue
        H = [h.strip().upper() for h in vals[0]]
        if "MASTER AWB" not in H or "TRACKING STATUS VIA API" not in H:
            continue
        ai, si = H.index("MASTER AWB"), H.index("TRACKING STATUS VIA API")
        pi = H.index("PROJECT NAME") if "PROJECT NAME" in H else -1
        di = H.index("DELIVERED DATE") if "DELIVERED DATE" in H else -1
        for r in vals[1:]:
            awb = str(r[ai]).strip() if ai < len(r) else ""
            status = str(r[si]).strip() if si < len(r) else ""
            if awb and "deliver" in status.lower():
                dest = _dest(r[pi] if 0 <= pi < len(r) else ws.title)
                dd = str(r[di]) if 0 <= di < len(r) else ""
                shipment_store.set_delivered(awb, dest, dd)
                report["delivered_seen"] += 1


def _process_ready(report):
    for awb, d in shipment_store.delivered().items():
        if shipment_store.get_generated(awb):
            continue  # already generated + emailed
        man = shipment_store.get_manifest(awb)
        if not man:
            report["waiting"].append({"awb": awb, "reason": "no shipment manifest yet"})
            continue
        dest = d.get("dest") or _dest(man.get("project", ""))
        uns = shipment_store.get_unshipped(dest)
        if not uns:
            report["waiting"].append({"awb": awb, "reason": f"no {dest or '?'} unshipped report"})
            continue
        text, summary = shipment_core.build_confirmation(man["rows"], set(uns["order_ids"]))
        filename = f"amazon-shipment-{(dest or 'ship')}-{awb}.txt"
        shipment_store.save_generated(awb, text, summary, filename)
        try:
            email_status = emailer.send_amazon_file(awb, man.get("project", ""), text, filename, summary)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            email_status = f"ERROR: {e}"
        report["generated"].append({
            "awb": awb, "confirmed": summary["confirmed_orders"],
            "cancelled": len(summary["cancelled_orders"]), "email": email_status})


def process_deliveries():
    report = {"files_pulled": 0, "delivered_seen": 0, "generated": [],
              "waiting": [], "errors": []}
    try:
        _sync_drive(report)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        report["errors"].append(f"drive sync: {e}")
    try:
        _poll_awb_deliveries(report)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        report["errors"].append(f"awb poll: {e}")
    _process_ready(report)
    return report
