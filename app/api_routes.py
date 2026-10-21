"""JSON API endpoints used by the admin UI (all behind session auth).

Exception: /api/extension/push is authenticated by X-Ext-Token header
(see main.py middleware) and is used by the TMT browser extension.
"""
from __future__ import annotations

import json
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import db, sync_engine
from .config import settings
from .google_source import parse_sheet_url
from .kdocs_client import KdocsError, backend_name, get_backend

router = APIRouter()


def _client():
    """Active backend client (cli or openapi)."""
    client = get_backend()
    if backend_name() == "openapi" and not settings.kdocs_app_id:
        raise HTTPException(400, "尚未配置 KDOCS_APP_ID / KDOCS_APP_KEY（.env）")
    return client


# ---------------------------------------------------------------------------
# Browser extension push API (X-Ext-Token auth, handled in main.py middleware)
# ---------------------------------------------------------------------------

# in-memory cache of already-seen dedupe keys: (file_id, sheet_id) -> set(keys)
_seen_keys: dict[tuple[str, int], set[str]] = {}
_seen_loaded: set[tuple[str, int]] = set()
_seen_lock = threading.Lock()


def _ensure_seen_table() -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS ext_seen_keys (
             file_id TEXT NOT NULL,
             sheet_id INTEGER NOT NULL,
             key TEXT NOT NULL,
             seen_at TEXT DEFAULT (datetime('now','localtime')),
             PRIMARY KEY (file_id, sheet_id, key)
           )"""
    )


def _get_ext_token() -> str:
    from .kdocs_cli import get_setting

    return get_setting("ext_push_token") or ""


def _ensure_ext_token() -> str:
    """Create the push token on first use."""
    from .kdocs_cli import set_setting

    tok = _get_ext_token()
    if not tok:
        import secrets as _secrets

        tok = _secrets.token_urlsafe(24)
        set_setting("ext_push_token", tok)
    return tok


def _load_seen_keys(file_id: str, sheet_id: int, dedupe_field: str) -> set[str]:
    """Dedupe keys for a table: memory cache -> db cache -> full-table backfill."""
    key = (file_id, sheet_id)
    with _seen_lock:
        if key in _seen_loaded:
            return _seen_keys.get(key, set())
    _ensure_seen_table()
    rows = db.q_all(
        "SELECT key FROM ext_seen_keys WHERE file_id = ? AND sheet_id = ?",
        (file_id, sheet_id),
    )
    if rows:
        keys = {r["key"] for r in rows}
    else:
        # cold start: backfill from the live table once
        client = _client()
        keys = set()
        for rec in client.query_all_records(file_id, sheet_id):
            fields = rec.get("fields") or {}
            v = fields.get(dedupe_field)
            if isinstance(v, dict):  # some field types wrap the value
                v = v.get("value") or v.get("text") or ""
            if v not in (None, ""):
                keys.add(str(v))
        for k in keys:
            db.execute(
                "INSERT OR IGNORE INTO ext_seen_keys (file_id, sheet_id, key) VALUES (?, ?, ?)",
                (file_id, sheet_id, k),
            )
    with _seen_lock:
        _seen_keys[key] = keys
        _seen_loaded.add(key)
    return keys


def _remember_seen_key(file_id: str, sheet_id: int, k: str) -> None:
    _ensure_seen_table()
    db.execute(
        "INSERT OR IGNORE INTO ext_seen_keys (file_id, sheet_id, key) VALUES (?, ?, ?)",
        (file_id, sheet_id, k),
    )
    with _seen_lock:
        _seen_keys.setdefault((file_id, sheet_id), set()).add(k)


# field-name cache per (file_id, sheet_id): avoids a schema call on every push
_field_cache: dict[tuple[str, int], tuple[float, set[str]]] = {}
_field_lock = threading.Lock()
_FIELD_TTL = 60  # seconds


def _ensure_row_fields(client, file_id: str, sheet_id: int, row: dict) -> None:
    """Create missing table fields (Text) so user-defined region names can land."""
    key = (file_id, sheet_id)
    now = time.time()
    with _field_lock:
        cached = _field_cache.get(key)
        if cached and now - cached[0] < _FIELD_TTL:
            existing = cached[1]
        else:
            try:
                existing = {f.get("name") for f in client.get_fields(file_id, sheet_id)}
            except Exception:
                return  # schema call failed; let the write attempt proceed
            _field_cache[key] = (now, existing)
    missing = [k for k in row.keys() if k and k not in existing]
    for name in missing:
        try:
            client.add_field(file_id, sheet_id, name)
        except Exception:
            continue
    if missing:
        with _field_lock:
            _field_cache.pop(key, None)


class _PushBody(BaseModel):
    file_id: str
    sheet_id: int
    dedupe_field: str = ""
    row: dict = {}


@router.post("/extension/push")
def extension_push(payload: _PushBody):
    """Append one row to a kdocs data table (used by the browser extension).

    NOTE: intentionally a sync `def` — kdocs-cli subprocess calls are slow and
    MUST run in FastAPI's thread pool, otherwise they block the event loop.
    Auth: X-Ext-Token header (validated in main.py middleware).
    """
    file_id = payload.file_id.strip()
    sheet_id = int(payload.sheet_id)
    row = payload.row
    dedupe_field = payload.dedupe_field.strip()
    if not row:
        raise HTTPException(400, "row 不能为空")

    client = _client()

    # auto-create missing table fields (user-defined region names become columns)
    _ensure_row_fields(client, file_id, sheet_id, row)

    try:
        client.create_records(file_id, sheet_id, [row])
    except KdocsError as e:
        raise HTTPException(400, f"写入多维表格失败: {e}")

    if dedupe_field:
        dval = row.get(dedupe_field)
        if dval not in (None, ""):
            _remember_seen_key(file_id, sheet_id, str(dval))

    return {"ok": True, "action": "append"}


@router.get("/extension/push-info")
def extension_push_info():
    """Admin-only (session auth): show the extension how to connect."""
    tok = _ensure_ext_token()
    return {
        "endpoint": "/api/extension/push",
        "token_masked": f"{tok[:6]}…{tok[-4:]}",
        "token": tok,
    }


# ---------------------------------------------------------------------------
# Extension login / presets / user-admin (X-Auth-Token, see main.py middleware)
# ---------------------------------------------------------------------------

from fastapi import Header  # noqa: E402

from . import ext_service  # noqa: E402


def _auth_user(x_auth_token: str = Header(default="")) -> dict:
    user = ext_service.auth(x_auth_token)
    if not user:
        raise HTTPException(401, "未登录或登录已过期")
    return user


def _require_admin(user: dict) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


class _LoginBody(BaseModel):
    username: str
    password: str


@router.post("/extension/auth/login")
def extension_login(payload: _LoginBody):
    """Username+password login -> session token (X-Auth-Token)."""
    result = ext_service.login(payload.username, payload.password)
    if not result:
        raise HTTPException(401, "用户名或密码错误，或账号已停用")
    return result


@router.post("/extension/auth/logout")
def extension_logout(x_auth_token: str = Header(default="")):
    ext_service.logout(x_auth_token)
    return {"ok": True}


@router.get("/extension/auth/me")
def extension_me(user: dict = Depends(_auth_user)):
    return {"user": ext_service._public_user(user)}


class _PasswordBody(BaseModel):
    old_password: str
    new_password: str


@router.post("/extension/auth/password")
def extension_change_password(payload: _PasswordBody, user: dict = Depends(_auth_user)):
    if len(payload.new_password) < 6:
        raise HTTPException(400, "新密码至少 6 位")
    ok = ext_service.change_password(user["id"], payload.old_password, payload.new_password)
    if not ok:
        raise HTTPException(400, "旧密码错误")
    return {"ok": True}


class _PresetBody(BaseModel):
    name: str
    config: dict
    is_public: bool = True
    preset_id: int | None = None


@router.get("/extension/presets")
def extension_presets_list(user: dict = Depends(_auth_user)):
    return {"presets": ext_service.list_presets(user["id"], user.get("role") == "admin")}


@router.get("/extension/presets/{preset_id}")
def extension_preset_get(preset_id: int, user: dict = Depends(_auth_user)):
    p = ext_service.get_preset(preset_id)
    if not p or (not p["isPublic"] and p["ownerId"] != user["id"] and user.get("role") != "admin"):
        raise HTTPException(404, "预设不存在")
    return p


@router.post("/extension/presets")
def extension_preset_save(payload: _PresetBody, user: dict = Depends(_auth_user)):
    if not payload.name.strip():
        raise HTTPException(400, "预设名称不能为空")
    try:
        pid = ext_service.save_preset(
            user["id"], payload.name.strip(), payload.config, payload.is_public, payload.preset_id
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": pid}


@router.delete("/extension/presets/{preset_id}")
def extension_preset_delete(preset_id: int, user: dict = Depends(_auth_user)):
    ok = ext_service.delete_preset(preset_id, user["id"], user.get("role") == "admin")
    if not ok:
        raise HTTPException(404, "预设不存在或无权限")
    return {"ok": True}


class _UserCreateBody(BaseModel):
    username: str
    password: str
    display_name: str = ""
    role: str = "user"


class _UserUpdateBody(BaseModel):
    password: str = ""
    display_name: str = ""
    role: str = ""
    enabled: bool | None = None


@router.get("/extension/admin/users")
def extension_admin_users(user: dict = Depends(_auth_user)):
    _require_admin(user)
    ext_service.ensure_admin()
    return {"users": ext_service.list_users()}


@router.post("/extension/admin/users")
def extension_admin_user_create(payload: _UserCreateBody, user: dict = Depends(_auth_user)):
    _require_admin(user)
    if len(payload.password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    try:
        u = ext_service.create_user(payload.username, payload.password, payload.display_name, payload.role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "user": u}


@router.put("/extension/admin/users/{user_id}")
def extension_admin_user_update(user_id: int, payload: _UserUpdateBody, user: dict = Depends(_auth_user)):
    _require_admin(user)
    if payload.password and len(payload.password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if user_id == user["id"] and payload.enabled is False:
        raise HTTPException(400, "不能停用自己的账号")
    u = ext_service.update_user(user_id, payload.password, payload.display_name, payload.role, payload.enabled)
    if not u:
        raise HTTPException(404, "用户不存在")
    return {"ok": True, "user": u}


@router.delete("/extension/admin/users/{user_id}")
def extension_admin_user_delete(user_id: int, user: dict = Depends(_auth_user)):
    _require_admin(user)
    if user_id == user["id"]:
        raise HTTPException(400, "不能删除自己")
    if not ext_service.delete_user(user_id):
        raise HTTPException(400, "用户不存在或为管理员账号（管理员不可删除）")
    return {"ok": True}


# ---------------------------------------------------------------------------
# AirScript webhook CORS proxy
# 浏览器页面无法直连 kdocs.cn（无 CORS 头），经服务器转发。
# 仅允许转发到 www.kdocs.cn 的 /api/v3/ide/ 路径，防 SSRF。
# ---------------------------------------------------------------------------

import httpx  # noqa: E402


class _ProxyBody(BaseModel):
    url: str
    token: str = ""
    payload: dict = {}


@router.post("/airscript")
async def airscript_proxy(body: _ProxyBody):
    from urllib.parse import urlparse

    u = urlparse(body.url or "")
    if u.scheme != "https" or u.hostname != "www.kdocs.cn" or not u.path.startswith("/api/v3/ide/"):
        raise HTTPException(400, "仅允许转发到 www.kdocs.cn 的 /api/v3/ide/ 路径")
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(
                body.url,
                json={"Context": {"argv": body.payload}},
                headers={"Content-Type": "application/json", "AirScript-Token": body.token},
            )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"webhook 请求失败: {e}")
    return JSONResponse(status_code=r.status_code, content=r.json())


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
    client = _client()
    try:
        if hasattr(client, "list_files_flat"):
            files = client.list_files_flat()
        else:
            files = client.list_files()
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
    # openapi: {detail:{sheet:{id,name}}}; cli: {sheet:{...}} or {id,name} directly
    sheet = (data.get("detail") or {}).get("sheet") or data.get("sheet") or data
    return {"sheet_id": sheet.get("id"), "name": sheet.get("name") or name}


@router.get("/kdocs/connection-test")
def kdocs_connection_test():
    """Works for both backends; verifies token/credentials are alive."""
    client = _client()
    try:
        if hasattr(client, "test_connection"):
            result = client.test_connection()
        else:
            client.list_files_flat(max_pages=1)
            result = {"ok": True}
    except KdocsError as e:
        raise HTTPException(400, str(e))
    return result


@router.post("/kdocs/cli-token")
async def kdocs_cli_token(request: Request):
    """Save/update the personal Skill token (cli backend). Hot-reload, no restart."""
    body = await request.json()
    token = (body.get("token") or "").strip()
    if not token:
        raise HTTPException(400, "token 不能为空")
    from .kdocs_cli import invalidate_token_cache, set_setting

    set_setting("cli_token", token)
    invalidate_token_cache()
    try:
        from .kdocs_cli import KdocsCliClient

        KdocsCliClient(token=token).test_connection()
    except KdocsError as e:
        raise HTTPException(400, f"token 已保存但连接测试失败: {e}")
    return {"ok": True, "message": "token 已保存并验证通过"}


@router.get("/kdocs/token-status")
def kdocs_token_status():
    if backend_name() == "cli":
        from .kdocs_cli import get_setting

        token = get_setting("cli_token")
        masked = f"{token[:6]}…{token[-4:]}" if len(token) > 12 else ("已设置" if token else "")
        return {
            "backend": "cli",
            "authorized": bool(token),
            "token_masked": masked,
        }
    row = db.q_one("SELECT * FROM kdocs_tokens WHERE id = 1")
    if not row or not row["refresh_token"]:
        return {"backend": "openapi", "authorized": False}
    import time

    return {
        "backend": "openapi",
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
