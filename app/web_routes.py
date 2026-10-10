"""HTML pages: login, dashboard, task CRUD, logs, Kdocs OAuth helper."""
from __future__ import annotations

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from . import db, scheduler
from .config import settings
from .kdocs_client import KdocsClient, KdocsError, authorize_url, backend_name
from .main import templates
from .security import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    client_ip,
    is_locked,
    make_session,
    new_state,
    register_failure,
    register_success,
    verify_login,
)
from .google_source import parse_sheet_url

router = APIRouter()


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ------------------------------------------------------------------ login
@router.get("/login")
def login_page(request: Request, next: str = "/"):
    if user := getattr(request.state, "user", None):
        return _redirect("/")
    return templates.TemplateResponse(
        request, "login.html", {"next": next if next.startswith("/") else "/"}
    )


@router.post("/login")
def login_submit(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")):
    ip = client_ip(request)
    if is_locked(ip):
        return templates.TemplateResponse(
            request, "login.html", {"next": next, "error": "尝试次数过多，请 5 分钟后再试"}, status_code=429
        )
    if not verify_login(username, password):
        register_failure(ip)
        return templates.TemplateResponse(
            request, "login.html", {"next": next, "error": "用户名或密码错误"}, status_code=401
        )
    register_success(ip)
    target = next if next.startswith("/") else "/"
    resp = _redirect(target)
    resp.set_cookie(
        SESSION_COOKIE, make_session(username.strip()), max_age=SESSION_MAX_AGE,
        httponly=True, samesite="lax",
    )
    return resp


@router.post("/logout")
def logout():
    resp = _redirect("/login")
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# -------------------------------------------------------------- dashboard
@router.get("/")
def dashboard(request: Request, msg: str = "", err: str = ""):
    tasks = [dict(t) for t in db.q_all("SELECT * FROM sync_tasks ORDER BY id DESC")]
    next_runs = scheduler.next_run_times()
    backend = backend_name()
    if backend == "cli":
        from .kdocs_cli import get_setting

        token = get_setting("cli_token")
        kdocs_ready = bool(token)
        kdocs_configured = True
    else:
        token = db.q_one("SELECT * FROM kdocs_tokens WHERE id = 1")
        kdocs_ready = bool(token and token["refresh_token"])
        kdocs_configured = bool(settings.kdocs_app_id and settings.kdocs_app_key)
    recent_errors = [
        dict(l) for l in db.q_all(
            "SELECT * FROM sync_logs WHERE status='error' ORDER BY id DESC LIMIT 5"
        )
    ]
    return templates.TemplateResponse(
        request, "dashboard.html",
        {
            "tasks": tasks, "next_runs": next_runs, "kdocs_ready": kdocs_ready,
            "kdocs_configured": kdocs_configured, "backend": backend,
            "recent_errors": recent_errors,
            "msg": msg, "err": err,
        },
    )


# -------------------------------------------------------------- task CRUD
def _parse_int(val, default: int, lo: int = 0, hi: int = 10**9) -> int:
    try:
        n = int(str(val).strip() or default)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _parse_task_form(form) -> tuple[dict | None, str]:
    name = (form.get("name") or "").strip()
    source_url = (form.get("source_url") or "").strip()
    if not name:
        return None, "任务名称不能为空"
    try:
        info = parse_sheet_url(source_url)
    except Exception as e:
        return None, str(e)
    spreadsheet_id = info["spreadsheet_id"]
    gid = (form.get("gid") or "").strip() or (info.get("gid") or "")

    target_file_token = (form.get("target_file_token") or "").strip()
    target_sheet_id = (form.get("target_sheet_id") or "").strip()
    if not target_file_token or not target_sheet_id:
        return None, "必须选择目标智能表格文件和数据表"

    mode = form.get("mode") or "upsert"
    if mode not in ("upsert", "replace"):
        mode = "upsert"
    key_field = (form.get("key_field") or "").strip()
    if mode == "upsert" and not key_field:
        return None, "upsert 模式必须选择唯一键列（key_field）"

    mapping_raw = (form.get("field_mapping") or "").strip()
    if mapping_raw:
        try:
            json.loads(mapping_raw)
        except json.JSONDecodeError:
            return None, "字段映射不是合法 JSON"

    stype = form.get("schedule_type") or "manual"
    if stype not in ("manual", "interval", "cron"):
        stype = "manual"
    svalue = (form.get("schedule_value") or "").strip()
    if stype != "manual":
        if not svalue:
            return None, "计划类型非手动时必须填写计划值"
        if stype == "cron":
            try:
                from apscheduler.triggers.cron import CronTrigger

                CronTrigger.from_crontab(svalue)
            except ValueError as e:
                return None, f"cron 表达式无效: {e}"
        elif stype == "interval":
            try:
                if int(svalue) < 1:
                    raise ValueError
            except ValueError:
                return None, "间隔分钟数必须是正整数"

    data = {
        "name": name,
        "source_url": source_url,
        "spreadsheet_id": spreadsheet_id,
        "gid": str(gid),
        "sheet_name": (form.get("sheet_name") or "").strip(),
        "source_range": (form.get("source_range") or "").strip(),
        "source_mode": form.get("source_mode") or "public",
        "target_file_token": target_file_token,
        "target_file_name": (form.get("target_file_name") or "").strip(),
        "target_sheet_id": target_sheet_id,
        "target_sheet_name": (form.get("target_sheet_name") or "").strip(),
        "mode": mode,
        "key_field": key_field,
        "field_mapping": mapping_raw,
        "auto_create_fields": 1 if form.get("auto_create_fields") else 0,
        "coerce_numbers": 1 if form.get("coerce_numbers") else 0,
        "value_max_chars": _parse_int(form.get("value_max_chars"), 8000, lo=100, hi=100000),
        "schedule_type": stype,
        "schedule_value": svalue if stype != "manual" else "",
        "enabled": 1 if form.get("enabled") else 0,
    }
    return data, ""


@router.get("/tasks/new")
def task_new_page(request: Request, err: str = ""):
    return templates.TemplateResponse(
        request, "task_form.html", {"task": None, "mapping": {}, "err": err}
    )


@router.post("/tasks/new")
async def task_new(request: Request):
    data, error = _parse_task_form(dict(await request.form()))
    if error:
        return _redirect(f"/tasks/new?err={error}")
    new_id = db.execute(
        """INSERT INTO sync_tasks
           (name, source_url, spreadsheet_id, gid, sheet_name, source_range, source_mode,
            target_file_token, target_file_name, target_sheet_id, target_sheet_name,
            mode, key_field, field_mapping, auto_create_fields, coerce_numbers,
            value_max_chars, schedule_type, schedule_value, enabled)
           VALUES (:name,:source_url,:spreadsheet_id,:gid,:sheet_name,:source_range,:source_mode,
                   :target_file_token,:target_file_name,:target_sheet_id,:target_sheet_name,
                   :mode,:key_field,:field_mapping,:auto_create_fields,:coerce_numbers,
                   :value_max_chars,:schedule_type,:schedule_value,:enabled)""",
        data,
    )
    task = dict(db.q_one("SELECT * FROM sync_tasks WHERE id = ?", (new_id,)))
    scheduler.apply_task(task)
    return _redirect("/?msg=任务已创建")


@router.get("/tasks/{task_id}/edit")
def task_edit_page(request: Request, task_id: int, err: str = ""):
    task = db.q_one("SELECT * FROM sync_tasks WHERE id = ?", (task_id,))
    if not task:
        return _redirect("/?err=任务不存在")
    mapping = {}
    if task["field_mapping"]:
        try:
            mapping = json.loads(task["field_mapping"])
        except json.JSONDecodeError:
            mapping = {}
    return templates.TemplateResponse(
        request, "task_form.html", {"task": dict(task), "mapping": mapping, "err": err}
    )


@router.post("/tasks/{task_id}/edit")
async def task_edit(request: Request, task_id: int):
    if not db.q_one("SELECT id FROM sync_tasks WHERE id = ?", (task_id,)):
        return _redirect("/?err=任务不存在")
    data, error = _parse_task_form(dict(await request.form()))
    if error:
        return _redirect(f"/tasks/{task_id}/edit?err={error}")
    db.execute(
        """UPDATE sync_tasks SET
             name=:name, source_url=:source_url, spreadsheet_id=:spreadsheet_id, gid=:gid,
             sheet_name=:sheet_name, source_range=:source_range, source_mode=:source_mode,
             target_file_token=:target_file_token, target_file_name=:target_file_name,
             target_sheet_id=:target_sheet_id, target_sheet_name=:target_sheet_name,
             mode=:mode, key_field=:key_field, field_mapping=:field_mapping,
             auto_create_fields=:auto_create_fields, coerce_numbers=:coerce_numbers,
             value_max_chars=:value_max_chars,
             schedule_type=:schedule_type, schedule_value=:schedule_value, enabled=:enabled
           WHERE id=:id""",
        {**data, "id": task_id},
    )
    task = dict(db.q_one("SELECT * FROM sync_tasks WHERE id = ?", (task_id,)))
    scheduler.apply_task(task)
    return _redirect("/?msg=任务已保存")


@router.post("/tasks/{task_id}/delete")
def task_delete(task_id: int):
    scheduler.remove_task(task_id)
    db.execute("DELETE FROM sync_tasks WHERE id = ?", (task_id,))
    db.execute("DELETE FROM sync_logs WHERE task_id = ?", (task_id,))
    return _redirect("/?msg=任务已删除")


@router.post("/tasks/{task_id}/toggle")
def task_toggle(task_id: int):
    task = db.q_one("SELECT * FROM sync_tasks WHERE id = ?", (task_id,))
    if not task:
        return _redirect("/?err=任务不存在")
    new_enabled = 0 if task["enabled"] else 1
    db.execute("UPDATE sync_tasks SET enabled = ? WHERE id = ?", (new_enabled, task_id))
    updated = dict(db.q_one("SELECT * FROM sync_tasks WHERE id = ?", (task_id,)))
    scheduler.apply_task(updated)
    return _redirect("/?msg=已" + ("启用" if new_enabled else "停用"))


@router.post("/tasks/{task_id}/run")
def task_run_form(task_id: int):
    from .api_routes import task_run  # reuse trigger logic

    try:
        task_run(task_id)
    except Exception:
        pass
    return _redirect("/?msg=已触发同步")


# ------------------------------------------------------------------- logs
@router.get("/logs")
def logs_page(request: Request, task_id: int = 0):
    if task_id:
        rows = [dict(l) for l in db.q_all(
            "SELECT * FROM sync_logs WHERE task_id = ? ORDER BY id DESC LIMIT 200", (task_id,)
        )]
    else:
        rows = [dict(l) for l in db.q_all("SELECT * FROM sync_logs ORDER BY id DESC LIMIT 200")]
    tasks = [dict(t) for t in db.q_all("SELECT id, name FROM sync_tasks ORDER BY name")]
    return templates.TemplateResponse(
        request, "logs.html", {"logs": rows, "tasks": tasks, "task_id": task_id}
    )


# ------------------------------------------------------------ kdocs auth
@router.get("/auth/kdocs")
def kdocs_auth_page(request: Request, msg: str = "", err: str = "", ok: int = 0):
    backend = backend_name()
    import time as _time

    if backend == "cli":
        from .kdocs_cli import get_setting

        token = get_setting("cli_token")
        masked = f"{token[:6]}…{token[-4:]}" if len(token) > 12 else ""
        return templates.TemplateResponse(
            request, "kdocs_cli_auth.html",
            {
                "backend": backend, "authorized": bool(token),
                "token_masked": masked, "msg": msg, "err": err, "ok": ok,
                "cli_configured": bool(token or settings.kdocs_cli_token_hint),
                "cli_path": settings.__dict__.get("kdocs_cli_path", "kdocs-cli"),
            },
        )

    token = db.q_one("SELECT * FROM kdocs_tokens WHERE id = 1")
    authorized = bool(token and token["refresh_token"])
    status = None
    if authorized:
        status = {
            "expired": int(token["expires_at"]) <= _time.time(),
            "expires_at": token["expires_at"],
            "updated_at": token["updated_at"],
        }
    link = ""
    link_error = ""
    if settings.kdocs_app_id and settings.kdocs_redirect_uri:
        try:
            link = authorize_url(
                settings.kdocs_app_id, settings.kdocs_redirect_uri, state=new_state()
            )
        except KdocsError as e:
            link_error = str(e)
    return templates.TemplateResponse(
        request, "kdocs_auth.html",
        {
            "backend": backend, "authorized": authorized, "status": status,
            "auth_link": link, "link_error": link_error, "msg": msg, "err": err,
            "ok": ok, "app_configured": bool(settings.kdocs_app_id and settings.kdocs_app_key),
            "redirect_uri": settings.kdocs_redirect_uri,
            "app_id": settings.kdocs_app_id,
        },
    )


@router.get("/auth/kdocs/callback")
def kdocs_callback(code: str = "", error: str = "", error_description: str = ""):
    if error:
        return _redirect(f"/auth/kdocs?err={error}: {error_description}")
    if not code:
        return _redirect("/auth/kdocs?err=缺少 code 参数")
    try:
        client = KdocsClient(settings.kdocs_app_id, settings.kdocs_app_key)
        client.exchange_code(code)
    except KdocsError as e:
        return _redirect(f"/auth/kdocs?err={e}")
    return _redirect("/auth/kdocs?ok=1")


@router.post("/auth/kdocs/cli-token")
def kdocs_cli_token_submit(request: Request, token: str = Form("")):
    token = token.strip()
    if not token:
        return _redirect("/auth/kdocs?err=请粘贴 token")
    from .kdocs_cli import KdocsCliClient, KdocsCliError, invalidate_token_cache, set_setting

    set_setting("cli_token", token)
    invalidate_token_cache()
    try:
        KdocsCliClient(token=token).test_connection()
    except KdocsCliError as e:
        return _redirect(f"/auth/kdocs?err={e}")
    return _redirect("/auth/kdocs?ok=1")


@router.post("/auth/kdocs/exchange")
def kdocs_exchange(code: str = Form("")):
    if not code.strip():
        return _redirect("/auth/kdocs?err=请粘贴授权后获得的 code")
    try:
        client = KdocsClient(settings.kdocs_app_id, settings.kdocs_app_key)
        client.exchange_code(code.strip())
    except KdocsError as e:
        return _redirect(f"/auth/kdocs?err={e}")
    return _redirect("/auth/kdocs?ok=1")


@router.post("/auth/kdocs/refresh")
def kdocs_refresh(refresh_token: str = Form("")):
    try:
        client = KdocsClient(settings.kdocs_app_id, settings.kdocs_app_key)
        client.refresh(refresh_token.strip() or None)
    except KdocsError as e:
        return _redirect(f"/auth/kdocs?err={e}")
    return _redirect("/auth/kdocs?msg=令牌已刷新")
