"""
Persistence backend.

When TURSO_DATABASE_URL + TURSO_AUTH_TOKEN are set, app state (users, settings,
dashboard, AWBs, shipment) is stored in a Turso/libSQL database over its HTTP
API — so it survives Render redeploys. Otherwise the stores fall back to local
JSON files (local dev / tests).

Each store is one JSON blob in a `kv` table: key TEXT PRIMARY KEY, value TEXT.
Reads are resilient (return the default if the DB hiccups); writes raise.
"""
import json
import os
import ssl
import urllib.request

try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover - certifi should be present
    _CTX = ssl.create_default_context()

_table_ready = False


def _url():
    u = os.getenv("TURSO_DATABASE_URL", "").strip()
    if u.startswith("libsql://"):
        u = "https://" + u[len("libsql://"):]
    return u


def _token():
    return os.getenv("TURSO_AUTH_TOKEN", "").strip()


def enabled():
    return bool(_url() and _token())


def _pipeline(stmts):
    """stmts: list of (sql, args|None). Returns the parsed response; raises on error."""
    reqs = []
    for sql, args in stmts:
        reqs.append({"type": "execute", "stmt": {"sql": sql, "args": [
            ({"type": "null"} if a is None else {"type": "text", "value": str(a)})
            for a in (args or [])]}})
    reqs.append({"type": "close"})
    body = json.dumps({"requests": reqs}).encode("utf-8")
    req = urllib.request.Request(
        _url() + "/v2/pipeline", data=body,
        headers={"Authorization": "Bearer " + _token(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20, context=_CTX) as resp:
        out = json.loads(resp.read())
    for res in out.get("results", []):
        if res.get("type") == "error":
            raise RuntimeError("libsql: " + json.dumps(res.get("error", res)))
    return out


def _ensure():
    global _table_ready
    if not _table_ready:
        _pipeline([("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)", None)])
        _table_ready = True


def kv_get(key, default=None):
    try:
        _ensure()
        out = _pipeline([("SELECT value FROM kv WHERE key = ?", [key])])
        rows = out["results"][0]["response"]["result"]["rows"]
        if not rows or rows[0][0].get("type") == "null":
            return default
        return json.loads(rows[0][0]["value"])
    except Exception:  # noqa: BLE001 - reads never crash the app
        return default


def kv_set(key, value):
    _ensure()
    _pipeline([("INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [key, json.dumps(value, ensure_ascii=False)])])
