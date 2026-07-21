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
# Daily summary — counts live from the RED / YELLOW sheets
# ---------------------------------------------------------------------------
def _count_blank_reason(vals):
    """Rows whose REASON is blank (an order still pending). A non-empty REASON =
    handled (out of stock / cancelled / refunded) -> not counted."""
    if len(vals) < 2:
        return 0
    H = [str(h).strip().lower() for h in vals[0]]
    ri = next((i for i, h in enumerate(H) if h == "reason"), None)
    oi = next((i for i, h in enumerate(H) if h == "order id"), None)
    n = 0
    for r in vals[1:]:
        reason = str(r[ri]).strip() if (ri is not None and ri < len(r)) else ""
        oid = str(r[oi]).strip() if (oi is not None and oi < len(r)) else ""
        if reason == "" and (oi is None or oid):
            n += 1
    return n


def _batch_counts(sh, lanes):
    """ONE batched read of all lane tabs -> {lane: (blank-REASON count, data-row
    count)}. Batched to avoid a read-per-tab (the Sheets per-minute read quota)."""
    ranges = [f"'{lane}'!A:Z" for lane in lanes]
    try:
        resp = sh.values_batch_get(ranges)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for lane, vr in zip(lanes, resp.get("valueRanges", [])):
        vals = vr.get("values", [])
        out[lane] = (_count_blank_reason(vals), max(0, len(vals) - 1))
    return out


def _lane_pending_counts():
    """Per-lane blank-REASON counts from the RED/YELLOW sheets. Excludes UK-
    destination lanes and UK -> USA. Also flags lanes with no report (empty tabs)
    as 'not_uploaded'. Returns rows + totals for emailer.send_daily_summary."""
    import splitter
    lanes = [l for l in config.LANES if not splitter._skip_lane(l)]
    rows, ty, tr, not_uploaded = [], 0, 0, []
    if not (config.has_service_account()
            and config.RED_SPREADSHEET_ID and config.YELLOW_SPREADSHEET_ID):
        return rows, {"yellow_today": 0, "red_today": 0, "not_uploaded": lanes}
    import sheets
    yc = _batch_counts(sheets.client().open_by_key(config.YELLOW_SPREADSHEET_ID), lanes)
    rc = _batch_counts(sheets.client().open_by_key(config.RED_SPREADSHEET_ID), lanes)
    for lane in lanes:
        y_blank, y_rows = yc.get(lane, (0, 0))
        r_blank, r_rows = rc.get(lane, (0, 0))
        rows.append({"lane": lane, "yellow_today": y_blank, "red_today": r_blank})
        ty += y_blank
        tr += r_blank
        if y_rows == 0 and r_rows == 0:      # no rows in either tab -> no report
            not_uploaded.append(lane)
    return rows, {"yellow_today": ty, "red_today": tr, "not_uploaded": not_uploaded}


def daily_summary(date_label=""):
    rows, totals = _lane_pending_counts()
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
    ingested = shipment_store.ingested_files()   # one load
    to_store = []
    for f in drive.list_data_files(config.DRIVE_FOLDER_ID):
        if f["id"] in ingested:
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
                    to_store.append({"awb": awb, "project": project, "rows": grp,
                                     "file_id": f["id"], "source_name": f["name"]})
                report["awbs_ingested"] = report.get("awbs_ingested", 0) + len(groups)
            else:
                # no AWB column — fall back to the filename AWB so nothing is lost
                to_store.append({"awb": fname_awb, "project": project, "rows": rows,
                                 "file_id": f["id"], "source_name": f["name"]})
            report["files_pulled"] += 1
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            report["errors"].append(f"pull {f.get('name')}: {e}")
    if to_store:
        shipment_store.store_manifests_bulk(to_store)   # one save


def _awb_index():
    """Read the AWB sheet ONCE -> {awb: {status, tab, delivered(bool), delivered_date}}.
    Skips ADMIN / LANE CONFIG / SHIPPED / FBA tabs and repeated 'MASTER AWB' header
    rows. Shared by delivery-polling and the shipment log so we read the sheet once."""
    idx, checks = {}, {}
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
        # columns for the SYSTEM UPDATE check (may not exist on every tab)
        aui = next((i for i, h in enumerate(H) if "AMAZON TRACKING ID UPDATED" in h), -1)
        mi = next((i for i, h in enumerate(H) if "MATCH" in h), -1)
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
            if not (prev and prev.get("delivered") and not delivered):
                idx[awb] = {"status": status, "tab": ws.title, "project": proj,
                            "delivered": delivered,
                            "delivered_date": dd or (prev or {}).get("delivered_date", "")}
            # aggregate the per-row tracking check across ALL rows of this AWB
            au_ok = bool(_norm(r[aui])) if 0 <= aui < len(r) else False
            mval = _norm(r[mi]).upper() if 0 <= mi < len(r) else ""
            m_ok = "MATCH" in mval and "UNMATCH" not in mval
            c = checks.setdefault(awb, {"au": True, "match": True, "n": 0, "cols": False})
            c["n"] += 1
            c["cols"] = c["cols"] or (aui >= 0 and mi >= 0)
            c["au"] = c["au"] and au_ok
            c["match"] = c["match"] and m_ok
    for awb, c in checks.items():
        if awb in idx:
            idx[awb]["amazon_updated_all"] = bool(c["cols"] and c["n"] and c["au"])
            idx[awb]["tracking_match_all"] = bool(c["cols"] and c["n"] and c["match"])
    return idx


def _poll_awb_deliveries(report, idx):
    entries = []
    for awb, info in idx.items():
        if info.get("delivered"):
            # prefer an explicit PROJECT NAME cell, fall back to the tab name
            dest = _dest(info.get("project") or info.get("tab") or "")
            entries.append((awb, dest, info.get("delivered_date", "")))
    if entries:
        shipment_store.set_delivered_bulk(entries)   # one save, not one per AWB
    report["delivered_seen"] += len(entries)


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


def _log_records(idx, actions=None):
    """Build one SHIPMENT LOGS row per stored manifest, enriched with the AWB
    sheet's status/route and whether the Amazon file has been generated. `actions`
    is {awb: action-cell} from the log sheet — SYSTEM UPDATE is computed only for
    AWBs whose action is 'UPDATED'. IND -> USA shipments are excluded."""
    actions = actions or {}
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
        project = info.get("tab") or m.get("project") or ""
        # per ops: don't log IND -> USA shipments
        if project.upper().replace("→", "TO").replace(" ", "") in ("INDTOUSA", "INDIATOUSA"):
            continue
        if shipment_store.get_generated(awb):
            update_status = "Amazon file generated"
        elif info.get("delivered"):
            update_status = "Delivered — processing"
        else:
            update_status = "Awaiting delivery"
        # SYSTEM UPDATE: only once ops marks 'action' = UPDATED. YES when the AWB
        # sheet shows Amazon-tracking updated AND tracking MATCH on all its rows.
        system_update = ""
        if (actions.get(awb, "") or "").strip().upper() == "UPDATED":
            system_update = "YES" if (info.get("amazon_updated_all")
                                      and info.get("tracking_match_all")) else "NO"
        records.append({
            "project": project,
            "AWB SHIP DATE": awb_ship,
            "awb": awb,
            "number of orders": n_orders,
            "awb status": info.get("status", ""),
            "last ship date": last_ship,
            "shipment update status": update_status,
            "SYSTEM UPDATE": system_update,
        })
    return records


def _update_shipment_log(report, idx):
    if not (config.SHIPMENT_LOG_SHEET_ID and config.has_service_account()):
        return
    import shipment_log
    actions = shipment_log.current_actions()
    updated, added = shipment_log.upsert(_log_records(idx, actions))
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
