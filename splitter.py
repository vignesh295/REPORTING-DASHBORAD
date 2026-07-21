"""
Red/Yellow splitter — turn a per-lane order report into RED (overdue) and
YELLOW (due-today) rows for the RED/YELLOW sheets.

Source: the report's "SHIPPING QUEUE" tab. The "SHIP STATUS" column classifies
each order — OVERDUE -> red, TODAY -> yellow, UPCOMING -> skip. The lane comes
from the file name. Column mapping to the output sheets:

  ORDER DATE <- DATE        ISBN  <- SKU         LAST SHIP DATE   <- LAST SHIP DATE
  ORDER ID   <- ORDER ID    TITLE <- TITLE NAME  ACTUAL SHIP DATE <- ACTUAL SHIP DATE
  QTY        <- QTY         REASON <- STATUS     LATE BY (red)    <- DAYS (+LEFT / -LATE)
"""
import os
import re

import openpyxl

import config
import sheets
from shipment_core import _clean

SOURCE_TAB = "SHIPPING QUEUE"
_ORIGIN = {"IND": "India", "INDIA": "India", "USA": "USA", "UK": "UK"}


def lane_from_filename(name, lanes):
    """Match 'ORIGIN TO DEST' in the file name to one of the configured lanes."""
    m = re.search(r"([A-Za-z]+)\s+TO\s+([A-Za-z]+)", name, re.I)
    if not m:
        return None
    origin = _ORIGIN.get(m.group(1).upper(), m.group(1).title())
    target = f"{origin} → {m.group(2).upper()}".replace(" ", "").upper()
    for lane in lanes:
        if lane.replace(" ", "").upper() == target:
            return lane
    return None


def _read_tab(path, tab):
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        real = next((s for s in wb.sheetnames if s.strip().upper() == tab.strip().upper()), None)
        if real is None:
            return [], []
        grid = [list(r) for r in wb[real].iter_rows(values_only=True)]
    finally:
        wb.close()
    if not grid:
        return [], []
    headers = [_clean(c) for c in grid[0]]
    rows = []
    for raw in grid[1:]:
        if not any(_clean(c) for c in raw):
            continue
        rows.append({headers[i]: _clean(raw[i]) for i in range(min(len(headers), len(raw)))})
    return headers, rows


def _get(row, *names):
    low = {str(k).strip().lower(): v for k, v in row.items()}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return ""


def split_order_report(path, tab=SOURCE_TAB):
    """Return (red_rows, yellow_rows) as dicts keyed by the output column names.
    OVERDUE -> red, TODAY -> yellow (by the SHIP STATUS column); UPCOMING skipped."""
    _headers, rows = _read_tab(path, tab)
    red, yellow = [], []
    for r in rows:
        if _get(r, "ORDER ID").strip().upper() == "ORDER ID":  # repeated header row
            continue
        ship_status = _get(r, "SHIP STATUS").upper()
        out = {
            "ORDER DATE": _get(r, "DATE", "ORDER DATE"),
            "ORDER ID": _get(r, "ORDER ID"),
            "ISBN": _get(r, "SKU", "ISBN"),
            "TITLE": _get(r, "TITLE NAME", "TITLE"),
            "QTY": _get(r, "QTY"),
            "LAST SHIP DATE": _get(r, "LAST SHIP DATE"),
            "ACTUAL SHIP DATE": _get(r, "ACTUAL SHIP DATE"),
            "REASON": _get(r, "STATUS"),
        }
        if "OVERDUE" in ship_status:
            out["LATE BY"] = _get(r, "DAYS (+LEFT / -LATE)", "DAYS", "LATE BY")
            red.append(out)
        elif "TODAY" in ship_status:
            yellow.append(out)
    return red, yellow


# ---------------------------------------------------------------------------
# Writing the split into the RED / YELLOW sheets (one tab per lane)
# ---------------------------------------------------------------------------
def _cells(row, header):
    """Map a split-row dict to a cell list aligned to the tab's header order
    (header names matched case-/space-insensitively)."""
    rn = {str(k).strip().lower(): v for k, v in row.items()}
    return ["" if rn.get(h.strip().lower()) is None else str(rn.get(h.strip().lower(), ""))
            for h in header]


def _replace_tab(sh, title, rows):
    """Clear a lane tab's data and rewrite it (header preserved)."""
    ws = sh.worksheet(title)
    header = ws.row_values(1)
    if not header:
        return 0
    ws.clear()
    ws.update([header] + [_cells(r, header) for r in rows], "A1", raw=True)
    return len(rows)


def _accumulate_tab(sh, title, rows, id_key="ORDER ID"):
    """Append rows into a history tab, skipping Order IDs already present."""
    ws = sh.worksheet(title)
    existing = ws.get_all_values()
    if not existing:
        return 0
    header = existing[0]
    idi = next((i for i, h in enumerate(header) if h.strip().lower() == id_key.strip().lower()), None)
    seen = set()
    if idi is not None:
        for r in existing[1:]:
            if idi < len(r) and str(r[idi]).strip():
                seen.add(str(r[idi]).strip())
    new = []
    for r in rows:
        oid = str(r.get(id_key, "")).strip()
        if oid and oid in seen:
            continue
        if oid:
            seen.add(oid)
        new.append(_cells(r, header))
    if new:
        ws.append_rows(new, value_input_option="RAW")
    return len(new)


def write_split(lane, red_rows, yellow_rows, red_id=None, yellow_id=None,
                history_suffix=" — Previous"):
    """Write a lane's split: refresh the current RED + YELLOW lane tabs and
    accumulate the RED rows into the lane's history tab (dedup by Order ID)."""
    shr = sheets.client().open_by_key(red_id or config.RED_SPREADSHEET_ID)
    shy = sheets.client().open_by_key(yellow_id or config.YELLOW_SPREADSHEET_ID)
    res = {"lane": lane,
           "yellow": _replace_tab(shy, lane, yellow_rows),
           "red": _replace_tab(shr, lane, red_rows)}
    try:
        res["history_added"] = _accumulate_tab(shr, lane + history_suffix, red_rows)
    except Exception:  # history tab missing -> skip, don't fail the write
        res["history_added"] = 0
    return res


def process_order_report(path, lanes=None):
    """Detect the lane from the file name, split it, and write to the sheets."""
    lanes = lanes or config.LANES
    lane = lane_from_filename(os.path.basename(path), lanes)
    if not lane:
        return {"error": f"no lane detected in {os.path.basename(path)!r}"}
    red, yellow = split_order_report(path)
    return write_split(lane, red, yellow)
