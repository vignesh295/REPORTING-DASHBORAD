"""
Read-only Google Drive access via the service account — list a folder and
download files (used to pull shipment manifests the ops team drops in Drive).
Requires the Drive API enabled on the Cloud project + the folder shared with
the service account.
"""
import io
import os

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

import config

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
_service = None

# Drive mime types we treat as data files (everything else in the folder — the
# Apps Script project, sub-folders — is ignored).
DATA_MIMES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "text/csv": ".csv",
    "text/plain": ".txt",
    "application/vnd.google-apps.spreadsheet": ".csv",  # exported as csv
}


def service():
    global _service
    if _service is None:
        info = config.service_account_info()
        if info is not None:
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file(config.SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        _service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _service


def list_data_files(folder_id):
    """Data files in the folder (xlsx/xls/csv/txt/Sheet), newest first."""
    drv = service()
    out, token = [], None
    while True:
        res = drv.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, mimeType, modifiedTime, size)",
            orderBy="modifiedTime desc", pageSize=100, pageToken=token,
            supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        out.extend(res.get("files", []))
        token = res.get("nextPageToken")
        if not token:
            break
    return [f for f in out if f.get("mimeType") in DATA_MIMES]


def get_meta(file_id):
    return service().files().get(
        fileId=file_id, fields="id, name, mimeType, modifiedTime",
        supportsAllDrives=True).execute()


def download(file_id, mime_type=None, name=""):
    """Return (bytes, ext) for a Drive file. Google Sheets are exported to CSV."""
    drv = service()
    if mime_type is None:
        meta = get_meta(file_id)
        mime_type, name = meta["mimeType"], meta["name"]
    if mime_type == "application/vnd.google-apps.spreadsheet":
        return drv.files().export(fileId=file_id, mimeType="text/csv").execute(), ".csv"
    req = drv.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    ext = os.path.splitext(name)[1].lower() or DATA_MIMES.get(mime_type, ".xlsx")
    return buf.getvalue(), ext
