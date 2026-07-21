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
import re

import openpyxl

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
