"""
Write the "SHIPMENT LOGS" Google Sheet — one row per AWB batch.

Columns: project, awb, number of orders, awb status, last ship date,
         shipment update status, link.

Upserts by AWB (case-/space-insensitive header match), so re-running updates the
existing row in place instead of appending duplicates.
"""
import config
import sheets

HEADERS = ["project", "AWB SHIP DATE", "awb", "number of orders", "awb status",
           "last ship date", "shipment update status", "link"]


def _nh(h):
    return str(h).strip().lower()


def _col_letter(n):
    """1-based column index -> spreadsheet letter (1->A, 7->G, 27->AA)."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _worksheet():
    return sheets.client().open_by_key(config.SHIPMENT_LOG_SHEET_ID).sheet1


def upsert(records):
    """records: list of dicts keyed by HEADERS names. New AWBs are appended,
    existing AWBs updated in place. Returns (updated, added)."""
    if not (config.SHIPMENT_LOG_SHEET_ID and config.has_service_account()):
        return 0, 0

    # De-dupe incoming by awb (last wins) so one call never double-appends.
    by_awb = {}
    for rec in records:
        awb = str(rec.get("awb", "")).strip()
        if awb:
            by_awb[awb] = rec

    ws = _worksheet()
    values = ws.get_all_values()

    if not values or not any(_nh(c) for c in values[0]):
        ws.update([HEADERS], "A1", raw=True)
        header, values = list(HEADERS), [list(HEADERS)]
    else:
        header = values[0]

    hmap = {_nh(h): i for i, h in enumerate(header)}
    if "awb" not in hmap:                      # header row is wrong -> reset it
        ws.update([HEADERS], "A1", raw=True)
        header, values = list(HEADERS), [list(HEADERS)]
        hmap = {_nh(h): i for i, h in enumerate(header)}

    awb_col = hmap["awb"]
    row_of = {}
    for i, row in enumerate(values[1:], start=2):
        a = row[awb_col].strip() if awb_col < len(row) else ""
        if a and a not in row_of:
            row_of[a] = i

    ncols = len(header)
    end = _col_letter(ncols)
    hkeys = [_nh(h) for h in header]

    def build_row(rec, existing):
        # Write only the columns this record manages; keep every other column
        # (a user-added 'action', or 'link' until we fill it) exactly as it was.
        rn = {_nh(k): v for k, v in rec.items()}
        base = list(existing) + [""] * ncols
        out = []
        for i, h in enumerate(hkeys):
            if h in rn:
                v = rn[h]
                out.append("" if v is None else str(v))
            else:
                out.append(base[i])
        return out

    updates, appends = [], []
    for awb, rec in by_awb.items():
        if awb in row_of:
            rn = row_of[awb]
            existing = values[rn - 1] if rn - 1 < len(values) else []
            updates.append({"range": f"A{rn}:{end}{rn}", "values": [build_row(rec, existing)]})
        else:
            appends.append(build_row(rec, []))

    if updates:
        ws.batch_update(updates, value_input_option="RAW")
    if appends:
        ws.append_rows(appends, value_input_option="RAW")
    return len(updates), len(appends)
