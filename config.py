"""
Central configuration.

Values come from two layers, in order of precedence:
  1. settings.json  — written by the in-app Settings page (web-editable).
  2. .env / environment variables — the defaults / bootstrap values.

The web-editable keys (spreadsheet IDs, lanes, email) live in settings.json;
secrets like the Flask secret and the service-account path stay in .env.
Call reload() after settings.json changes to pick them up without a restart.
"""
import json
import os
from dotenv import load_dotenv

load_dotenv()

SETTINGS_FILE = os.getenv("SETTINGS_FILE", "settings.json")

DEFAULT_LANES = ("Lane 1,Lane 2,Lane 3,Lane 4,Lane 5,"
                 "Lane 6,Lane 7,Lane 8,Lane 9,Lane 10")


def _list(name, default=""):
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _env_bool(name, default):
    return os.getenv(name, default).lower() == "true"


def _load_settings():
    import db
    if db.enabled():
        return db.kv_get("settings", {}) or {}
    if not os.path.exists(SETTINGS_FILE):
        return {}
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Static config (env only — not web-editable)
# ---------------------------------------------------------------------------
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")

SHEET_NAME = os.getenv("SHEET_NAME", "SHIPPING QUEUE")
RED_STATUS = os.getenv("RED_STATUS", "OVERDUE")
YELLOW_STATUS = os.getenv("YELLOW_STATUS", "TODAY")
RED_MODE = os.getenv("RED_MODE", "replace").lower()
YELLOW_MODE = os.getenv("YELLOW_MODE", "replace").lower()
RED_ALL_SUFFIX = os.getenv("RED_ALL_SUFFIX", " — All Red")
DATE_FLAGGED_HEADER = os.getenv("DATE_FLAGGED_HEADER", "DATE FLAGGED")

# Columns pulled from SHIPPING QUEUE into the output tabs.
#   C=3 DATE | E=5 ORDER ID | F=6 SKU/ISBN | G=7 TITLE | H=8 QTY | L=12 SHIP DATE
COLUMN_MAP = [
    ("DATE", 3),
    ("ORDER ID", 5),
    ("ISBN", 6),
    ("TITLE", 7),
    ("QTY", 8),
    ("SHIP DATE", 12),
]

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))

SECRET_KEY = os.getenv("FLASK_SECRET", "dev-only-change-me")
MAX_CONTENT_MB = int(os.getenv("MAX_CONTENT_MB", "50"))

AUTH_ENABLED = _env_bool("AUTH_ENABLED", "true")
USERS_FILE = os.getenv("USERS_FILE", "users.json")   # hashed passwords; seed via manage_users.py
STATE_FILE = os.getenv("STATE_FILE", "state.json")   # per-lane dashboard record

# ---------------------------------------------------------------------------
# AWB tracking (batches shipped out; delivery status fed in via Apps Script API)
# ---------------------------------------------------------------------------
AWB_STORE_FILE = os.getenv("AWB_STORE_FILE", "awbs.json")
# An AWB "awaiting delivery" past its first ship date by more than this many days
# is flagged "late".
AWB_LATE_DAYS = int(os.getenv("AWB_LATE_DAYS", "3"))
# Shared secret the Apps Scripts send to the /api/awb/* endpoints. Empty = the
# API is disabled (returns 503) until a token is set on the Settings page.
AWB_API_TOKEN = ""   # web-editable; set by reload()

# ---------------------------------------------------------------------------
# Shipment workflow (Drive folder of shipment manifests + the AWB tracking sheet)
# ---------------------------------------------------------------------------
SHIPMENT_STORE_FILE = os.getenv("SHIPMENT_STORE_FILE", "shipment.json")
DRIVE_FOLDER_ID = ""   # web-editable; the shared folder the manifests land in
AWB_SHEET_ID = ""      # web-editable; the "ALL NEW COMBINED AWB REPORT" sheet
SHIPMENT_LOG_SHEET_ID = ""  # web-editable; the "SHIPMENT LOGS" summary sheet (one row per AWB)

# Built-in default IDs. Used when nothing is saved in Settings/env, so the app
# works out of the box and survives redeploys without re-entering them. Each can
# still be overridden from the Settings page (a non-empty value there wins).
_DEFAULT_DRIVE_FOLDER_ID = "1c1lRKRFAVV72a3dw_1WqV4snI51Ti5yE"
_DEFAULT_AWB_SHEET_ID = "1KZAt5yC1ehVv82URiRRla549zqFUlsHqGyrd8OtU3OQ"
_DEFAULT_SHIPMENT_LOG_SHEET_ID = "1YCalR2F9EUM7WhcoPsk8utpNVpf-3O9rOSFJzsbgh-s"
_DEFAULT_RED_SPREADSHEET_ID = "1eugJXDkluk3CVDUTa1xLUgSo298FV96RHv_nazjQb74"
_DEFAULT_YELLOW_SPREADSHEET_ID = "1saZbj7OVSsIE4EKMOXFl-aqybYw48XOYcz7dCKU_v-w"


# ---------------------------------------------------------------------------
# Web-editable config (settings.json overlays .env). Set by reload().
# ---------------------------------------------------------------------------
RED_SPREADSHEET_ID = ""
YELLOW_SPREADSHEET_ID = ""
LANES = []
EMAIL_ENABLED = True
SMTP_SENDER = ""
SMTP_APP_PASSWORD = ""
EMAIL_RECIPIENTS = []
# Resend HTTP email API — used instead of SMTP when set (works where SMTP is
# blocked, e.g. Render). EMAIL_FROM is the "from" address (e.g. onboarding@resend.dev
# for testing, or an address on your verified domain).
RESEND_API_KEY = ""
EMAIL_FROM = ""
# Brevo HTTP email API — uses single-sender verification (verify your own inbox,
# no domain DNS needed), so you can send to any recipient.
BREVO_API_KEY = ""
# Gmail API (HTTP) — sends as your own Google/Workspace address via an OAuth2
# refresh token, so it works where SMTP is blocked (Render) with top-tier
# deliverability. Preferred over Brevo/Resend when all three fields are set.
# EMAIL_FROM (if set) is used as the From header and must be the authorised account.
GMAIL_CLIENT_ID = ""
GMAIL_CLIENT_SECRET = ""
GMAIL_REFRESH_TOKEN = ""

# Keys the Settings page is allowed to write.
EDITABLE_KEYS = (
    "RED_SPREADSHEET_ID", "YELLOW_SPREADSHEET_ID", "LANES",
    "EMAIL_ENABLED", "SMTP_SENDER", "SMTP_APP_PASSWORD", "EMAIL_RECIPIENTS",
    "AWB_API_TOKEN", "DRIVE_FOLDER_ID", "AWB_SHEET_ID", "SHIPMENT_LOG_SHEET_ID",
    "RESEND_API_KEY", "EMAIL_FROM", "BREVO_API_KEY",
    "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN",
)


def reload():
    """(Re)compute the web-editable values from settings.json over .env."""
    global RED_SPREADSHEET_ID, YELLOW_SPREADSHEET_ID, LANES
    global EMAIL_ENABLED, SMTP_SENDER, SMTP_APP_PASSWORD, EMAIL_RECIPIENTS
    global AWB_API_TOKEN, DRIVE_FOLDER_ID, AWB_SHEET_ID, RESEND_API_KEY, EMAIL_FROM
    global BREVO_API_KEY, GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN
    global SHIPMENT_LOG_SHEET_ID
    s = _load_settings()
    RESEND_API_KEY = s["RESEND_API_KEY"] if "RESEND_API_KEY" in s else os.getenv("RESEND_API_KEY", "")
    EMAIL_FROM = s["EMAIL_FROM"] if "EMAIL_FROM" in s else os.getenv("EMAIL_FROM", "")
    BREVO_API_KEY = s["BREVO_API_KEY"] if "BREVO_API_KEY" in s else os.getenv("BREVO_API_KEY", "")
    GMAIL_CLIENT_ID = s["GMAIL_CLIENT_ID"] if "GMAIL_CLIENT_ID" in s else os.getenv("GMAIL_CLIENT_ID", "")
    GMAIL_CLIENT_SECRET = s["GMAIL_CLIENT_SECRET"] if "GMAIL_CLIENT_SECRET" in s \
        else os.getenv("GMAIL_CLIENT_SECRET", "")
    GMAIL_REFRESH_TOKEN = s["GMAIL_REFRESH_TOKEN"] if "GMAIL_REFRESH_TOKEN" in s \
        else os.getenv("GMAIL_REFRESH_TOKEN", "")
    AWB_API_TOKEN = s["AWB_API_TOKEN"] if "AWB_API_TOKEN" in s else os.getenv("AWB_API_TOKEN", "")
    # These fall back to a built-in default when unset/blank (so a wiped DB or a
    # fresh deploy still works); a non-empty Settings/env value overrides.
    DRIVE_FOLDER_ID = s.get("DRIVE_FOLDER_ID") or os.getenv("DRIVE_FOLDER_ID") or _DEFAULT_DRIVE_FOLDER_ID
    AWB_SHEET_ID = s.get("AWB_SHEET_ID") or os.getenv("AWB_SHEET_ID") or _DEFAULT_AWB_SHEET_ID
    SHIPMENT_LOG_SHEET_ID = (s.get("SHIPMENT_LOG_SHEET_ID") or os.getenv("SHIPMENT_LOG_SHEET_ID")
                             or _DEFAULT_SHIPMENT_LOG_SHEET_ID)

    RED_SPREADSHEET_ID = (s.get("RED_SPREADSHEET_ID") or os.getenv("RED_SPREADSHEET_ID")
                          or _DEFAULT_RED_SPREADSHEET_ID)
    YELLOW_SPREADSHEET_ID = (s.get("YELLOW_SPREADSHEET_ID") or os.getenv("YELLOW_SPREADSHEET_ID")
                             or _DEFAULT_YELLOW_SPREADSHEET_ID)
    LANES = s["LANES"] if isinstance(s.get("LANES"), list) and s["LANES"] \
        else _list("LANES", DEFAULT_LANES)
    EMAIL_ENABLED = bool(s["EMAIL_ENABLED"]) if "EMAIL_ENABLED" in s \
        else _env_bool("EMAIL_ENABLED", "true")
    SMTP_SENDER = s["SMTP_SENDER"] if "SMTP_SENDER" in s else os.getenv("SMTP_SENDER", "")
    SMTP_APP_PASSWORD = s["SMTP_APP_PASSWORD"] if "SMTP_APP_PASSWORD" in s \
        else os.getenv("SMTP_APP_PASSWORD", "")
    EMAIL_RECIPIENTS = s["EMAIL_RECIPIENTS"] if isinstance(s.get("EMAIL_RECIPIENTS"), list) \
        else _list("EMAIL_RECIPIENTS", "")


def save_settings(updates):
    """Merge `updates` (only EDITABLE_KEYS) into settings and reload()."""
    import db
    s = _load_settings()
    for k, v in updates.items():
        if k in EDITABLE_KEYS:
            s[k] = v
    if db.enabled():
        db.kv_set("settings", s)
    else:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(s, f, indent=2, ensure_ascii=False)
    reload()


def current_settings():
    """The current web-editable values (for pre-filling the Settings form)."""
    return {
        "RED_SPREADSHEET_ID": RED_SPREADSHEET_ID,
        "YELLOW_SPREADSHEET_ID": YELLOW_SPREADSHEET_ID,
        "LANES": LANES,
        "EMAIL_ENABLED": EMAIL_ENABLED,
        "SMTP_SENDER": SMTP_SENDER,
        "SMTP_APP_PASSWORD": SMTP_APP_PASSWORD,
        "EMAIL_RECIPIENTS": EMAIL_RECIPIENTS,
        "AWB_API_TOKEN": AWB_API_TOKEN,
        "DRIVE_FOLDER_ID": DRIVE_FOLDER_ID,
        "AWB_SHEET_ID": AWB_SHEET_ID,
        "SHIPMENT_LOG_SHEET_ID": SHIPMENT_LOG_SHEET_ID,
        "RESEND_API_KEY": RESEND_API_KEY,
        "EMAIL_FROM": EMAIL_FROM,
        "BREVO_API_KEY": BREVO_API_KEY,
        "GMAIL_CLIENT_ID": GMAIL_CLIENT_ID,
        "GMAIL_CLIENT_SECRET": GMAIL_CLIENT_SECRET,
        "GMAIL_REFRESH_TOKEN": GMAIL_REFRESH_TOKEN,
    }


def service_account_info():
    """The service-account key as a dict — from the GOOGLE_SERVICE_ACCOUNT_JSON
    env var (for Render/hosted, where you can't commit the file) or the local
    JSON file. Returns None if neither is available/valid."""
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    if os.path.exists(SERVICE_ACCOUNT_FILE):
        try:
            with open(SERVICE_ACCOUNT_FILE) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
    return None


def has_service_account():
    return service_account_info() is not None


def service_account_email():
    """The service-account client_email (to share sheets with)."""
    info = service_account_info()
    return info.get("client_email", "") if info else ""


def google_ready():
    return bool(RED_SPREADSHEET_ID and YELLOW_SPREADSHEET_ID and has_service_account())


def email_ready():
    if not (EMAIL_ENABLED and EMAIL_RECIPIENTS):
        return False
    return bool((GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN)
                or (BREVO_API_KEY and EMAIL_FROM) or (RESEND_API_KEY and EMAIL_FROM)
                or (SMTP_SENDER and SMTP_APP_PASSWORD))


reload()
