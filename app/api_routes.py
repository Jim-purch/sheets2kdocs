"""JSON API endpoints used by the admin UI (all behind session auth)."""
from __future__ import annotations

import json
import threading

from fastapi import APIRouter, HTTPException, Request

from . import db, sync_engine
from .config import settings
from .google_source import parse_sheet_url
from .kdocs_client import KdocsClient, KdocsError

router = APIRouter()


def _client() -> KdocsClient:
    if not settings.kdocs_app_id or not settings.kdocs_app_key:
        raise HTTPException(400, "尚未配置 KDOCS_APP_ID / KDOCS_APP_KEY（.env）")
    return KdocsClient(settings.kdocs_app_id, settings.kdocs_app_key)


@router.get("/google/preview")
def google_preview(url: str, sheet_name: str = "", source_range: str = "", mode: str = "public"):
    try:
        info = parse_sheet_url(url)
    except Exception as e:
        raise HTTPException(400, str(e))
    source = {
        **info,
        "sheet_name": sheet_name,
        "source_range": source_range,
        "mode": mode,
        "api_key": settings.google_api_key,
        "sa_json": settings.google_sa_json,
    }
    from .google_source import fetch_sheet_rows

    try:
        headers, rows = fetch_sheet_rows(source)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {
        "spreadsheet_id": info["spreadsheet_id"],
        "gid": info.get("gid") or "",
        "headers": headers,
        "sample_rows": rows[:5],
        "total_rows": len(rows),
    }


@router.get("/kdocs/files")
def kdocs_files():
    try:
        files = _client().list_files_flat()
    except KdocsError as e:
        raise HTTPException(400, str(e))
    return {"files": files}


@router.get("/kdocs/sheets/{file_token}")
def kdocs_sheets(file_token: str):
    try:
        sheets = _client().list_sheets(file_token)
    except KdocsError as e:
        raise HTTPException(400, str(e))
    return {"sheets": sheets}


@router.get("/kdocs/fields/{file_token}/{sheet_id}")
def kdocs_fields(file_token: str, sheet_id: int):
    try:
        fields = _client().get_fields(file_token, sheet_id)
    except KdocsError as e:
        raise HTTPException(400, str(e))
    return {"fields": fields}


@router.post("/kdocs/tables")
async def kdocs_create_table(request: Request):
    body = await request.json()
    file_token = (body.get("file_token") or "").strip()
    name = (body.get("name") or "").strip()
    fields = [str(f).strip() for f in body.get("fields") or [] if str(f).strip()]
    if not file_token or not name or not fields:
        raise HTTPException(400, "file_token / name / fields 均必填")
    try:
        data = _client().create_data_table(file_token, name, fields)
    except KdocsError as e:
        raise HTTPException(400, str(e))
    sheet = (data.get("detail") or {}).get("sheet") or {}
    return {"sheet_id": sheet.get("id"), "name": sheet.get("name")}


@router.get("/kdocs/token-status")
def kdocs_token_status():
    row = db.q_one("SELECT * FROM kdocs_tokens WHERE id = 1")
    if not row or not row["refresh_token"]:
        return {"authorized": False}
    import time

    return {
        "authorized": True,
        "expires_at": row["expires_at"],
        "expired": int(row["expires_at"]) <= time.time(),
        "updated_at": row["updated_at"],
    }


@router.post("/tasks/{task_id}/run")
def task_run(task_id: int):
    task = db.q_one("SELECT * FROM sync_tasks WHERE id = ?", (task_id,))
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["last_status"] == "running":
        raise HTTPException(409, "任务正在运行中")

    def _worker():
        from . import db as _db

        _db.close_thread_conn()
        try:
            sync_engine.run_task(task_id)
        except Exception:
            pass
        finally:
            _db.close_thread_conn()

    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True, "message": "已触发，稍后刷新查看结果"}
