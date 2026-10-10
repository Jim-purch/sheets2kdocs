import json
import time

import httpx
import pytest

from app.kdocs_client import KdocsClient, KdocsError, TokenStore

APP_ID = "AKTEST"
APP_KEY = "sk-test"


def make_client(handler, tmp_db) -> KdocsClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return KdocsClient(APP_ID, APP_KEY, store=TokenStore(), http=http)


def route(method, url, body):
    return httpx.Response(200, json=body)


class TestTokenExchange:
    def test_exchange_saves_tokens(self, tmp_db):
        def handler(request):
            assert request.url.path == "/api/v1/oauth2/access_token"
            assert request.url.params["app_id"] == APP_ID
            assert request.url.params["code"] == "c1"
            return route(request.method, request.url, {
                "code": 0,
                "data": {"access_token": "at1", "refresh_token": "rt1", "expires_in": 86400},
            })

        c = make_client(handler, tmp_db)
        data = c.exchange_code("c1")
        assert data["access_token"] == "at1"
        tok = c.store.get()
        assert tok["access_token"] == "at1"
        assert tok["refresh_token"] == "rt1"

    def test_exchange_bad_response(self, tmp_db):
        def handler(request):
            return route(request.method, request.url, {"code": 20001, "message": "bad code"})

        c = make_client(handler, tmp_db)
        with pytest.raises(KdocsError):
            c.exchange_code("bad")


class TestAutoRefresh:
    def test_expired_token_triggers_refresh(self, tmp_db):
        c = make_client(lambda r: route(r.method, r.url, {}), tmp_db)
        # seed an expired token
        c.store.save("old-at", "rt-ok", expires_in=-100)

        def handler(request):
            if request.url.path == "/api/v1/oauth2/refresh_token":
                assert request.url.params["app_id"] == APP_ID
                body = json.loads(request.content)
                assert body["refresh_token"] == "rt-ok"
                return route(request.method, request.url, {
                    "code": 0,
                    "data": {"access_token": "new-at", "refresh_token": "", "expires_in": 86400},
                })
            # the actual API call
            assert request.url.params["access_token"] == "new-at"
            return route(request.method, request.url, {"code": 0, "data": {"ok": 1}})

        c.http = httpx.Client(transport=httpx.MockTransport(handler))
        out = c._request("GET", "/api/v1/openapi/personal/files/flat")
        assert out == {"ok": 1}
        assert c.store.get()["access_token"] == "new-at"
        # refresh_token kept when API returns empty
        assert c.store.get()["refresh_token"] == "rt-ok"

    def test_401_retries_after_refresh(self, tmp_db):
        c = make_client(lambda r: route(r.method, r.url, {}), tmp_db)
        c.store.save("expired-at", "rt-2", expires_in=3600)
        calls = {"api": 0, "refresh": 0}

        def handler(request):
            if request.url.path.endswith("/refresh_token"):
                calls["refresh"] += 1
                return route(request.method, request.url, {
                    "code": 0,
                    "data": {"access_token": "fresh", "refresh_token": "rt-3", "expires_in": 86400},
                })
            calls["api"] += 1
            if calls["api"] == 1:
                return httpx.Response(401, json={"code": 401, "message": "token expired"})
            assert request.url.params["access_token"] == "fresh"
            return route(request.method, request.url, {"code": 0, "data": {}})

        c.http = httpx.Client(transport=httpx.MockTransport(handler))
        c._request("GET", "/api/v1/openapi/ksheet/ft/sheets")
        assert calls == {"api": 2, "refresh": 1}

    def test_no_token_raises(self, tmp_db):
        c = make_client(lambda r: route(r.method, r.url, {}), tmp_db)
        with pytest.raises(KdocsError, match="尚未授权"):
            c._request("GET", "/x")


class TestRecords:
    def _seed(self, c):
        c.store.save("at", "rt", expires_in=3600)

    def test_query_all_records_paginates(self, tmp_db):
        c = make_client(lambda r: route(r.method, r.url, {}), tmp_db)
        self._seed(c)
        pages = [
            {"detail": {"records": [{"id": "1", "fields": {"K": "a"}}], "offset": "p2"}},
            {"detail": {"records": [{"id": "2", "fields": {"K": "b"}}], "offset": None}},
        ]
        n = {"i": 0}

        def handler(request):
            body = json.loads(request.content)
            assert request.url.path.endswith("/records/complex_query")
            assert body["pageSize"] == 100
            if n["i"] == 1:
                assert body.get("offset") == "p2"
            out = pages[n["i"]]
            n["i"] += 1
            return route(request.method, request.url, {"code": 0, "data": out})

        c.http = httpx.Client(transport=httpx.MockTransport(handler))
        records = c.query_all_records("ft", 1)
        assert [r["id"] for r in records] == ["1", "2"]

    def test_create_records_chunked(self, tmp_db):
        c = make_client(lambda r: route(r.method, r.url, {}), tmp_db)
        self._seed(c)
        posts = []

        def handler(request):
            if request.method == "POST" and request.url.path.endswith("/records"):
                body = json.loads(request.content)
                posts.append(len(body["records"]))
                return route(request.method, request.url, {"code": 0, "data": {}})
            return route(request.method, request.url, {"code": 0, "data": {}})

        c.http = httpx.Client(transport=httpx.MockTransport(handler))
        rows = [{"F": i} for i in range(450)]
        assert c.create_records("ft", 1, rows) == 450
        assert posts == [200, 200, 50]

    def test_delete_records_payload(self, tmp_db):
        c = make_client(lambda r: route(r.method, r.url, {}), tmp_db)
        self._seed(c)
        seen = []

        def handler(request):
            if request.method == "DELETE":
                seen.append(json.loads(request.content))
                return route(request.method, request.url, {
                    "code": 0, "data": {"detail": {"records": [{"id": "a", "deleted": True}]}},
                })
            return route(request.method, request.url, {"code": 0, "data": {}})

        c.http = httpx.Client(transport=httpx.MockTransport(handler))
        c.delete_records("ft", 1, ["a", "b"])
        assert seen == [{"records": [{"id": "a"}, {"id": "b"}]}]


class TestApiError:
    def test_error_raises_with_code(self, tmp_db):
        c = make_client(lambda r: route(r.method, r.url, {}), tmp_db)
        c.store.save("at", "rt", expires_in=3600)

        def handler(request):
            return route(request.method, request.url, {"code": 20002, "message": "文件不存在"})

        c.http = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(KdocsError, match="20002"):
            c._request("GET", "/api/v1/openapi/ksheet/ft/sheets")


class TestAuthorizeUrl:
    def test_url_contains_params(self):
        from app.kdocs_client import authorize_url

        url = authorize_url("AK1", "http://x/auth/kdocs/callback", state="s")
        assert url.startswith("https://developer.kdocs.cn/h5/auth?")
        assert "app_id=AK1" in url
        assert "user_basic" in url

    def test_missing_app_id(self):
        from app.kdocs_client import authorize_url

        with pytest.raises(KdocsError):
            authorize_url("", "http://x/cb")


class TestEnsureFields:
    def test_creates_missing(self, tmp_db):
        c = make_client(lambda r: route(r.method, r.url, {}), tmp_db)
        c.store.save("at", "rt", expires_in=3600)
        state = {"fields": [{"name": "A"}, {"name": "B"}]}

        def handler(request):
            if request.url.path.endswith("/schemas"):
                return route(request.method, request.url, {
                    "code": 0,
                    "data": {"detail": {"sheets": [{"id": 1, "fields": state["fields"]}]}},
                })
            if request.url.path.endswith("/fields"):
                body = json.loads(request.content)
                state["fields"].extend(body["fields"])
                return route(request.method, request.url, {"code": 0, "data": {}})
            return route(request.method, request.url, {"code": 0, "data": {}})

        c.http = httpx.Client(transport=httpx.MockTransport(handler))
        missing = c.ensure_fields("ft", 1, ["A", "B", "C"])
        assert missing == []
        assert [f["name"] for f in state["fields"]] == ["A", "B", "C"]
