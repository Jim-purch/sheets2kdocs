"""Tests for extension auth / presets / user admin."""
import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture()
def client(tmp_db, monkeypatch):
    from app import ext_service

    ext_service.ensure_tables()
    ext_service.ensure_admin()
    return TestClient(app)


def _login(client, username="admin", password=None):
    from app.config import settings

    r = client.post(
        "/api/extension/auth/login",
        json={"username": username, "password": password or settings.admin_password},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_login_me_logout(client):
    d = _login(client)
    assert d["user"]["role"] == "admin"
    tok = {"X-Auth-Token": d["token"]}

    r = client.get("/api/extension/auth/me", headers=tok)
    assert r.status_code == 200 and r.json()["user"]["username"]

    r = client.post("/api/extension/auth/logout", headers=tok)
    assert r.status_code == 200
    r = client.get("/api/extension/auth/me", headers=tok)
    assert r.status_code == 401


def test_login_rejects_bad_credentials(client):
    r = client.post("/api/extension/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_preset_crud_and_visibility(client):
    admin = _login(client)
    atok = {"X-Auth-Token": admin["token"]}

    # create a regular user
    r = client.post(
        "/api/extension/admin/users",
        headers=atok,
        json={"username": "worker1", "password": "secret66", "display_name": "小王"},
    )
    assert r.status_code == 200
    worker = _login(client, "worker1", "secret66")
    wtok = {"X-Auth-Token": worker["token"]}

    # worker saves a preset
    r = client.post(
        "/api/extension/presets",
        headers=wtok,
        json={"name": "配件站点A", "config": {"sites": [{"pattern": "https://a.com/*"}]}, "is_public": True},
    )
    assert r.status_code == 200
    pid = r.json()["id"]

    # admin sees it in the list
    r = client.get("/api/extension/presets", headers=atok)
    names = [p["name"] for p in r.json()["presets"]]
    assert "配件站点A" in names

    # worker can fetch its own config
    r = client.get(f"/api/extension/presets/{pid}", headers=wtok)
    assert r.json()["config"]["sites"][0]["pattern"] == "https://a.com/*"

    # private preset hidden from other users
    client.post(
        "/api/extension/presets", headers=wtok,
        json={"name": "私有", "config": {}, "is_public": False},
    )
    priv_id = [p for p in client.get("/api/extension/presets", headers=atok).json()["presets"] if p["name"] == "私有"][0]["id"]
    # admin can see everything; create third user to test hiding
    client.post("/api/extension/admin/users", headers=atok, json={"username": "worker2", "password": "secret66"})
    w2 = _login(client, "worker2", "secret66")
    r = client.get(f"/api/extension/presets/{priv_id}", headers={"X-Auth-Token": w2["token"]})
    assert r.status_code == 404


def test_admin_user_management(client):
    admin = _login(client)
    atok = {"X-Auth-Token": admin["token"]}

    # create
    r = client.post("/api/extension/admin/users", headers=atok,
                    json={"username": "temp1", "password": "secret66"})
    uid = r.json()["user"]["id"]

    # disable
    r = client.put(f"/api/extension/admin/users/{uid}", headers=atok, json={"enabled": False})
    assert r.json()["user"]["enabled"] is False

    # disabled user cannot login
    r = client.post("/api/extension/auth/login", json={"username": "temp1", "password": "secret66"})
    assert r.status_code == 401

    # re-enable and reset password
    client.put(f"/api/extension/admin/users/{uid}", headers=atok, json={"enabled": True, "password": "newpass66"})
    r = client.post("/api/extension/auth/login", json={"username": "temp1", "password": "newpass66"})
    assert r.status_code == 200

    # delete
    r = client.delete(f"/api/extension/admin/users/{uid}", headers=atok)
    assert r.status_code == 200
    # admin account cannot be deleted
    admin_id = client.get("/api/extension/auth/me", headers=atok).json()["user"]["id"]
    r = client.delete(f"/api/extension/admin/users/{admin_id}", headers=atok)
    assert r.status_code == 400


def test_non_admin_cannot_manage_users(client):
    admin = _login(client)
    atok = {"X-Auth-Token": admin["token"]}
    client.post("/api/extension/admin/users", headers=atok, json={"username": "plain1", "password": "secret66"})
    plain = _login(client, "plain1", "secret66")
    r = client.get("/api/extension/admin/users", headers={"X-Auth-Token": plain["token"]})
    assert r.status_code == 403
