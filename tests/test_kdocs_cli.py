import json
import subprocess
from unittest.mock import patch

import pytest

from app.kdocs_cli import KdocsCliClient, KdocsCliError


def cli_response(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps(payload), stderr=""
    )


class TestRun:
    def _client(self) -> KdocsCliClient:
        return KdocsCliClient(token="tok", cli_path="/bin/kdocs-cli")

    def test_ok_detail_wrapper(self):
        c = self._client()
        with patch("app.kdocs_cli.subprocess.run") as run:
            run.return_value = cli_response(
                {"code": 0, "data": {"detail": {"records": [{"id": "A"}], "offset": ""}}}
            )
            out = c._run(["dbsheet", "list-records", "{}"])
        assert out == {"records": [{"id": "A"}], "offset": ""}
        cmd = run.call_args.args[0]
        assert cmd[:2] == ["/bin/kdocs-cli", "dbsheet"]
        assert cmd[-1] == "json"
        assert cmd[cmd.index("--token") + 1] == "tok"

    def test_nested_data_wrapper(self):
        c = self._client()
        with patch("app.kdocs_cli.subprocess.run") as run:
            run.return_value = cli_response(
                {"code": 0, "data": {"data": {"sheets": [1]}}}
            )
            out = c._run(["dbsheet", "get-schema", "{}"])
        assert out == {"sheets": [1]}

    def test_business_error(self):
        c = self._client()
        with patch("app.kdocs_cli.subprocess.run") as run:
            run.return_value = cli_response({"code": 403001, "message": "企业账户限制"})
            with pytest.raises(KdocsCliError, match="403001"):
                c._run(["drive", "list-my-files", "{}"])

    def test_nonzero_exit(self):
        c = self._client()
        with patch("app.kdocs_cli.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="boom"
            )
            with pytest.raises(KdocsCliError, match="boom"):
                c._run(["x", "y", "{}"])

    def test_cli_missing(self):
        c = self._client()
        with patch(
            "app.kdocs_cli.subprocess.run", side_effect=FileNotFoundError()
        ):
            with pytest.raises(KdocsCliError, match="未安装"):
                c._run(["x", "y", "{}"])

    def test_no_token_flag_when_empty(self):
        c = KdocsCliClient(token="", cli_path="/bin/kdocs-cli")
        with patch("app.kdocs_cli.subprocess.run") as run:
            run.return_value = cli_response({"code": 0, "data": {}})
            c._run(["drive", "list-my-files", "{}"])
        cmd = run.call_args.args[0]
        assert "--token" not in cmd


class TestRecords:
    def _client(self) -> KdocsCliClient:
        return KdocsCliClient(token="tok", cli_path="/bin/kdocs-cli")

    def test_query_all_records_pages(self):
        c = self._client()
        pages = [
            {"records": [{"id": "1"}], "offset": "p2"},
            {"records": [{"id": "2"}], "offset": ""},
        ]
        seen = []

        def fake_run(args, **kw):
            params = json.loads(args[3])
            seen.append(params)
            idx = 0 if len(seen) == 1 else 1
            return cli_response({"code": 0, "data": {"detail": pages[idx]}})

        with patch("app.kdocs_cli.subprocess.run", side_effect=fake_run):
            records = c.query_all_records("FT", 3)
        assert [r["id"] for r in records] == ["1", "2"]
        assert seen[0].get("offset", "") == ""
        assert seen[1]["offset"] == "p2"
        assert seen[0]["sheet_id"] == 3 and seen[0]["page_size"] == 100

    def test_create_records_chunked_payload(self):
        c = self._client()
        payloads = []

        def fake_run(args, **kw):
            payloads.append(json.loads(args[3]))
            return cli_response({"code": 0, "data": {"detail": {}}})

        with patch("app.kdocs_cli.subprocess.run", side_effect=fake_run):
            count = c.create_records("FT", 3, [{"A": i} for i in range(450)])
        assert count == 450
        assert [len(p["records"]) for p in payloads] == [200, 200, 50]
        assert payloads[0]["records"][0] == {"fields": {"A": 0}}

    def test_delete_records_object_array(self):
        c = self._client()
        payloads = []

        def fake_run(args, **kw):
            payloads.append(json.loads(args[3]))
            return cli_response({"code": 0, "data": {"detail": {}}})

        with patch("app.kdocs_cli.subprocess.run", side_effect=fake_run):
            c.delete_records("FT", 3, ["x", "y"])
        # records must be [{id:..}, ...] — NOT string array
        assert payloads[0]["records"] == [{"id": "x"}, {"id": "y"}]

    def test_update_records_payload(self):
        c = self._client()
        payloads = []

        def fake_run(args, **kw):
            payloads.append(json.loads(args[3]))
            return cli_response({"code": 0, "data": {"detail": {}}})

        with patch("app.kdocs_cli.subprocess.run", side_effect=fake_run):
            c.update_records("FT", 3, [{"id": "r1", "fields": {"A": 1}}])
        assert payloads[0]["records"] == [{"id": "r1", "fields": {"A": 1}}]


class TestSchema:
    def _client(self) -> KdocsCliClient:
        return KdocsCliClient(token="tok", cli_path="/bin/kdocs-cli")

    def test_get_fields_matches_sheet(self):
        c = self._client()
        schema = {"sheets": [{"id": 1, "fields": [{"name": "A"}]}, {"id": 7, "fields": [{"name": "Z"}]}]}

        with patch("app.kdocs_cli.subprocess.run") as run:
            run.return_value = cli_response({"code": 0, "data": {"data": schema}})
            assert [f["name"] for f in c.get_fields("FT", 7)] == ["Z"]
            # string sheet_id also matches
            assert [f["name"] for f in c.get_fields("FT", "1")] == ["A"]

    def test_list_sheets_marks_db(self):
        c = self._client()
        schema = {"sheets": [{"id": 1, "name": "报价表", "fields": []}]}
        with patch("app.kdocs_cli.subprocess.run") as run:
            run.return_value = cli_response({"code": 0, "data": {"data": schema}})
            sheets = c.list_sheets("FT")
        assert sheets == [{"sheet_id": 1, "name": "报价表", "type": "db", "idx": 0}]

    def test_ensure_fields_creates_missing(self):
        c = self._client()
        state = {"fields": [{"name": "A"}]}
        actions = []

        def fake_run(args, **kw):
            action = args[2]
            params = json.loads(args[3]) if len(args) > 3 else {}
            actions.append(action)
            if action == "get-schema":
                return cli_response(
                    {"code": 0, "data": {"data": {"sheets": [{"id": 3, "fields": state["fields"]}]}}}
                )
            if action == "create-fields":
                state["fields"].extend(params["fields"])
            return cli_response({"code": 0, "data": {"detail": {}}})

        with patch("app.kdocs_cli.subprocess.run", side_effect=fake_run):
            missing = c.ensure_fields("FT", 3, ["A", "B"])
        assert missing == []
        assert "create-fields" in actions
        assert [f["name"] for f in state["fields"]] == ["A", "B"]


class TestTokenStore:
    def _setup_db(self, tmp_path):
        from app import db as appdb

        appdb.close_thread_conn()
        appdb.init_db(tmp_path / "t.db")
        return appdb

    def test_setting_roundtrip(self, tmp_path):
        db = self._setup_db(tmp_path)
        from app.kdocs_cli import get_setting, set_setting

        assert get_setting("cli_token") == ""
        set_setting("cli_token", "abc")
        assert get_setting("cli_token") == "abc"
        set_setting("cli_token", "xyz")  # upsert
        assert get_setting("cli_token") == "xyz"
        db.close_thread_conn()

    def test_resolve_token_db_over_env(self, tmp_path, monkeypatch):
        db = self._setup_db(tmp_path)
        from app.kdocs_cli import invalidate_token_cache, resolve_token, set_setting

        monkeypatch.setenv("KDOCS_CLI_TOKEN", "env-token")
        invalidate_token_cache()
        assert resolve_token() == "env-token"
        set_setting("cli_token", "db-token")
        invalidate_token_cache()
        assert resolve_token() == "db-token"
        db.close_thread_conn()
