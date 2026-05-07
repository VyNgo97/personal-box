"""
Fetch newsletters from Microsoft Graph API and store in the local SQLite DB.

Usage:
    python ingest.py            # Incremental sync
    python ingest.py --full     # Re-fetch all messages
    python ingest.py --test     # Verify auth and folder access
"""

import argparse
import os
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from .categorize import categorize
from .db import get_sync_state, init_db, set_sync_state, upsert_newsletter
from .ms_auth import get_access_token

load_dotenv()

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _running_as_subprocess() -> bool:
    return not sys.stdin.isatty()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _get_folder_id(token: str, display_name: str) -> str:
    resp = requests.get(
        f"{GRAPH_BASE}/me/mailFolders",
        headers=_headers(token),
        params={"$top": 100},
        timeout=30,
    )
    resp.raise_for_status()
    top_folders = resp.json().get("value", [])
    for folder in top_folders:
        if folder.get("displayName", "").lower() == display_name.lower():
            return folder["id"]
    # Search child folders (one level deep)
    for folder in top_folders:
        if folder.get("childFolderCount", 0) > 0:
            child_resp = requests.get(
                f"{GRAPH_BASE}/me/mailFolders/{folder['id']}/childFolders",
                headers=_headers(token),
                params={"$top": 100},
                timeout=30,
            )
            child_resp.raise_for_status()
            for child in child_resp.json().get("value", []):
                if child.get("displayName", "").lower() == display_name.lower():
                    return child["id"]
    names = [f["displayName"] for f in top_folders]
    print(f"ERROR: Folder '{display_name}' not found. Available: {names}")
    sys.exit(1)


def _parse_date(received_dt: str) -> str:
    try:
        dt = datetime.fromisoformat(received_dt.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _map_message(msg: dict, folder_name: str) -> dict | None:
    if "@removed" in msg:
        return None
    body_obj = msg.get("body", {})
    content_type = body_obj.get("contentType", "text").lower()
    body_content = body_obj.get("content", "")
    html_body = body_content if content_type == "html" else ""
    text_body = body_content if content_type != "html" else ""
    from_obj = msg.get("from", {}).get("emailAddress", {})
    sender_name = from_obj.get("name", "") or from_obj.get("address", "")
    sender_email = from_obj.get("address", "")
    subject = msg.get("subject") or "(no subject)"
    return {
        "id": msg["id"],
        "subject": subject,
        "sender_name": sender_name,
        "sender_email": sender_email,
        "date": _parse_date(msg.get("receivedDateTime", "")),
        "category": categorize(sender_email, sender_name, subject),
        "html_body": html_body,
        "text_body": text_body,
        "is_read": 1 if msg.get("isRead") else 0,
        "folder": folder_name,
    }


def _run_delta(token: str, folder_id: str, delta_url: str | None) -> tuple[list[dict], str | None]:
    if delta_url:
        url, params = delta_url, {}
    else:
        url = f"{GRAPH_BASE}/me/mailFolders/{folder_id}/messages/delta"
        params = {"$select": "id,subject,from,receivedDateTime,body,isRead", "$top": 50}

    messages, new_delta_link = [], None
    while url:
        resp = requests.get(url, headers=_headers(token), params=params, timeout=30)
        if resp.status_code == 410 and delta_url:
            print("WARNING: Delta link expired — falling back to full sync.")
            return _run_delta(token, folder_id, None)
        resp.raise_for_status()
        data = resp.json()
        messages.extend(data.get("value", []))
        params = {}  # params already encoded in nextLink/deltaLink
        if "@odata.nextLink" in data:
            url = data["@odata.nextLink"]
        else:
            new_delta_link = data.get("@odata.deltaLink")
            url = None

    return messages, new_delta_link


def sync(full: bool = False):
    init_db()
    allow_interactive = not _running_as_subprocess()
    try:
        token = get_access_token(allow_device_flow=allow_interactive)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    folder_name = os.getenv("MAIL_FOLDER", "INBOX")
    folder_id = _get_folder_id(token, folder_name)
    delta_url = None if full else get_sync_state("delta_link")

    print(f"Starting {'full' if (full or not delta_url) else 'incremental'} sync...")
    messages, new_delta_link = _run_delta(token, folder_id, delta_url)

    processed, tombstones = 0, 0
    for msg in messages:
        record = _map_message(msg, folder_name)
        if record is None:
            tombstones += 1
            continue
        upsert_newsletter(record)
        processed += 1
        if processed % 10 == 0:
            print(f"  Processed {processed}...")

    if new_delta_link:
        set_sync_state("delta_link", new_delta_link)

    suffix = f" Skipped {tombstones} deleted item(s)." if tombstones else ""
    print(f"Done. Ingested {processed} newsletter(s).{suffix}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest newsletters from Microsoft Graph API")
    parser.add_argument("--full", action="store_true", help="Re-fetch all messages")
    parser.add_argument("--test", action="store_true", help="Verify auth and folder access")
    args = parser.parse_args()

    if args.test:
        print("Testing Microsoft Graph authentication...")
        token = get_access_token(allow_device_flow=True)
        print("Token acquired.")
        folder_name = os.getenv("MAIL_FOLDER", "INBOX")
        folder_id = _get_folder_id(token, folder_name)
        print(f"Folder '{folder_name}' found. All checks passed.")
    else:
        sync(full=args.full)
