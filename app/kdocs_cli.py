"""Kdocs access via kdocs-cli binary (personal-account Skill token, no enterprise
verification needed).

The CLI is the official 金山文档 Skill CLI (installed from
https://wpsai.wpscdn.cn/skillhub/pro). Auth model:
- personal WPS account token, ~1 year validity
- obtain: https://www.kdocs.cn/latest → menu → 金山文档Skill → copy token
- inject: `--token <TOKEN>` per invocation (server has no keychain)

This module exposes the same surface as the OpenAPI client, so the sync engine
works with either backend.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from typing import Any

from . import db

DEFAULT_TIMEOUT = 120  # seconds per CLI invocation


class KdocsCliError(RuntimeError):
    def __init__(self, message: str, code: Any = None):
        super().__init__(message)
        self.code = code


def get_setting(key: str) -> str:
    row = db.q_one("SELECT value FROM app_settings WHERE key = ?", (key,))
    return (row["value"] if row else "") or ""


def set_setting(key: str, value: str) -> None:
    db.execute(
        """INSERT INTO app_settings (key, value, updated_at)
           VALUES (?, ?, datetime('now','localtime'))
           ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                         updated_at = excluded.updated_at""",
        (key, value),
    )


_token_lock = threading.Lock()
_token_cache: dict[str, str] = {"v": ""}


def resolve_token() -> str:
    """DB (hot-updatable from admin UI) > env > empty (keychain/dev mode)."""
    with _token_lock:
        if _token_cache["v"]:
            return _token_cache["v"]
        try:
            tok = get_setting("cli_token")
        except Exception:
            tok = ""
        if not tok:
            tok = os.environ.get("KDOCS_CLI_TOKEN", "")
        _token_cache["v"] = tok
        return tok


def invalidate_token_cache() -> None:
    with _token_lock:
        _token_cache["v"] = ""


class KdocsCliClient:
    """Subprocess wrapper around kdocs-cli. Mirrors KdocsOpenApiClient methods."""

    # CLI-created fields are Text-type by default and reject raw numbers,
    # so the sync engine stringifies values for this backend unless the
    # target field is a known numeric type.
    stringify_values = True

    NUMERIC_FIELD_TYPES = {"Number", "Currency", "Percent", "Rating", "AutoNumber"}

    def __init__(
        self,
        token: str | None = None,
        cli_path: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.token = token if token is not None else resolve_token()
        self.cli_path = cli_path or os.environ.get("KDOCS_CLI_PATH", "kdocs-cli")
        self.timeout = timeout

    # ------------------------------------------------------------- plumbing
    def _run(self, args: list[str]) -> dict:
        cmd = [self.cli_path, *args]
        if self.token:
            cmd += ["--token", self.token]
        cmd += ["--output", "json"]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout
            )
        except FileNotFoundError as e:
            raise KdocsCliError(
                f"kdocs-cli 未安装（{self.cli_path}）。Docker 镜像已内置；"
                "裸机部署请运行 scripts/install-kdocs-cli.sh"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise KdocsCliError(f"kdocs-cli 执行超时（>{self.timeout}s）") from e
        if proc.returncode != 0:
            raise KdocsCliError(
                f"kdocs-cli 退出码 {proc.returncode}: {(proc.stderr or proc.stdout)[-400:]}"
            )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise KdocsCliError(f"kdocs-cli 输出非 JSON: {proc.stdout[:200]}") from e
        code = payload.get("code")
        if code not in (0, None):
            raise KdocsCliError(
                f"kdocs-cli 业务错误 (code={code}): {payload.get('message')}", code=code
            )
        data = payload.get("data")
        # wrappers: {data:{detail:...}} or {data:{data:...}} or {data:...}
        if isinstance(data, dict):
            if isinstance(data.get("data"), dict):
                data = data["data"]
            if isinstance(data.get("detail"), dict):
                data = data["detail"]
        return data if isinstance(data, dict) else {}

    def _dbsheet(self, action: str, params: dict[str, Any]) -> dict:
        return self._run(["dbsheet", action, json.dumps(params, ensure_ascii=False)])

    # ------------------------------------------------------------ capability
    def test_connection(self) -> dict:
        """Lightweight connectivity/auth check via drive root listing."""
        data = self._run(["drive", "list-my-files", json.dumps({"page_size": 1})])
        return {"ok": True, "drive_id": data.get("drive_id", "")}

    # --------------------------------------------------------- target select
    def list_files(self, max_pages: int = 10) -> list[dict]:
        """Personal files (flat-ish): drive root + one level of folders."""
        out: list[dict] = []
        seen: set[str] = set()

        def _absorb(items: list[dict]) -> None:
            for it in items:
                fid = str(it.get("id") or "")
                if not fid or fid in seen:
                    continue
                seen.add(fid)
                out.append(
                    {
                        "file_token": fid,
                        "name": it.get("name") or "",
                        "ftype": ("folder" if it.get("type") == "folder" else "file"),
                        "mtime": it.get("mtime", 0),
                        "ext": it.get("ext") or "",
                    }
                )

        page_token = ""
        for _ in range(max_pages):
            params: dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            data = self._run(["drive", "list-my-files", json.dumps(params)])
            items = data.get("items") or []
            _absorb(items)
            page_token = data.get("page_token") or data.get("next_page_token") or ""
            if not page_token or not items:
                break

        # one level deeper for folders found (personal docs usually nest 1-2 levels)
        folder_ids = [f["file_token"] for f in out if f["ftype"] == "folder"][:30]
        for fid in folder_ids:
            try:
                data = self._run(
                    ["drive", "list-files", json.dumps({"parent_id": fid, "page_size": 100})]
                )
                _absorb(data.get("items") or data.get("files") or [])
            except KdocsCliError:
                continue
        return out

    # ------------------------------------------------------------- ksheet/db
    def list_sheets(self, file_token: str) -> list[dict]:
        schema = self.get_schemas(file_token)
        sheets = schema.get("sheets") or []
        return [
            {
                "sheet_id": s.get("id"),
                "name": s.get("name"),
                "type": "db",  # dbsheet API only exposes data tables
                "idx": i,
            }
            for i, s in enumerate(sheets)
        ]

    def get_schemas(self, file_token: str) -> dict:
        return self._dbsheet("get-schema", {"file_id": file_token})

    def get_fields(self, file_token: str, sheet_id: int) -> list[dict]:
        schema = self.get_schemas(file_token)
        for s in schema.get("sheets") or []:
            if str(s.get("id")) == str(sheet_id):
                return s.get("fields") or []
        return []

    def create_data_table(self, file_token: str, name: str, field_names: list[str]) -> dict:
        # CLI supports create-sheet with views+fields; SingleLineText default
        params = {
            "file_id": file_token,
            "name": name,
            "views": [{"name": "表格视图", "type": "grid"}],
            "fields": [{"name": n, "type": "Text"} for n in field_names],
        }
        return self._dbsheet("create-sheet", params)

    def add_field(self, file_token: str, sheet_id: int, name: str) -> dict:
        return self._dbsheet(
            "create-fields",
            {
                "file_id": file_token,
                "sheet_id": int(sheet_id),
                "fields": [{"name": name, "type": "Text"}],
            },
        )

    def ensure_fields(self, file_token: str, sheet_id: int, names: list[str]) -> list[str]:
        existing = {f.get("name") for f in self.get_fields(file_token, sheet_id)}
        missing = [n for n in names if n not in existing]
        for name in missing:
            try:
                self.add_field(file_token, sheet_id, name)
            except KdocsCliError:
                continue
        existing = {f.get("name") for f in self.get_fields(file_token, sheet_id)}
        return [n for n in names if n not in existing]

    # ------------------------------------------------------------ records
    def _stringify_for_sheet(self, file_token: str, sheet_id: int, fields_list: list[dict]) -> list[dict]:
        """Convert numbers to strings except for numeric-typed target fields."""
        numeric = {
            f.get("name")
            for f in self.get_fields(file_token, sheet_id)
            if f.get("type") in self.NUMERIC_FIELD_TYPES
        }
        out = []
        for row in fields_list:
            out.append(
                {
                    k: (v if (k in numeric and not isinstance(v, str)) else str(v) if v is not None else "")
                    for k, v in row.items()
                }
            )
        return out

    def query_all_records(self, file_token: str, sheet_id: int) -> list[dict]:
        records: list[dict] = []
        offset = ""
        while True:
            params: dict[str, Any] = {
                "file_id": file_token,
                "sheet_id": int(sheet_id),
                "page_size": 100,
            }
            if offset:
                params["offset"] = offset
            detail = self._dbsheet("list-records", params)
            batch = detail.get("records") or []
            records.extend(batch)
            offset = detail.get("offset") or ""
            if not offset or not batch:
                break
        return records

    def create_records(self, file_token: str, sheet_id: int, fields_list: list[dict]) -> int:
        fields_list = self._stringify_for_sheet(file_token, sheet_id, fields_list)
        count = 0
        for i in range(0, len(fields_list), 200):
            chunk = fields_list[i : i + 200]
            self._dbsheet(
                "create-records",
                {
                    "file_id": file_token,
                    "sheet_id": int(sheet_id),
                    "records": [{"fields": f} for f in chunk],
                },
            )
            count += len(chunk)
        return count

    def update_records(self, file_token: str, sheet_id: int, items: list[dict]) -> int:
        converted = self._stringify_for_sheet(
            file_token, sheet_id, [it["fields"] for it in items]
        )
        items = [
            {"id": it["id"], "fields": conv}
            for it, conv in zip(items, converted)
        ]
        count = 0
        for i in range(0, len(items), 200):
            chunk = items[i : i + 200]
            self._dbsheet(
                "update-records",
                {
                    "file_id": file_token,
                    "sheet_id": int(sheet_id),
                    "records": [{"id": it["id"], "fields": it["fields"]} for it in chunk],
                },
            )
            count += len(chunk)
        return count

    def delete_records(self, file_token: str, sheet_id: int, record_ids: list[str]) -> int:
        count = 0
        for i in range(0, len(record_ids), 200):
            chunk = record_ids[i : i + 200]
            self._dbsheet(
                "delete-records",
                {
                    "file_id": file_token,
                    "sheet_id": int(sheet_id),
                    "records": [{"id": rid} for rid in chunk],
                },
            )
            count += len(chunk)
        return count
