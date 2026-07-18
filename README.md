# Shipping Queue Splitter

A small Flask web app. You pick a **lane** and upload the order report
(`.xlsm`). The app reads the **SHIPPING QUEUE** sheet, splits the orders by
their colour, and pushes them into Google Sheets:

| Colour | SHIP STATUS | Meaning | Goes to |
|--------|-------------|---------|---------|
| 🔴 Red | `OVERDUE` | should have shipped yesterday, still not shipped | **RED** spreadsheet |
| 🟡 Yellow | `TODAY` | due to ship today, not shipped yet | **YELLOW** spreadsheet |

**Yellow** has **one tab per lane**, and each upload **replaces** it — the yellow
list only lives until the next update.

**Red** keeps **two tabs per lane**:

- `<lane>` — **today's** overdue orders, replaced on every upload (mirrors yellow).
- `<lane> — All Red` — a running **history of every order that has ever shown
  red**, one row per Order ID (deduped), with an extra **`DATE FLAGGED`** column
  showing the date each order first went overdue. This tab only ever grows.

After writing, the app **emails a per-lane count table** via Gmail, including the
uploaded lane's cumulative overdue-on-record count.

Columns copied into the output tabs: **DATE, ORDER ID, ISBN, TITLE, QTY, SHIP DATE**.

---

## 1. Install

You need Python 3.9+.

```bash
cd shipping-queue-app
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Configure

```bash
cp .env.example .env
```

Then open `.env` and fill in the values. The three things you must set up are a
**Google service account**, the **two spreadsheets**, and a **Gmail app
password** — instructions below.

### 2a. Google service account (so the app can write to Sheets)

1. Go to <https://console.cloud.google.com/> and create a project (or pick one).
2. **APIs & Services → Library** → search **Google Sheets API** → **Enable**.
   (Enabling **Google Drive API** too is harmless and avoids edge cases.)
3. **APIs & Services → Credentials → Create credentials → Service account**.
   Give it a name, click through, and create it.
4. Open the new service account → **Keys → Add key → Create new key → JSON**.
   A `.json` file downloads.
5. Put that file in this folder and name it `service_account.json`
   (or point `GOOGLE_SERVICE_ACCOUNT_FILE` at wherever you saved it).
6. Open the JSON and copy the **`client_email`** value — it looks like
   `something@your-project.iam.gserviceaccount.com`. You'll share the sheets
   with this address next.

### 2b. Create the two spreadsheets

1. In Google Sheets, create two blank spreadsheets — call them e.g.
   **"RED / OVERDUE"** and **"YELLOW / TODAY"**.
2. For each one, click **Share** and give **Editor** access to the service
   account's `client_email` from step 2a.6.
3. Copy each spreadsheet's **ID** from its URL and paste into `.env`:

   ```
   https://docs.google.com/spreadsheets/d/1AbCdEf...XyZ/edit
                                          └──────ID──────┘
   ```
   ```
   RED_SPREADSHEET_ID=1AbCdEf...XyZ
   YELLOW_SPREADSHEET_ID=1GhIjKl...WvU
   ```

   You don't need to make the 10 tabs yourself — the app creates any lane tab
   that's missing on first write. (Optionally run the helper in step 4.)

### 2c. Gmail app password (so the app can send the count email)

1. The sender Gmail account needs **2-Step Verification ON**
   (<https://myaccount.google.com/security>).
2. Go to <https://myaccount.google.com/apppasswords>, create an app password,
   and copy the 16-character code.
3. In `.env`:
   ```
   SMTP_SENDER=youraccount@gmail.com
   SMTP_APP_PASSWORD=the16charcode
   EMAIL_RECIPIENTS=boss@example.com,ops@example.com
   ```

> Don't want email yet? Set `EMAIL_ENABLED=false` and the app skips it.

## 2d. Logins & roles

The app requires a login. There are two roles:

| Role | Can see |
|------|---------|
| `ops` | **only** the Upload page. After uploading they get a plain confirmation (no stats). |
| `admin` | the **Dashboard** (per-lane red/amber overview), the full upload result, and Upload. |

Every upload records the uploader's username, which appears in the email as the
**assignee**. Passwords are stored **hashed** (never in plain text) in `users.json`.

Seed / manage accounts with the CLI:

```bash
python manage_users.py list
python manage_users.py add <username> <role>      # role: ops | admin  (prompts for password)
python manage_users.py passwd <username>          # change a password
python manage_users.py delete <username>
```

> Two demo accounts (`admin` / `ops`) are seeded with placeholder passwords —
> **change them** with `manage_users.py passwd` before real use.
>
> To work without logging in during development, set `AUTH_ENABLED=false` in
> `.env` (every request is then treated as a built-in `dev` admin).

## 3. Run

```bash
python app.py
```

Open <http://127.0.0.1:5000> and sign in.

- **Admins** land on the **Dashboard** — how many red (overdue) and amber (due-today)
  orders are in each lane, who last uploaded, and when. "Refresh from Sheets"
  reconciles the counts against the live Google Sheets. Counts also come straight
  from each upload, so the dashboard works even before Google is connected.
- **Ops** land on the **Upload** page. They pick a lane, upload the `.xlsm`, and get
  a confirmation — the detailed split is not shown to them.

### Settings page (admin)

Admins get a **Settings** page (sidebar) to configure the app from the browser —
no editing `.env` by hand:

- **Google Sheets** — the RED / YELLOW spreadsheet IDs, plus the service-account
  email to share your sheets with.
- **Lanes** — the lane list (one per line); becomes the upload dropdown + tab names.
- **Email** — enable/disable, sender, app password, recipients.
- **Users** — add users, change roles, reset passwords, delete (you can't delete
  yourself or the last admin).

These are saved to `settings.json` (which overlays `.env`) and take effect
immediately. Secrets like the Flask key and the service-account file path stay
in `.env`.

### Lane view (admin)

The dashboard shows **one lane at a time** — the route, its red/amber/on-record
counts, and (when Google is connected) the actual order rows for today's overdue,
today's due, and the full overdue history. A **lane switcher** (dropdown + prev/next)
jumps between lanes without leaving the page.

### AWB tracking (admin)

Once orders ship they go out in **batches — one AWB = one batch** (many Order IDs).
The **AWB Tracker** (sidebar) lists every batch with a derived status:

| Status | Meaning |
|--------|---------|
| Scheduled | first ship date is still in the future |
| Awaiting | first ship date has arrived; being watched for delivery |
| Late | awaiting and overdue by more than `AWB_LATE_DAYS` (default 3) |
| Delivered | the carrier reported it delivered (terminal — drops out of the active view) |

Two things feed it, both via your **Google Apps Scripts** (Settings → AWB tracking
has the token, endpoint URLs, and ready-to-paste scripts):

- **Ingest** — a script on your shared Drive folder posts each new AWB file's rows
  to `POST /api/awb/ingest`. Columns understood: `AWB, Creation Date, Order ID, QTY,
  Last Ship Date, Tracking ID, Carrier` (grouped by AWB).
- **Delivery status** — a script on your AWB status sheet posts to
  `POST /api/awb/status` when a row flips to *delivered*; the app marks that batch done
  (matched by AWB number, or Tracking ID).

Both endpoints require the shared token as the `X-API-Key` header. Generate/rotate
it on the Settings page.

> **Deployment note:** Apps Script runs in Google's cloud, so it can't reach
> `127.0.0.1`. To receive live data, host the app on a public URL (deploy behind a
> real WSGI server, or use a tunnel like ngrok) and put that host into the scripts.

---

## Optional: pre-create the 10 lane tabs

```bash
python -c "import config, sheets; \
sheets.ensure_lane_tabs(config.RED_SPREADSHEET_ID); \
sheets.ensure_lane_tabs(config.YELLOW_SPREADSHEET_ID); \
print('Lane tabs ready.')"
```

## Test the parser without Google

```bash
python selftest.py "NEW FINAL ORDER REPORTS (INDIA TO USA).xlsm"
```

---

## How red / yellow is decided

In the workbook, the row colours aren't manual fills — they come from
**conditional formatting** on the **SHIP STATUS** column (A):
`OVERDUE → red`, `TODAY → yellow`, `UPCOMING → green`. The app therefore keys
off that column's value (read from the file's cached results), which is exactly
what drives the colour. If you ever change those rules, update `RED_STATUS` /
`YELLOW_STATUS` in `.env`.

> **Important:** SHIP STATUS is a formula. If a file was generated without ever
> being opened/saved in Excel, the cached values can be blank and nothing will
> match. Just open the workbook in Excel and **Save** once, then re-upload.

## Settings reference (`.env`)

| Key | Meaning |
|-----|---------|
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Path to the service-account JSON key |
| `RED_SPREADSHEET_ID` / `YELLOW_SPREADSHEET_ID` | The two target spreadsheets |
| `LANES` | Comma-separated lane names → dropdown + tab names |
| `SHEET_NAME` | Source sheet name (default `SHIPPING QUEUE`) |
| `RED_STATUS` / `YELLOW_STATUS` | SHIP STATUS values that mean red / yellow |
| `RED_MODE` / `YELLOW_MODE` | `replace` (overwrite the today tab) or `append` |
| `RED_ALL_SUFFIX` | Tab-name suffix for the Red history tab (default ` — All Red`) |
| `SMTP_SENDER` / `SMTP_APP_PASSWORD` | Gmail sender + app password |
| `EMAIL_RECIPIENTS` | Comma-separated recipients |
| `EMAIL_ENABLED` | `false` to skip email entirely |

## Deploy to Render

The repo is Render-ready (`Procfile`, `render.yaml`, gunicorn). Secrets are **never
committed** — they're supplied as Render environment variables.

1. Push this repo to GitHub.
2. In Render: **New → Blueprint**, point it at the repo (`render.yaml` is detected),
   or **New → Web Service** with:
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app --bind 0.0.0.0:$PORT`
3. Set these environment variables (Render dashboard → Environment):

   | Var | Value |
   |-----|-------|
   | `FLASK_SECRET` | any long random string (Blueprint auto-generates) |
   | `ADMIN_USER` / `ADMIN_PASSWORD` | your first admin login (created on boot) |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | the **entire** `service_account.json`, pasted as one line |
   | `RED_SPREADSHEET_ID` / `YELLOW_SPREADSHEET_ID` | the two sheet IDs |
   | `AWB_API_TOKEN` | a token for the AWB endpoints (or set it on the Settings page) |
   | `EMAIL_ENABLED` + `SMTP_*` / `EMAIL_RECIPIENTS` | only if you want the email |

Then open the Render URL and sign in with `ADMIN_USER` / `ADMIN_PASSWORD`.

> ⚠️ **Render's free disk is ephemeral** — `users.json`, `settings.json`, `state.json`
> and `awbs.json` reset on every redeploy. That's why the admin login and the Google/
> email config come from **env vars** (they survive restarts). Users you add through
> the Settings page, and uploaded counts, will not persist across redeploys yet — fine
> for testing; we'd move to a database / persistent disk for real use.

## Notes / security

- `.env`, `service_account.json`, `users.json`, `settings.json` hold secrets — they're
  in `.gitignore`; don't commit or share them.
- This runs on `127.0.0.1` (your machine only). Before exposing it on a network,
  put it behind a real WSGI server (gunicorn/waitress) and add authentication.
- ISBNs and order IDs are written as **text** so long numbers don't turn into
  `9.78E+12`.
