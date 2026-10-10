"""Sync engine: diff Google Sheets rows against Kdocs data-table records and apply."""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import db
from .config import settings
from .google_source import GoogleSourceError, fetch_sheet_rows
from .kdocs_client import get_backend, KdocsError

# Empirically probed Kdocs dbsheet cell limit: 30000 chars OK, 30625 rejected.
KDOCS_CELL_CHAR_LIMIT = 30000
DEFAULT_VALUE_MAX_CHARS = 20000  # safe default below the hard limit

logger = logging.getLogger("sheets2kdocs.sync")

_task_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def task_lock(task_id: int) -> threading.Lock:
    with _locks_guard:
        if task_id not in _task_locks:
            _task_locks[task_id] = threading.Lock()
        return _task_locks[task_id]


@dataclass
class SyncPlan:
    creates: list[dict] = field(default_factory=list)
    updates: list[dict] = field(default_factory=list)   # [{id, fields}]
    deletes: list[str] = field(default_factory=list)
    unchanged: int = 0


class SyncError(RuntimeError):
    pass


# ------------------------------------------------------------------ helpers
def _norm(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


_INT_RE = re.compile(r"^-?(0|[1-9][0-9]*)$")
_FLOAT_RE = re.compile(r"^-?(0|[1-9][0-9]*)\.[0-9]+$")


def coerce(value: str, enabled: bool) -> Any:
    """Optionally convert numeric-looking strings to numbers (preserves leading zeros)."""
    s = value.strip()
    if not enabled or not s:
        return value
    if _INT_RE.match(s):
        try:
            return int(s)
        except ValueError:
            return value
    if _FLOAT_RE.match(s):
        try:
            return float(s)
        except ValueError:
            return value
    return value


_HTML_MARK_RE = re.compile(r"<[a-zA-Z!/]")
_CLASS_DQ_RE = re.compile(r'\s*class\s*=\s*"[^"]*"')
_CLASS_SQ_RE = re.compile(r"\s*class\s*=\s*'[^']*'")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_TAG_WS_RE = re.compile(r">\s+<")


def clean_html(value: str) -> str:
    """Strip framework-generated redundancy from scraped HTML:
    class attributes (tss-*/mui-* hashed names carry no content), comments,
    and inter-tag whitespace. Tag structure and text stay intact."""
    v = _CLASS_DQ_RE.sub("", value)
    v = _CLASS_SQ_RE.sub("", v)
    v = _HTML_COMMENT_RE.sub("", v)
    v = _TAG_WS_RE.sub("><", v)
    return v


def maybe_clean_html(value: str) -> str:
    """Apply clean_html only to values that look like HTML markup."""
    if not value or len(value) < 64 or not _HTML_MARK_RE.search(value):
        return value
    return clean_html(value)


def truncate_values(rows: list[dict], max_chars: int) -> tuple[list[dict], int]:
    """Clamp over-long cell values; Kdocs rejects cells past its char limit.
    Returns (rows, truncated_count)."""
    if not max_chars or max_chars <= 0:
        return rows, 0
    truncated = 0
    out = []
    for row in rows:
        new_row = {}
        for k, v in row.items():
            if isinstance(v, str) and len(v) > max_chars:
                new_row[k] = v[:max_chars]
                truncated += 1
            else:
                new_row[k] = v
        out.append(new_row)
    return out, truncated


def apply_mapping(row: dict[str, str], mapping: dict[str, str]) -> dict[str, Any]:
    """Map {source_header: value} to {target_field: value} using field_mapping.
    Empty mapping -> identity (header names equal target field names)."""
    out: dict[str, Any] = {}
    if not mapping:
        return dict(row)
    for src, dst in mapping.items():
        if not dst or src not in row:
            continue
        out[dst] = row[src]
    return out


def compute_plan(
    src_rows: list[dict[str, Any]],
    target_records: list[dict],
    mode: str,
    key_field: str,
) -> SyncPlan:
    """Diff source rows (already mapped to target field names) vs target records.

    target_records: [{id, fields:{field: value}}]
    mode: 'replace' -> delete all target records, create everything
          'upsert'  -> key_field required; create/update/delete by key
    """
    plan = SyncPlan()
    if mode == "replace":
        plan.creates = [dict(r) for r in src_rows]
        plan.deletes = [str(r["id"]) for r in target_records]
        return plan

    if not key_field:
        raise SyncError("upsert 模式必须设置 key_field（唯一键列）")

    by_key: dict[str, dict] = {}
    for rec in target_records:
        fields = rec.get("fields") or {}
        k = _norm(fields.get(key_field))
        if k:
            by_key[k] = rec

    seen_keys: set[str] = set()
    for row in src_rows:
        k = _norm(row.get(key_field))
        if not k:
            # rows without a key are created (they cannot be matched)
            plan.creates.append(dict(row))
            continue
        seen_keys.add(k)
        rec = by_key.get(k)
        if rec is None:
            plan.creates.append(dict(row))
            continue
        existing = rec.get("fields") or {}
        changed = {
            f: v
            for f, v in row.items()
            if f in existing and _norm(existing.get(f)) != _norm(v)
        }
        # also catch fields present in source but missing on the record
        for f, v in row.items():
            if f not in existing:
                changed[f] = v
        if changed:
            plan.updates.append({"id": str(rec["id"]), "fields": changed})
        else:
            plan.unchanged += 1

    for k, rec in by_key.items():
        if k not in seen_keys:
            plan.deletes.append(str(rec["id"]))
    return plan


# --------------------------------------------------------------- task runner
def _task_source(task) -> dict[str, Any]:
    return {
        "spreadsheet_id": task["spreadsheet_id"],
        "gid": task["gid"] or None,
        "sheet_name": task["sheet_name"] or "",
        "source_range": task["source_range"] or "",
        "mode": task["source_mode"] or "public",
        "api_key": settings.google_api_key,
        "sa_json": settings.google_sa_json,
    }


def _run_task_impl(task) -> dict[str, Any]:
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    log = {
        "task_id": task["id"],
        "task_name": task["name"],
        "started_at": started,
        "status": "ok",
        "rows_total": 0,
        "created": 0,
        "updated": 0,
        "deleted": 0,
        "unchanged": 0,
        "error": "",
        "detail": "",
    }
    try:
        headers, rows = fetch_sheet_rows(_task_source(task))
        if not headers:
            raise SyncError("Google 表格没有读到任何数据（检查共享设置 / 工作表名 / 范围）")
        src_dicts = [dict(zip(headers, row)) for row in rows]
        log["rows_total"] = len(src_dicts)
        n_html_cleaned = 0

        mapping: dict[str, str] = {}
        if task["field_mapping"]:
            try:
                mapping = json.loads(task["field_mapping"])
            except json.JSONDecodeError:
                raise SyncError("field_mapping 不是合法 JSON")
        mapped = [apply_mapping(r, mapping) for r in src_dicts]
        target_fields = sorted({f for r in mapped for f in r})
        for r in mapped:
            for f in r:
                r[f] = coerce(r[f], bool(task["coerce_numbers"]))

        max_chars = min(int(task["value_max_chars"] or DEFAULT_VALUE_MAX_CHARS), KDOCS_CELL_CHAR_LIMIT)
        do_strip = bool(task["strip_html"])
        if do_strip:
            n_html_cleaned = 0
            for r in mapped:
                for f in list(r):
                    if isinstance(r[f], str):
                        cleaned = maybe_clean_html(r[f])
                        if cleaned is not r[f]:
                            n_html_cleaned += 1
                            r[f] = cleaned
            if n_html_cleaned:
                logger.info("task %s: cleaned %d html values", task["id"], n_html_cleaned)
        mapped, n_trunc = truncate_values(mapped, max_chars)
        if n_trunc:
            logger.info("task %s: truncated %d over-long values to %d chars", task["id"], n_trunc, max_chars)

        client = get_backend()

        if task["auto_create_fields"]:
            still_missing = client.ensure_fields(
                task["target_file_token"], int(task["target_sheet_id"]), target_fields
            )
            if still_missing:
                raise SyncError(
                    f"目标表缺少字段且自动创建失败: {', '.join(still_missing)}"
                )

        key_field = task["key_field"] or ""
        if mapping and key_field:
            key_field = mapping.get(key_field, key_field)

        target_records = client.query_all_records(
            task["target_file_token"], int(task["target_sheet_id"])
        )
        plan = compute_plan(
            mapped, target_records, task["mode"], key_field
        )

        if task["mode"] == "replace" and target_records and not plan.deletes:
            raise SyncError("replace 模式未能枚举目标记录，已中止以保护数据")

        created = client.create_records(
            task["target_file_token"], int(task["target_sheet_id"]), plan.creates
        )
        updated = client.update_records(
            task["target_file_token"], int(task["target_sheet_id"]), plan.updates
        )
        deleted = client.delete_records(
            task["target_file_token"], int(task["target_sheet_id"]), plan.deletes
        )

        log.update(
            created=created,
            updated=updated,
            deleted=deleted,
            unchanged=plan.unchanged,
            detail=f"mode={task['mode']}, key={key_field or '-'}"
                   + (f", html_cleaned={n_html_cleaned}" if do_strip and n_html_cleaned else "")
                   + (f", truncated={n_trunc}" if n_trunc else ""),
        )
    except (GoogleSourceError, KdocsError, SyncError) as e:
        log["status"] = "error"
        log["error"] = str(e)[:1000]
    except Exception as e:  # unexpected
        logger.exception("unexpected sync error for task %s", task["id"])
        log["status"] = "error"
        log["error"] = f"unexpected: {e}"[:1000]

    _write_log(log)
    _update_task_state(log)
    return log


def _write_log(log: dict[str, Any]) -> None:
    db.execute(
        """INSERT INTO sync_logs
           (task_id, task_name, started_at, finished_at, status, rows_total,
            created, updated, deleted, unchanged, error, detail)
           VALUES (?, ?, ?, datetime('now','localtime'), ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            log["task_id"], log["task_name"], log["started_at"], log["status"],
            log["rows_total"], log["created"], log["updated"], log["deleted"],
            log["unchanged"], log["error"], log["detail"],
        ),
    )


def _update_task_state(log: dict[str, Any]) -> None:
    db.execute(
        "UPDATE sync_tasks SET last_run_at = datetime('now','localtime'), last_status = ?, last_error = ? WHERE id = ?",
        (log["status"], log["error"][:500], log["task_id"]),
    )


def run_task(task_id: int) -> dict[str, Any]:
    """Run a task synchronously (used by scheduler and manual runs)."""
    task = db.q_one("SELECT * FROM sync_tasks WHERE id = ?", (task_id,))
    if not task:
        raise SyncError(f"任务不存在: {task_id}")
    lock = task_lock(task_id)
    if not lock.acquire(blocking=False):
        raise SyncError("该任务正在运行中，请稍后再试")
    try:
        db.execute(
            "UPDATE sync_tasks SET last_status = 'running', last_error = '' WHERE id = ?",
            (task_id,),
        )
        return _run_task_impl(task)
    finally:
        lock.release()
