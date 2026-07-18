"""
Google Sheets access via a service account (gspread).

Layout:
  RED spreadsheet    -> per lane: "<lane>" (today's overdue, replaced each upload)
                        AND "<lane> — All Red" (running history, one row per order)
  YELLOW spreadsheet -> one tab per lane (TODAY orders, replaced each upload)

Writing a lane "replaces" that lane's tab by default (clear + write), which is
why the yellow list only lives until the next update. The All-Red history tab is
the exception: it accumulates, deduped by Order ID, so overdue orders persist.
"""
import gspread
from google.oauth2.service_account import Credentials

import config


def _order_id_index(headers):
    """Position of the ORDER ID column within a data row (fallback: 1)."""
    try:
        return headers.index("ORDER ID")
    except ValueError:
        return 1


def merge_red_history(existing_values, new_rows, headers, flagged_date):
    """
    Pure planner for the All-Red history tab — no I/O, so it's easy to test.

    Dedupes by Order ID: an order that is already on record keeps its original
    row (and its original DATE FLAGGED); only orders not seen before are added,
    stamped with `flagged_date`.

    Args:
      existing_values: rows already in the tab as returned by gspread
                       (`get_all_values()`), including the header row; [] if empty.
      new_rows:        today's red rows (each len == len(headers)).
      headers:         the 6 column labels (without DATE FLAGGED).
      flagged_date:    string date to stamp on newly-added orders.

    Returns a dict:
      header       -> headers + [DATE FLAGGED]
      init         -> True if the tab is empty and must be initialised with a header
      append       -> the brand-new rows to append (each = row + [flagged_date])
      total_after  -> data-row count once these are written
      added        -> how many new orders are being added
    """
    oid = _order_id_index(headers)
    hist_header = list(headers) + [config.DATE_FLAGGED_HEADER]

    data_rows = existing_values[1:] if existing_values else []
    seen = set()
    for row in data_rows:
        if len(row) > oid and str(row[oid]).strip():
            seen.add(str(row[oid]).strip())

    append = []
    for row in new_rows:
        if len(row) <= oid:
            continue
        order_id = str(row[oid]).strip()
        if not order_id or order_id in seen:
            continue
        seen.add(order_id)
        append.append(list(row) + [flagged_date])

    init = len(existing_values) == 0
    existing_data_count = len(data_rows)
    return {
        "header": hist_header,
        "init": init,
        "append": append,
        "total_after": existing_data_count + len(append),
        "added": len(append),
    }

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_client = None


def client():
    global _client
    if _client is None:
        info = config.service_account_info()
        if info is not None:
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file(
                config.SERVICE_ACCOUNT_FILE, scopes=SCOPES
            )
        _client = gspread.authorize(creds)
    return _client


def _get_or_create_ws(sh, title, rows=1000, cols=len(config.COLUMN_MAP)):
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=max(cols, 6))


def ensure_lane_tabs(spreadsheet_id):
    """Make sure every lane has a tab. Returns the opened spreadsheet."""
    sh = client().open_by_key(spreadsheet_id)
    existing = {ws.title for ws in sh.worksheets()}
    for lane in config.LANES:
        if lane not in existing:
            _get_or_create_ws(sh, lane)
    return sh


def write_lane(spreadsheet_id, lane, headers, rows, mode="replace"):
    """
    Write `rows` (list of lists) to the `lane` tab.
      replace -> clear the tab, then write header + rows
      append  -> keep existing content, add rows underneath (header written once)
    Uses RAW input so ISBNs / order IDs stay as text (no scientific notation).
    """
    sh = client().open_by_key(spreadsheet_id)
    ws = _get_or_create_ws(sh, lane)

    if mode == "append":
        existing = ws.get_all_values()
        if not existing:
            ws.update([headers] + rows, "A1", raw=True)
        elif rows:
            ws.append_rows(rows, value_input_option="RAW")
    else:  # replace (default)
        ws.clear()
        ws.update([headers] + rows, "A1", raw=True)

    return len(rows)


def accumulate_red_history(spreadsheet_id, lane, headers, rows, flagged_date):
    """
    Append today's red rows into the lane's "All Red" history tab, deduped by
    Order ID (one row per order ever overdue), stamping new orders with
    `flagged_date` in a DATE FLAGGED column.

    Returns (total_after, added): the tab's data-row count after writing, and how
    many brand-new orders were added this upload.
    """
    title = lane + config.RED_ALL_SUFFIX
    sh = client().open_by_key(spreadsheet_id)
    ws = _get_or_create_ws(sh, title, cols=len(headers) + 1)

    existing = ws.get_all_values()
    plan = merge_red_history(existing, rows, headers, flagged_date)

    if plan["init"]:
        ws.clear()
        ws.update([plan["header"]] + plan["append"], "A1", raw=True)
    elif plan["append"]:
        ws.append_rows(plan["append"], value_input_option="RAW")

    return plan["total_after"], plan["added"]


def lane_counts(spreadsheet_id):
    """Return {lane: number_of_data_rows} for every lane (header excluded)."""
    sh = client().open_by_key(spreadsheet_id)
    by_title = {ws.title: ws for ws in sh.worksheets()}
    counts = {}
    for lane in config.LANES:
        ws = by_title.get(lane)
        if ws is None:
            counts[lane] = 0
            continue
        values = ws.get_all_values()
        counts[lane] = max(0, len(values) - 1)  # drop the header row
    return counts


def all_red_counts(spreadsheet_id):
    """Return {lane: rows in the lane's 'All Red' history tab} (header excluded)."""
    sh = client().open_by_key(spreadsheet_id)
    by_title = {ws.title: ws for ws in sh.worksheets()}
    counts = {}
    for lane in config.LANES:
        ws = by_title.get(lane + config.RED_ALL_SUFFIX)
        counts[lane] = max(0, len(ws.get_all_values()) - 1) if ws else 0
    return counts


def read_tab(spreadsheet_id, title):
    """Return (headers, rows) for a tab, or ([], []) if it doesn't exist / is empty."""
    sh = client().open_by_key(spreadsheet_id)
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return [], []
    values = ws.get_all_values()
    if not values:
        return [], []
    return values[0], values[1:]
