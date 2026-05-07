"""Microsoft Graph authentication via MSAL device code flow."""
import os
import sys
from pathlib import Path

import msal
from dotenv import load_dotenv

load_dotenv()

AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["https://graph.microsoft.com/Mail.Read"]
TOKEN_CACHE_PATH = Path(__file__).parent / ".token_cache.json"


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE_PATH.exists():
        cache.deserialize(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        TOKEN_CACHE_PATH.write_text(cache.serialize(), encoding="utf-8")


def get_access_token(allow_device_flow: bool = True) -> str:
    """Return a valid access token. Raises RuntimeError if no cache and allow_device_flow=False."""
    client_id = os.getenv("GRAPH_CLIENT_ID")
    if not client_id:
        print("ERROR: GRAPH_CLIENT_ID must be set in .env")
        sys.exit(1)

    cache = _load_cache()
    app = msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache)

    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _save_cache(cache)
            return result["access_token"]

    if not allow_device_flow:
        raise RuntimeError(
            "Not authenticated. Run `uv run python ingest.py --test` first to sign in."
        )

    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        print(f"ERROR: Could not start device flow: {flow.get('error_description')}")
        sys.exit(1)

    print("\n" + flow["message"])
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        print(f"ERROR: Authentication failed: {result.get('error_description')}")
        sys.exit(1)

    _save_cache(cache)
    print("Authenticated. Token cached to .token_cache.json")
    return result["access_token"]
