"""
FastAPI local newsletter viewer.

Run with:
    uv run uvicorn app:app --reload --port 8765
"""
import asyncio
import subprocess
import sys
from datetime import date, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .db import (
    get_by_category,
    get_by_date_range,
    get_categories_with_counts,
    get_newsletter,
    get_recent,
    init_db,
    mark_read,
)
from .ms_auth import get_access_token

app = FastAPI(title="Personal Newsletter Viewer")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

init_db()

# Auth flow state: "idle" | "pending" | "done" | "failed"
_auth_state: dict = {"status": "idle", "user_code": None, "verification_uri": None, "error": None}


def _sidebar_data() -> dict:
    return {"categories": get_categories_with_counts()}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    newsletters = get_recent(days=2, limit=100)
    # Group by category
    grouped: dict[str, list] = {}
    for nl in newsletters:
        grouped.setdefault(nl["category"], []).append(nl)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "grouped": grouped,
            **_sidebar_data(),
        },
    )


@app.get("/category/{name}", response_class=HTMLResponse)
async def category_view(
    request: Request,
    name: str,
    page: int = Query(1, ge=1),
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    limit = 30
    offset = (page - 1) * limit

    if start and end:
        newsletters = get_by_date_range(start, end, category=name, limit=limit, offset=offset)
    else:
        default_start = (date.today() - timedelta(days=2)).isoformat()
        default_end = date.today().isoformat()
        newsletters = get_by_date_range(default_start, default_end, category=name, limit=limit, offset=offset)

    return templates.TemplateResponse(
        "category.html",
        {
            "request": request,
            "category_name": name,
            "newsletters": newsletters,
            "page": page,
            "has_next": len(newsletters) == limit,
            "start": start or "",
            "end": end or "",
            **_sidebar_data(),
        },
    )


@app.get("/newsletter/{id:path}", response_class=HTMLResponse)
async def newsletter_view(request: Request, id: str):
    nl = get_newsletter(id)
    if not nl:
        raise HTTPException(status_code=404, detail="Newsletter not found")
    mark_read(id)
    return templates.TemplateResponse(
        "newsletter.html",
        {
            "request": request,
            "nl": nl,
            **_sidebar_data(),
        },
    )


@app.get("/raw/{id:path}", response_class=HTMLResponse)
async def raw_view(id: str):
    nl = get_newsletter(id)
    if not nl:
        raise HTTPException(status_code=404, detail="Newsletter not found")
    body = nl.get("html_body") or f"<pre>{nl.get('text_body', '')}</pre>"
    return HTMLResponse(content=body)


@app.get("/auth/status")
async def auth_status():
    try:
        await asyncio.to_thread(get_access_token, False)
        return {"authenticated": True}
    except RuntimeError:
        return {"authenticated": False}


@app.post("/auth/start")
async def auth_start():
    import os
    import msal
    from dotenv import load_dotenv
    load_dotenv()

    if _auth_state["status"] == "pending":
        return {
            "user_code": _auth_state["user_code"],
            "verification_uri": _auth_state["verification_uri"],
        }

    client_id = os.getenv("GRAPH_CLIENT_ID")
    if not client_id:
        raise HTTPException(status_code=500, detail="GRAPH_CLIENT_ID not set")

    from .ms_auth import AUTHORITY, SCOPES, _load_cache, _save_cache
    cache = _load_cache()
    msal_app = msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache)
    flow = msal_app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise HTTPException(status_code=500, detail=flow.get("error_description", "Failed to start auth"))

    _auth_state.update(status="pending", user_code=flow["user_code"], verification_uri=flow["verification_uri"], error=None)

    async def _poll():
        result = await asyncio.to_thread(msal_app.acquire_token_by_device_flow, flow)
        if "access_token" in result:
            _save_cache(cache)
            _auth_state.update(status="done", user_code=None, verification_uri=None)
        else:
            _auth_state.update(status="failed", error=result.get("error_description", "Auth failed"))

    asyncio.create_task(_poll())
    return {"user_code": flow["user_code"], "verification_uri": flow["verification_uri"]}


@app.get("/auth/poll")
async def auth_poll():
    return {"status": _auth_state["status"], "error": _auth_state.get("error")}


@app.post("/sync")
async def sync_newsletters(full: bool = False):
    try:
        cmd = [sys.executable, "-m", "personal_box.ingest"]
        if full:
            cmd.append("--full")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            return JSONResponse(
                status_code=500,
                content={"status": "error", "message": result.stderr or result.stdout},
            )
        return {"status": "ok", "message": result.stdout.strip()}
    except subprocess.TimeoutExpired:
        return JSONResponse(
            status_code=504,
            content={"status": "error", "message": "Sync timed out after 5 minutes"},
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": str(e)},
        )


@app.get("/api/newsletters")
async def api_newsletters(
    category: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=100),
):
    offset = (page - 1) * limit

    if start and end:
        results = get_by_date_range(start, end, category=category, limit=limit, offset=offset)
    else:
        default_start = (date.today() - timedelta(days=2)).isoformat()
        default_end = date.today().isoformat()
        results = get_by_date_range(default_start, default_end, category=category, limit=limit, offset=offset)

    return {
        "page": page,
        "limit": limit,
        "count": len(results),
        "newsletters": results,
    }
