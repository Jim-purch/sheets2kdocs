"""Tests for the browser-extension push endpoint (/api/extension/push)."""
import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


class FakeCli:
    stringify_values = True
    written = None
    fields_added = None

    def get_fields(self, file_id, sheet_id):
        return [{"name": "URL"}, {"name": "英文名称"}]

    def add_field(self, file_id, sheet_id, name):
        FakeCli.fields_added = name
        return {}

    def create_records(self, file_id, sheet_id, fields_list):
        FakeCli.written = fields_list
        return len(fields_list)


@pytest.fixture()
def client(tmp_db, monkeypatch):
    """App client with a fake kdocs CLI backend (no real network)."""
    from app import api_routes

    monkeypatch.setattr(api_routes, "_client", lambda: FakeCli())
    # reset module-level field cache between tests
    monkeypatch.setattr(api_routes, "_field_cache", {})
    FakeCli.written = None
    FakeCli.fields_added = None
    return TestClient(app)


def _push(client, token="tok-123", **overrides):
    body = {"file_id": "FILE", "sheet_id": 3, "row": {"URL": "u", "英文名称": "n"}}
    body.update(overrides)
    return client.post(
        "/api/extension/push", json=body, headers={"X-Ext-Token": token}
    )


def test_push_requires_token(client):
    assert client.post("/api/extension/push", json={}).status_code == 401
    r = _push(client, token="wrong")
    assert r.status_code == 401


def test_push_appends_without_dedupe(client):
    _setup_token()
    # same row pushed twice -> both append (server-side dedupe removed)
    assert _push(client).json()["action"] == "append"
    assert _push(client).json()["action"] == "append"
    assert FakeCli.written == [{"URL": "u", "英文名称": "n"}]


def test_push_autocreates_missing_fields(client):
    _setup_token()
    r = _push(client, row={"URL": "u", "新自定义区域": "v"})
    assert r.status_code == 200
    assert FakeCli.fields_added == "新自定义区域"
    assert FakeCli.written == [{"URL": "u", "新自定义区域": "v"}]


def test_push_empty_row_rejected(client):
    _setup_token()
    r = _push(client, row={})
    assert r.status_code == 400


def _setup_token():
    db.execute(
        "INSERT INTO app_settings (key, value, updated_at) "
        "VALUES ('ext_push_token', 'tok-123', datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value='tok-123'"
    )
