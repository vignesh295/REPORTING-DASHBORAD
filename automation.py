"""
Scheduled automation (run by the /cron/* endpoints).

  daily_summary()      -> email the per-lane red/yellow counts (end of day).
  process_deliveries() -> pull new shipment files from Drive, read the AWB sheet
                          for delivered AWBs, and for any delivered AWB that has a
                          manifest + the project's unshipped report, generate the
                          Amazon shipment-confirmation file and email it (once).
"""
import datetime
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


def _norm(v):
    """Trim whitespace and stray trailing commas the AWB sheet/manifests carry."""
    return str(v).strip().strip(",").strip()


def _awb_column(headers):
    """The header that holds the batch AWB in a manifest (case-insensitive)."""
    for h in headers:
        if str(h).strip().upper() == "AWB":
            return h
    return None


def _group_by_awb(headers, rows):
    """Group manifest rows by their AWB-column value. Returns {awb: [rows]}.
    Filenames don't reliably carry the AWB, but every manifest row does — and a
    single file can hold several batches, so we split by the real column."""
    col = _awb_column(headers)
    if not col:
        return {}
    groups = {}
    for r in rows:
        a = _norm(r.get(col, ""))
        if not a or a.upper() in ("AWB", "MASTER AWB"):
            continue
        groups.setdefault(a, []).append(r)
    return groups


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
                headers, rows = shipment_core.read_table(tmp.name)
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
            project, fname_awb = _parse_filename(f["name"])
            groups = _group_by_awb(headers, rows)
            if groups:
                # one manifest per real AWB in the file (a file may hold several)
                for awb, grp in groups.items():
                    shipment_store.store_manifest(awb, project, grp, f["id"], f["name"])
                report["awbs_ingested"] = report.get("awbs_ingested", 0) + len(groups)
            else:
                # no AWB column — fall back to the filename AWB so nothing is lost
                shipment_store.store_manifest(fname_awb, project, rows, f["id"], f["name"])
            report["files_pulled"] += 1
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            report["errors"].append(f"pull {f.get('name')}: {e}")


def _awb_index():
    """Read the AWB sheet ONCE -> {awb: {status, tab, delivered(bool), delivered_date}}.
    Skips ADMIN / LANE CONFIG / SHIPPED / FBA tabs and repeated 'MASTER AWB' header
    rows. Shared by delivery-polling and the shipment log so we read the sheet once."""
    idx = {}
    if not (config.AWB_SHEET_ID and config.has_service_account()):
        return idx
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
            awb = _norm(r[ai]) if ai < len(r) else ""
            if not awb or awb.upper() == "MASTER AWB":  # skip blanks + repeated headers
                continue
            status = _norm(r[si]) if si < len(r) else ""
            dd = _norm(r[di]) if 0 <= di < len(r) else ""
            proj = _norm(r[pi]) if 0 <= pi < len(r) else ""
            delivered = "deliver" in status.lower()
            prev = idx.get(awb)
            # delivery is monotonic: never let a later non-delivered row for the
            # same AWB (e.g. a duplicate in another tab) overwrite a delivered one.
            if prev and prev.get("delivered") and not delivered:
                continue
            idx[awb] = {"status": status, "tab": ws.title, "project": proj,
                        "delivered": delivered,
                        "delivered_date": dd or (prev or {}).get("delivered_date", "")}
    return idx


def _poll_awb_deliveries(report, idx):
    for awb, info in idx.items():
        if info.get("delivered"):
            # prefer an explicit PROJECT NAME cell, fall back to the tab name
            dest = _dest(info.get("project") or info.get("tab") or "")
            shipment_store.set_delivered(awb, dest, info.get("delivered_date", ""))
            report["delivered_seen"] += 1


# ---------------------------------------------------------------------------
# Shipment log sheet (one row per AWB)
# ---------------------------------------------------------------------------
def _row_get(row, *names):
    low = {str(k).strip().lower(): v for k, v in row.items()}
    for n in names:
        if n.lower() in low:
            return _norm(low[n.lower()])
    return ""


_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y")


def _parse_date(s):
    """Parse the assorted date strings the manifests carry (ISO, dd.mm.yyyy) to a
    date for comparison. Returns None for blanks / '0' / unrecognised formats."""
    s = str(s).strip()
    if not s or s in ("0", "0.0"):
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _log_records(idx):
    """Build one SHIPMENT LOGS row per stored manifest, enriched with the AWB
    sheet's status/route and whether the Amazon file has been generated."""
    records = []
    for awb, m in shipment_store.manifests().items():
        rows = m.get("rows", [])
        oids = {_row_get(r, "ORDER ID", "order-id") for r in rows}
        oids.discard("")
        n_orders = len(oids) or len(rows)
        # AWB SHIP DATE = the batch's AWB CREATION DATE (same on every row)
        awb_ship = ""
        for r in rows:
            v = _row_get(r, "AWB CREATION DATE", "awb creation date")
            if v and v not in ("0", "0.0"):
                awb_ship = v
                break
        # last ship date = the EARLIEST last-ship-date across the batch's orders
        dated = [(d, v) for r in rows
                 for v in [_row_get(r, "LAST SHIP DATE", "last ship date", "last-ship-date")]
                 for d in [_parse_date(v)] if d]
        if dated:
            last_ship = min(dated, key=lambda x: x[0])[1]
        else:
            last_ship = ""
            for r in rows:
                v = _row_get(r, "LAST SHIP DATE", "last ship date", "last-ship-date")
                if v and v not in ("0", "0.0"):
                    last_ship = v
                    break
        info = idx.get(awb, {})
        if shipment_store.get_generated(awb):
            update_status = "Amazon file generated"
        elif info.get("delivered"):
            update_status = "Delivered — processing"
        else:
            update_status = "Awaiting delivery"
        records.append({
            "project": info.get("tab") or m.get("project") or "",
            "AWB SHIP DATE": awb_ship,
            "awb": awb,
            "number of orders": n_orders,
            "awb status": info.get("status", ""),
            "last ship date": last_ship,
            "shipment update status": update_status,
        })
    return records


def _update_shipment_log(report, idx):
    if not (config.SHIPMENT_LOG_SHEET_ID and config.has_service_account()):
        return
    import shipment_log
    updated, added = shipment_log.upsert(_log_records(idx))
    report["log_updated"] = updated
    report["log_added"] = added


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
    report = {"files_pulled": 0, "awbs_ingested": 0, "delivered_seen": 0,
              "log_updated": 0, "log_added": 0,
              "generated": [], "waiting": [], "errors": []}
    try:
        _sync_drive(report)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        report["errors"].append(f"drive sync: {e}")
    idx = {}
    try:
        idx = _awb_index()
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        report["errors"].append(f"awb index: {e}")
    try:
        _poll_awb_deliveries(report, idx)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        report["errors"].append(f"awb poll: {e}")
    _process_ready(report)
    try:
        _update_shipment_log(report, idx)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        report["errors"].append(f"shipment log: {e}")
    return report
