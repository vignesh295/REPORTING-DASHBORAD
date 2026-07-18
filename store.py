"""
Local per-lane dashboard state (a small JSON file).

The dashboard reads this so it loads instantly and works even before Google
Sheets is connected. It's written on every upload, and can be reconciled against
the live sheets with the "refresh from Sheets" action.
"""
import datetime
import json
import os

import config


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _load():
    if not os.path.exists(config.STATE_FILE):
        return {"lanes": {}}
    try:
        with open(config.STATE_FILE) as f:
            data = json.load(f)
            data.setdefault("lanes", {})
            return data
    except (OSError, json.JSONDecodeError):
        return {"lanes": {}}


def _save(data):
    with open(config.STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def record_upload(lane, red_today, yellow_today, red_all_time, uploaded_by):
    """Store the latest counts for a lane after an upload, with who + when."""
    data = _load()
    data["lanes"][lane] = {
        "red_today": red_today,
        "yellow_today": yellow_today,
        "red_all_time": red_all_time,
        "last_uploaded_at": _now(),
        "last_uploaded_by": uploaded_by,
    }
    _save(data)


def reconcile_from_sheets(red_counts, yellow_counts, red_all_counts=None):
    """Overwrite today's counts (and optionally all-time) from the live sheets."""
    data = _load()
    for lane in config.LANES:
        rec = data["lanes"].setdefault(lane, {})
        rec["red_today"] = red_counts.get(lane, 0)
        rec["yellow_today"] = yellow_counts.get(lane, 0)
        if red_all_counts is not None:
            rec["red_all_time"] = red_all_counts.get(lane, 0)
    data["refreshed_at"] = _now()
    _save(data)


def get_lane(lane):
    """The stored record for one lane (with zero/None defaults)."""
    rec = _load().get("lanes", {}).get(lane, {})
    return {
        "red_today": rec.get("red_today", 0),
        "yellow_today": rec.get("yellow_today", 0),
        "red_all_time": rec.get("red_all_time", 0),
        "last_uploaded_at": rec.get("last_uploaded_at"),
        "last_uploaded_by": rec.get("last_uploaded_by"),
    }


def dashboard_rows():
    """Return (rows, totals, refreshed_at) for every configured lane."""
    data = _load()
    lanes = data.get("lanes", {})
    rows = []
    totals = {"red_today": 0, "yellow_today": 0, "red_all_time": 0}
    for lane in config.LANES:
        rec = lanes.get(lane, {})
        row = {
            "lane": lane,
            "red_today": rec.get("red_today", 0),
            "yellow_today": rec.get("yellow_today", 0),
            "red_all_time": rec.get("red_all_time", 0),
            "last_uploaded_at": rec.get("last_uploaded_at"),
            "last_uploaded_by": rec.get("last_uploaded_by"),
        }
        for k in totals:
            totals[k] += row[k]
        rows.append(row)
    return rows, totals, data.get("refreshed_at")
