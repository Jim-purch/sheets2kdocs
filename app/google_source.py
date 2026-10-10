"""Google Sheets readers.

Three access modes:
- public:      link-shared sheet, read via the gviz CSV endpoint (no credentials)
- api_key:     Google Sheets API v4 with an API key (sheet must be link-shared)
- sa:          Google Sheets API v4 with a service-account JSON (private sheets;
               share the sheet with the service-account e-mail as Viewer)
"""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

import httpx

USER_AGENT = "sheets2kdocs/1.0 (+https://github.com/)"

_SHEET_URL_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
_GID_RE = re.compile(r"[#&?]gid=([0-9]+)")


class GoogleSourceError(RuntimeError):
    pass


def parse_sheet_url(url: str) -> dict[str, Any]:
    """Extract spreadsheet_id and gid from a Google Sheets URL."""
    m = _SHEET_URL_RE.search(url or "")
    if not m:
        raise GoogleSourceError(
            "无法从 URL 解析 spreadsheet_id，请粘贴形如 "
            "https://docs.google.com/spreadsheets/d/<ID>/edit 的链接"
        )
    gid = None
    mg = _GID_RE.search(url)
    if mg:
        gid = mg.group(1)
    return {"spreadsheet_id": m.group(1), "gid": gid}


def parse_sa_json(sa_json: str) -> dict:
    """Service-account credentials: inline JSON string or path to a JSON file."""
    text = sa_json.strip()
    if text.startswith("{"):
        return json.loads(text)
    with open(text, "r", encoding="utf-8") as f:
        return json.load(f)


def _sa_access_token(sa_info: dict) -> str:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
    except ImportError as e:  # pragma: no cover
        raise GoogleSourceError("缺少 google-auth 依赖，无法使用 Service Account 模式") from e

    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    creds.refresh(Request())
    return creds.token


def _resolve_title_if_needed(
    spreadsheet_id: str, gid: str | None, sheet_name: str, auth: dict[str, Any]
) -> str:
    """Resolve a worksheet title from gid via the spreadsheet metadata API."""
    if sheet_name:
        return sheet_name
    if not gid:
        return ""
    params: dict[str, Any] = {"fields": "sheets.properties"}
    http = auth["http"]
    r = http.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
        params=params,
    )
    if r.status_code != 200:
        raise GoogleSourceError(f"读取表格元信息失败: HTTP {r.status_code} {r.text[:200]}")
    for s in r.json().get("sheets", []):
        props = s.get("properties", {})
        if str(props.get("sheetId")) == str(gid):
            return props.get("title", "")
    raise GoogleSourceError(f"未找到 gid={gid} 对应的工作表")


def _fetch_via_api(source: dict[str, Any], auth: dict[str, Any]) -> tuple[str, list[list[str]]]:
    spreadsheet_id = source["spreadsheet_id"]
    title = _resolve_title_if_needed(
        spreadsheet_id, source.get("gid"), source.get("sheet_name", ""), auth
    )
    rng = source.get("source_range") or (f"'{title}'" if title else "A1:ZZ1000000")
    r = http.get(
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{rng}",
        params={"majorDimension": "ROWS", **auth["params"]},
    )
    if r.status_code != 200:
        raise GoogleSourceError(
            f"Google Sheets API 返回 HTTP {r.status_code}: {r.text[:300]}"
        )
    values = r.json().get("values", [])
    return "api", [[str(c) if c is not None else "" for c in row] for row in values]


def _fetch_via_gviz(source: dict[str, Any]) -> tuple[str, list[list[str]]]:
    spreadsheet_id = source["spreadsheet_id"]
    params: dict[str, Any] = {"tqx": "out:csv"}
    if source.get("sheet_name"):
        params["sheet"] = source["sheet_name"]
    elif source.get("gid"):
        params["gid"] = source["gid"]
    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq"
    try:
        r = httpx.get(
            url,
            params=params,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=30,
        )
    except httpx.HTTPError as e:
        raise GoogleSourceError(f"请求 Google 失败: {e}") from e
    if r.status_code != 200:
        raise GoogleSourceError(
            f"gviz 导出返回 HTTP {r.status_code}。请确认表格已设为「知道链接的任何人可查看」。"
        )
    if r.text.lstrip().startswith("<"):
        raise GoogleSourceError(
            "gviz 返回了 HTML（通常是表格未公开分享或地址错误）。"
            "请在 Google Sheets 中设置「分享 → 知道链接的任何人 → 查看者」。"
        )
    return "gviz", parse_csv_grid(r.text)


def parse_csv_grid(text: str) -> list[list[str]]:
    """Parse CSV text into a trimmed grid: header row + data rows."""
    rows = list(csv.reader(io.StringIO(text)))
    return normalize_grid(rows)


def normalize_grid(rows: list[list[str]]) -> list[list[str]]:
    """Trim fully-empty trailing rows/columns and pad ragged rows."""
    grid = [[("" if c is None else str(c)) for c in row] for row in rows]
    # trim trailing empty rows
    while grid and not any(c.strip() for c in grid[-1]):
        grid.pop()
    # find last non-empty column
    width = 0
    for row in grid:
        for i in range(len(row) - 1, -1, -1):
            if row[i].strip():
                width = max(width, i + 1)
                break
    grid = [row[:width] + [""] * max(0, width - len(row)) for row in grid]
    return grid


def build_headers(first_row: list[str]) -> list[str]:
    headers: list[str] = []
    seen: set[str] = set()
    for i, cell in enumerate(first_row):
        name = cell.strip()
        if not name:
            name = f"col_{i + 1}"
        base, n = name, 2
        while name in seen:
            name = f"{base}_{n}"
            n += 1
        seen.add(name)
        headers.append(name)
    return headers


def fetch_sheet_rows(source: dict[str, Any]) -> tuple[list[str], list[list[str]]]:
    """Fetch a worksheet.

    Returns (headers, data_rows). `source` keys:
      spreadsheet_id*, gid, sheet_name, source_range,
      mode: public | api_key | sa, api_key, sa_json
    """
    mode = source.get("mode", "public")
    if mode in ("api_key", "sa"):
        auth: dict[str, Any] = {"params": {}, "http": httpx}
        if mode == "api_key":
            api_key = source.get("api_key", "")
            if not api_key:
                raise GoogleSourceError("api_key 模式需要在 .env 配置 GOOGLE_API_KEY")
            auth["params"]["key"] = api_key
        else:
            sa_json = source.get("sa_json", "")
            if not sa_json:
                raise GoogleSourceError(
                    "sa 模式需要在 .env 配置 GOOGLE_SERVICE_ACCOUNT_JSON"
                )
            token = _sa_access_token(parse_sa_json(sa_json))
            auth["http"] = httpx.Client(
                headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
                timeout=30,
            )
            auth["params"] = {}
        kind, grid = _fetch_via_api(source, auth)
        if kind == "api" and auth["http"] is not httpx and hasattr(auth["http"], "close"):
            auth["http"].close()
    else:
        grid = _fetch_via_gviz(source)[1]

    if not grid:
        return [], []
    headers = build_headers(grid[0])
    rows = grid[1:]
    # drop fully-empty rows inside data
    rows = [row for row in rows if any(c.strip() for c in row)]
    return headers, rows
