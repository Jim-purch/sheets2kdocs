# sheets2kdocs

**Scheduled sync connector: Google Sheets → Kdocs (WPS) 智能表格 (AirSheet / .ksheet data tables)**

A self-hosted, open-source sync service that periodically (or manually) copies rows from a Google Sheets worksheet into a Kdocs Smart Sheet data table, with a built-in web admin UI.

> 中文文档见 [README.md](README.md)

## Features

- **Two sync modes**: `upsert` (diff by key column: create / update / delete) and `replace` (clear & rewrite)
- **Three Google readers**: public share link (zero credentials) · API key · Service Account (private sheets)
- **Field mapping**: rename source headers to target fields; auto-match identical names; auto-create missing target fields
- **Schedules**: fixed interval (minutes) or 5-field cron; manual run anytime
- **Admin UI**: task CRUD, connection testers, run logs with per-run create/update/delete stats
- **Kdocs OAuth**: guided authorization + automatic token refresh (refresh_token valid 90 days)
- **Lightweight**: FastAPI + SQLite in a single container (~60 MB RAM)

## Architecture

```
Google Sheets ──① fetch──▶ sheets2kdocs ──② record-level writes──▶ Kdocs Smart Sheet
                           (FastAPI + APScheduler + SQLite)         developer.kdocs.cn OpenAPI
Browser ──▶ Nginx ──▶ Admin UI (login via .env credentials)
```

One-way sync only: Google Sheets is the source of truth; the Kdocs data table is the destination.

## Quick start (Docker)

```bash
git clone https://github.com/<you>/sheets2kdocs.git
cd sheets2kdocs
cp .env.example .env   # edit: admin credentials, SECRET_KEY, BASE_URL, KDOCS_APP_ID/KEY
docker compose up -d --build
# open http://<server>:8300
```

Prerequisites:

1. A Kdocs Open Platform app (enterprise developer verification required) — see [docs/KDOCS_SETUP.md](docs/KDOCS_SETUP.md). Register the OAuth callback `{BASE_URL}/auth/kdocs/callback` and request scopes `user_basic`, `access_personal_files`, `edit_personal_files`, `download_personal_files`.
2. The Google sheet must be link-shared ("Anyone with the link → Viewer") for public mode.
3. The target .ksheet file must contain at least one **data table** (db-type sheet).

## Usage

1. Log in to the admin UI with the credentials from `.env`
2. Complete Kdocs authorization on the **Kdocs 授权** page
3. Create a sync task: paste the Google Sheets URL → test-read headers → pick target file & data table → check field mapping → pick schedule → save
4. Hit **▶ 运行** to sync once; check **运行日志** for results

## Sync semantics

| Mode | Behavior | Use case |
|---|---|---|
| `upsert` | missing in target → create; same key, changed values → update; missing in source → **delete** | continuously maintained data |
| `replace` | wipe all target records, then insert everything | display snapshots |

Notes: values are written as text by default (enable numeric coercion to convert `123`/`1.5`; leading zeros like `007` are preserved); missing target fields are auto-created as SingleLineText; the first Google row is treated as the header.

## Configuration

See [.env.example](.env.example) for all variables. Key ones: `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `SECRET_KEY`, `BASE_URL`, `KDOCS_APP_ID`, `KDOCS_APP_KEY`, optional `GOOGLE_API_KEY` and `GOOGLE_SERVICE_ACCOUNT_JSON`.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload --port 8300
pytest
```

## License

[MIT](LICENSE)
