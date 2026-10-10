"""Kdocs (金山文档) OpenAPI client — server-side, token auto-refresh.

API host & docs: https://developer.kdocs.cn
- OAuth2 web flow:   /h5/auth  ->  code  ->  /api/v1/oauth2/access_token
- Token refresh:     /api/v1/oauth2/refresh_token (refresh_token valid 90 days)
- Personal files:    /api/v1/openapi/personal/files/flat
- Ksheet (智能表格):  /api/v1/openapi/ksheet/{file_token}/...
  - sheets: GET/POST (list / create data table), DELETE /{sheet_id}
  - schemas: GET (data tables + fields + views)
  - records: POST complex_query (paged list), POST/PUT/DELETE (batch CRUD)
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from . import db

BASE = "https://developer.kdocs.cn"
SCOPES = "user_basic,access_personal_files,edit_personal_files,download_personal_files"

BATCH_SIZE = 200          # records per write request (conservative)
PAGE_SIZE = 100           # records per query page
TOKEN_SKEW = 300          # refresh 5 minutes before expiry


class KdocsError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def authorize_url(app_id: str, redirect_uri: str, state: str = "") -> str:
    if not app_id:
        raise KdocsError("未配置 KDOCS_APP_ID（请在 .env 中填写开放平台应用凭据）")
    if not redirect_uri:
        raise KdocsError(
            "未配置回调地址：请在 .env 中设置 BASE_URL（或 KDOCS_REDIRECT_URI），"
            "并确保它与开放平台应用中登记的回调地址完全一致"
        )
    qs = urlencode(
        {
            "app_id": app_id,
            "scope": SCOPES,
            "redirect_uri": quote(redirect_uri, safe=""),
            "state": state,
        }
    )
    return f"{BASE}/h5/auth?{qs}"


class TokenStore:
    """Persist OAuth tokens in the app database (single row, id=1)."""

    def get(self) -> dict[str, Any] | None:
        row = db.q_one("SELECT * FROM kdocs_tokens WHERE id = 1")
        if not row or not row["refresh_token"]:
            return None
        return dict(row)

    def save(
        self,
        access_token: str,
        refresh_token: str,
        expires_in: int,
        open_id: str = "",
    ) -> None:
        db.execute(
            """INSERT INTO kdocs_tokens (id, access_token, refresh_token, expires_at, open_id, updated_at)
               VALUES (1, ?, ?, ?, ?, datetime('now','localtime'))
               ON CONFLICT(id) DO UPDATE SET
                 access_token=excluded.access_token,
                 refresh_token=CASE WHEN excluded.refresh_token <> ''
                                    THEN excluded.refresh_token ELSE kdocs_tokens.refresh_token END,
                 expires_at=excluded.expires_at,
                 open_id=CASE WHEN excluded.open_id <> '' THEN excluded.open_id ELSE kdocs_tokens.open_id END,
                 updated_at=excluded.updated_at""",
            (access_token, refresh_token, int(time.time()) + int(expires_in), open_id),
        )

    def clear(self) -> None:
        db.execute("DELETE FROM kdocs_tokens WHERE id = 1")


_refresh_lock = threading.Lock()


class KdocsClient:
    def __init__(
        self,
        app_id: str,
        app_key: str,
        store: TokenStore | None = None,
        http: httpx.Client | None = None,
    ):
        self.app_id = app_id
        self.app_key = app_key
        self.store = store or TokenStore()
        self.http = http or httpx.Client(timeout=60, headers={"Content-Type": "application/json"})

    # ------------------------------------------------------------- tokens
    def exchange_code(self, code: str) -> dict[str, Any]:
        """OAuth code -> tokens (call from the callback route)."""
        r = self.http.get(
            f"{BASE}/api/v1/oauth2/access_token",
            params={"code": code, "app_id": self.app_id, "app_key": self.app_key},
        )
        body = self._json(r)
        data = body.get("data") or {}
        if not data.get("access_token"):
            raise KdocsError(f"换取令牌失败: {json.dumps(body, ensure_ascii=False)[:300]}")
        self.store.save(
            data["access_token"],
            data.get("refresh_token", ""),
            int(data.get("expires_in", 86400)),
        )
        return data

    def refresh(self, refresh_token: str | None = None) -> dict[str, Any]:
        """Refresh access_token; optionally bootstrap with a manually pasted token."""
        if not self.app_id or not self.app_key:
            raise KdocsError("未配置 KDOCS_APP_ID / KDOCS_APP_KEY")
        rt = refresh_token
        if not rt:
            tok = self.store.get()
            rt = tok.get("refresh_token", "") if tok else ""
        if not rt:
            raise KdocsError("没有可用的 refresh_token，请先完成授权或手动粘贴 refresh_token")
        r = self.http.post(
            f"{BASE}/api/v1/oauth2/refresh_token",
            params={"app_id": self.app_id},
            json={"app_key": self.app_key, "refresh_token": rt},
        )
        body = self._json(r)
        data = body.get("data") or {}
        if not data.get("access_token"):
            raise KdocsError(
                f"刷新令牌失败（refresh_token 可能已失效，请重新授权）: "
                f"{json.dumps(body, ensure_ascii=False)[:300]}"
            )
        self.store.save(
            data["access_token"],
            data.get("refresh_token", ""),
            int(data.get("expires_in", 86400)),
        )
        return data

    def _get_token(self, force_refresh: bool = False) -> str:
        tok = self.store.get()
        if not tok:
            raise KdocsError("Kdocs 尚未授权：请先到「Kdocs 授权」页完成 OAuth 授权")
        expired = force_refresh or int(tok.get("expires_at") or 0) - TOKEN_SKEW <= time.time()
        if expired:
            with _refresh_lock:
                tok = self.store.get() or tok
                if force_refresh or int(tok.get("expires_at") or 0) - TOKEN_SKEW <= time.time():
                    self.refresh()
                    tok = self.store.get() or tok
        return tok["access_token"]

    # ----------------------------------------------------------- plumbing
    @staticmethod
    def _json(r: httpx.Response) -> dict:
        try:
            return r.json()
        except Exception:
            raise KdocsError(f"Kdocs 返回非 JSON 响应: HTTP {r.status_code} {r.text[:200]}")

    @staticmethod
    def _is_auth_error(status: int, code: Any, message: str) -> bool:
        if status in (401, 403):
            return True
        text = f"{message}".lower()
        return any(k in text for k in ("token", "令牌", "授权", "权限", "登录"))

    def _request(
        self, method: str, path: str, *, params: dict | None = None,
        payload: Any = None, _retry: bool = True,
    ) -> Any:
        token = self._get_token()
        qs = {"access_token": token}
        if params:
            qs.update(params)
        r = self.http.request(method, f"{BASE}{path}", params=qs, json=payload)
        body = self._json(r)
        code = body.get("code", 0)
        if code == 0 and r.status_code == 200:
            return body.get("data")
        message = str(body.get("message") or body.get("msg") or body)[:300]
        if _retry and self._is_auth_error(r.status_code, code, message):
            self._get_token(force_refresh=True)
            return self._request(method, path, params=params, payload=payload, _retry=False)
        raise KdocsError(f"Kdocs API 错误 (HTTP {r.status_code}, code={code}): {message}", code=code)

    # ------------------------------------------------------- personal files
    def list_files_flat(self, max_pages: int = 10) -> list[dict]:
        """All personal docs (flat), newest-modified first."""
        files: list[dict] = []
        offset: int | None = None
        for _ in range(max_pages):
            params: dict[str, Any] = {"count": 200, "order": "desc", "order_by": "mtime"}
            if offset is not None:
                params["offset"] = offset
            data = self._request("GET", "/api/v1/openapi/personal/files/flat", params=params) or {}
            batch = data.get("files") or data.get("file_list") or []
            for f in batch:
                fid = f.get("id") or {}
                file_token = (
                    f.get("fileid")
                    or f.get("file_token")
                    or fid.get("open_id")
                    or ""
                )
                files.append(
                    {
                        "file_token": str(file_token),
                        "name": f.get("fname") or f.get("name") or "",
                        "ftype": f.get("ftype") or f.get("type") or "",
                        "mtime": f.get("mtime", 0),
                    }
                )
            offset = data.get("next_offset")
            if offset in (None, -1):
                break
        return files

    # ------------------------------------------------------------- ksheet
    def list_sheets(self, file_token: str) -> list[dict]:
        data = self._request("GET", f"/api/v1/openapi/ksheet/{file_token}/sheets") or {}
        out = []
        for s in data.get("sheets_info") or []:
            out.append(
                {
                    "sheet_id": s.get("sheet_id"),
                    "name": s.get("sheet_name"),
                    "type": s.get("sheet_type"),  # 'et' worksheet | 'db' data table
                    "idx": s.get("sheet_idx"),
                }
            )
        return out

    def get_schemas(self, file_token: str) -> dict:
        """Full schema: data tables + fields + views."""
        return self._request("GET", f"/api/v1/openapi/ksheet/{file_token}/schemas") or {}

    def get_fields(self, file_token: str, sheet_id: int) -> list[dict]:
        schema = self.get_schemas(file_token)
        sheets = schema.get("detail", {}).get("sheets") or schema.get("sheets") or []
        for s in sheets:
            if str(s.get("id")) == str(sheet_id) or s.get("sheet_id") == sheet_id:
                return s.get("fields") or []
        # fallback: scan any sheet match by name only if ids differ
        return []

    def create_data_table(
        self, file_token: str, name: str, field_names: list[str]
    ) -> dict:
        body = {
            "name": name,
            "views": [{"name": "表格视图", "type": "grid"}],
            "fields": [{"name": n, "type": "SingleLineText"} for n in field_names],
        }
        return self._request(
            "POST", f"/api/v1/openapi/ksheet/{file_token}/sheets", payload=body
        ) or {}

    def add_field(self, file_token: str, sheet_id: int, name: str) -> dict:
        return self._request(
            "POST",
            f"/api/v1/openapi/ksheet/{file_token}/sheets/{sheet_id}/fields",
            payload={"fields": [{"name": name, "type": "SingleLineText"}]},
        ) or {}

    def ensure_fields(self, file_token: str, sheet_id: int, names: list[str]) -> list[str]:
        """Create missing SingleLineText fields; returns names still missing after attempts."""
        existing = {f.get("name") for f in self.get_fields(file_token, sheet_id)}
        missing = [n for n in names if n not in existing]
        for name in missing:
            try:
                self.add_field(file_token, sheet_id, name)
            except KdocsError:
                continue
        existing = {f.get("name") for f in self.get_fields(file_token, sheet_id)}
        return [n for n in names if n not in existing]

    # ------------------------------------------------------------ records
    def query_all_records(self, file_token: str, sheet_id: int) -> list[dict]:
        """Page through all records -> [{id, fields}, ...]"""
        records: list[dict] = []
        offset: str | None = None
        while True:
            body: dict[str, Any] = {"pageSize": PAGE_SIZE}
            if offset:
                body["offset"] = offset
            data = self._request(
                "POST",
                f"/api/v1/openapi/ksheet/{file_token}/sheets/{sheet_id}/records/complex_query",
                payload=body,
            )
            detail = (data or {}).get("detail") or {}
            batch = detail.get("records") or []
            records.extend(batch)
            offset = detail.get("offset")
            if not offset or not batch:
                break
        return records

    def create_records(self, file_token: str, sheet_id: int, fields_list: list[dict]) -> int:
        count = 0
        for i in range(0, len(fields_list), BATCH_SIZE):
            chunk = fields_list[i : i + BATCH_SIZE]
            self._request(
                "POST",
                f"/api/v1/openapi/ksheet/{file_token}/sheets/{sheet_id}/records",
                payload={"records": [{"fields": f} for f in chunk]},
            )
            count += len(chunk)
        return count

    def update_records(self, file_token: str, sheet_id: int, items: list[dict]) -> int:
        """items: [{id, fields}] — update only supplied fields."""
        count = 0
        for i in range(0, len(items), BATCH_SIZE):
            chunk = items[i : i + BATCH_SIZE]
            self._request(
                "PUT",
                f"/api/v1/openapi/ksheet/{file_token}/sheets/{sheet_id}/records",
                payload={"records": [{"id": it["id"], "fields": it["fields"]} for it in chunk]},
            )
            count += len(chunk)
        return count

    def delete_records(self, file_token: str, sheet_id: int, record_ids: list[str]) -> int:
        count = 0
        for i in range(0, len(record_ids), BATCH_SIZE):
            chunk = record_ids[i : i + BATCH_SIZE]
            self._request(
                "DELETE",
                f"/api/v1/openapi/ksheet/{file_token}/sheets/{sheet_id}/records",
                payload={"records": [{"id": rid} for rid in chunk]},
            )
            count += len(chunk)
        return count


def client_from_settings() -> KdocsClient:
    from .config import settings

    return KdocsClient(settings.kdocs_app_id, settings.kdocs_app_key)


def get_backend():
    """Return the active Kdocs client based on KDOCS_BACKEND setting.

    'openapi' (default): enterprise-verified OpenAPI app (app_id/app_key + OAuth)
    'cli':               kdocs-cli binary with a personal-account Skill token
                         (no enterprise verification required)
    """
    from .config import settings

    backend = (settings.kdocs_backend or "openapi").lower()
    if backend == "cli":
        from .kdocs_cli import KdocsCliClient

        return KdocsCliClient()
    return client_from_settings()


def backend_name() -> str:
    from .config import settings

    return (settings.kdocs_backend or "openapi").lower()
