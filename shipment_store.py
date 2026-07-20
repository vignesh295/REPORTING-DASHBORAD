"""
Local store for the shipment workflow (JSON file).

Holds:
  * manifests  — per AWB: the shipment rows (order-id, tracking, ship-date, …)
  * unshipped  — per project (UAE/AUS): the latest unshipped-orders report's order-ids
  * files      — Drive file-ids already ingested (so we never double-process)
  * generated  — per AWB: the last Amazon file we built (text + summary)
"""
import datetime
import json
import os

import config
import db

_KEY = "shipment"
_DEFAULT = {"manifests": {}, "unshipped": {}, "files": [], "generated": {}, "delivered": {}}


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _defaults(data):
    data.setdefault("manifests", {})
    data.setdefault("unshipped", {})
    data.setdefault("files", [])
    data.setdefault("generated", {})
    data.setdefault("delivered", {})
    return data


def _load():
    if db.enabled():
        return _defaults(db.kv_get(_KEY, dict(_DEFAULT)) or dict(_DEFAULT))
    if not os.path.exists(config.SHIPMENT_STORE_FILE):
        return dict(_DEFAULT)
    try:
        with open(config.SHIPMENT_STORE_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    return _defaults(data)


def _save(data):
    if db.enabled():
        db.kv_set(_KEY, data)
        return
    # Serialise fully first: if anything is non-serialisable this raises BEFORE we
    # open/truncate the file, so a bad value can never half-write and corrupt it.
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    with open(config.SHIPMENT_STORE_FILE, "w") as f:
        f.write(payload)


# ---- Drive-file dedup ------------------------------------------------------
def already_ingested(file_id):
    return file_id in _load().get("files", [])


# ---- Manifests (per AWB) ---------------------------------------------------
def store_manifest(awb, project, rows, file_id=None, source_name=""):
    """Save/replace an AWB's shipment manifest rows. Keyed by AWB when we have
    one, else by the source file name so nothing is lost."""
    data = _load()
    key = str(awb).strip() or f"file:{source_name or file_id}"
    data["manifests"][key] = {
        "awb": str(awb).strip(),
        "project": str(project).strip(),
        "source": source_name,
        "received": _now(),
        "rows": rows,
    }
    if file_id and file_id not in data["files"]:
        data["files"].append(file_id)
    _save(data)
    return key, len(rows)


def get_manifest(key):
    return _load()["manifests"].get(key)


def manifests():
    return _load()["manifests"]


# ---- Unshipped report (per project) ----------------------------------------
def set_unshipped(project, order_ids, source_name=""):
    data = _load()
    data["unshipped"][str(project).strip().upper()] = {
        "order_ids": sorted(order_ids),
        "count": len(order_ids),
        "source": source_name,
        "uploaded": _now(),
    }
    _save(data)


def get_unshipped(project):
    return _load()["unshipped"].get(str(project).strip().upper())


def unshipped_projects():
    return _load()["unshipped"]


# ---- Generated Amazon files (per AWB) --------------------------------------
def save_generated(key, file_text, summary, filename):
    data = _load()
    data["generated"][key] = {
        "filename": filename,
        "text": file_text,
        "summary": summary,
        "created": _now(),
    }
    _save(data)


def get_generated(key):
    return _load()["generated"].get(key)


# ---- Delivered AWBs (from the AWB sheet poll) ------------------------------
def set_delivered(awb, dest="", delivered_date=""):
    awb = str(awb).strip()
    if not awb:
        return
    data = _load()
    rec = data["delivered"].get(awb, {})
    rec.update({"dest": dest or rec.get("dest", ""),
                "delivered_date": delivered_date or rec.get("delivered_date", ""),
                "seen": _now()})
    data["delivered"][awb] = rec
    _save(data)


def delivered():
    return _load()["delivered"]
