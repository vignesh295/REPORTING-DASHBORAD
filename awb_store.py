"""
Local store for AWB batches (one AWB = one batch of orders).

Ingested from the AWB file (via the Drive Apps Script -> POST /api/awb/ingest)
and marked delivered by the status-sheet Apps Script (-> POST /api/awb/status).
Rows share an AWB number; each AWB groups its order rows.

Status is derived from the order ship dates + the delivered flag:
  scheduled -> first ship date is still in the future
  awaiting  -> first ship date has arrived, not delivered yet (being watched)
  late      -> awaiting and overdue by more than config.AWB_LATE_DAYS
  delivered -> the status sheet reported it delivered (terminal, "done")
"""
import datetime
import json
import os

import config

# ---- column aliases (Apps Script may send any of these) -------------------
_ALIASES = {
    "awb": ["awb", "AWB", "awb_no", "AWB NO", "awbNo"],
    "creation_date": ["creation_date", "Creation Date", "CREATION DATE", "creationDate"],
    "order_id": ["order_id", "Order ID", "ORDER ID", "orderId", "order id"],
    "qty": ["qty", "Qty", "QTY", "quantity"],
    "last_ship_date": ["last_ship_date", "Last Ship Date", "LAST SHIP DATE", "lastShipDate", "ship_date"],
    "tracking_id": ["tracking_id", "Tracking ID", "TRACKING ID", "trackingId", "tracking id"],
    "carrier": ["carrier", "Carrier", "CARRIER"],
}


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _today():
    return datetime.date.today()


def _norm(v):
    return str(v).strip() if v is not None else ""


def _pick(row, field):
    for key in _ALIASES[field]:
        if key in row and _norm(row[key]):
            return _norm(row[key])
    return ""


def _parse_date(s):
    s = _norm(s)
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.datetime.fromisoformat(s).date()
    except ValueError:
        return None


def _load():
    if not os.path.exists(config.AWB_STORE_FILE):
        return {"awbs": {}}
    try:
        with open(config.AWB_STORE_FILE) as f:
            data = json.load(f)
            data.setdefault("awbs", {})
            return data
    except (OSError, json.JSONDecodeError):
        return {"awbs": {}}


def _save(data):
    with open(config.AWB_STORE_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _first_ship(rec):
    dates = [_parse_date(o.get("last_ship_date")) for o in rec.get("orders", [])]
    dates = [d for d in dates if d]
    return min(dates) if dates else None


def _status(rec):
    """Return (status, days) — days = days since first ship (awaiting/late),
    days-until (scheduled), or None/0."""
    if rec.get("delivered"):
        return "delivered", 0
    first = _first_ship(rec)
    if first is None:
        return "awaiting", None
    today = _today()
    if first > today:
        return "scheduled", (first - today).days
    days = (today - first).days
    if days > config.AWB_LATE_DAYS:
        return "late", days
    return "awaiting", days


def ingest_rows(rows):
    """Upsert AWB rows (grouped by AWB number). Returns (awbs_touched, orders_added)."""
    data = _load()
    awbs = data["awbs"]
    touched, added = set(), 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        awb = _pick(row, "awb")
        if not awb:
            continue
        touched.add(awb)
        rec = awbs.setdefault(awb, {
            "awb": awb, "creation_date": "", "carrier": "", "tracking_id": "",
            "orders": [], "delivered": False, "delivered_at": None,
            "created_at": _now(), "updated_at": _now(),
        })
        for field in ("creation_date", "carrier", "tracking_id"):
            val = _pick(row, field)
            if val:
                rec[field] = val
        order_id = _pick(row, "order_id")
        if order_id:
            qty = _pick(row, "qty")
            ship = _pick(row, "last_ship_date")
            existing = next((o for o in rec["orders"] if o["order_id"] == order_id), None)
            if existing:
                existing["qty"] = qty or existing.get("qty", "")
                existing["last_ship_date"] = ship or existing.get("last_ship_date", "")
            else:
                rec["orders"].append({"order_id": order_id, "qty": qty, "last_ship_date": ship})
                added += 1
        rec["updated_at"] = _now()
    _save(data)
    return len(touched), added


def mark_delivered(awb=None, tracking_id=None, when=None):
    """Mark an AWB delivered, matched by AWB number (preferred) or tracking ID.
    Returns the AWB number on success, else None."""
    data = _load()
    awbs = data["awbs"]
    target_key = None
    if awb and _norm(awb) in awbs:
        target_key = _norm(awb)
    elif tracking_id:
        t = _norm(tracking_id)
        target_key = next((k for k, a in awbs.items() if _norm(a.get("tracking_id")) == t), None)
    if target_key is None:
        return None
    rec = awbs[target_key]
    rec["delivered"] = True
    rec["delivered_at"] = _norm(when) or _now()
    rec["updated_at"] = _now()
    _save(data)
    return target_key


def _decorate(rec):
    status, days = _status(rec)
    first = _first_ship(rec)
    qty_total = sum(int(o["qty"]) for o in rec.get("orders", []) if str(o.get("qty", "")).strip().isdigit())
    return {
        **rec,
        "status": status,
        "days": days,
        "order_count": len(rec.get("orders", [])),
        "qty_total": qty_total,
        "first_ship": first.strftime("%d.%m.%Y") if first else "",
    }


_SORT = {"late": 0, "awaiting": 1, "scheduled": 2, "delivered": 3}


def rows(status_filter="active"):
    """Decorated AWB rows. status_filter: all | active | awaiting | late | scheduled | delivered."""
    data = _load()
    out = [_decorate(rec) for rec in data["awbs"].values()]
    out.sort(key=lambda r: (_SORT.get(r["status"], 9), r.get("first_ship") or "", r["awb"]))
    if status_filter == "active":
        out = [r for r in out if r["status"] in ("late", "awaiting", "scheduled")]
    elif status_filter != "all":
        out = [r for r in out if r["status"] == status_filter]
    return out


def summary():
    data = _load()
    counts = {"total": 0, "late": 0, "awaiting": 0, "scheduled": 0, "delivered": 0}
    for rec in data["awbs"].values():
        status, _ = _status(rec)
        counts["total"] += 1
        counts[status] = counts.get(status, 0) + 1
    counts["active"] = counts["late"] + counts["awaiting"] + counts["scheduled"]
    return counts


def get(awb):
    rec = _load()["awbs"].get(_norm(awb))
    return _decorate(rec) if rec else None
