import pytest

from app.google_source import (
    GoogleSourceError,
    build_headers,
    normalize_grid,
    parse_csv_grid,
    parse_sheet_url,
)


class TestParseSheetUrl:
    def test_edit_url_with_gid(self):
        info = parse_sheet_url(
            "https://docs.google.com/spreadsheets/d/ABC123/edit#gid=42"
        )
        assert info == {"spreadsheet_id": "ABC123", "gid": "42"}

    def test_url_without_gid(self):
        info = parse_sheet_url("https://docs.google.com/spreadsheets/d/XYZ/edit")
        assert info == {"spreadsheet_id": "XYZ", "gid": None}

    def test_export_url(self):
        info = parse_sheet_url(
            "https://docs.google.com/spreadsheets/d/ID9/export?format=csv&gid=7"
        )
        assert info["spreadsheet_id"] == "ID9"
        assert info["gid"] == "7"

    def test_invalid_url_raises(self):
        with pytest.raises(GoogleSourceError):
            parse_sheet_url("https://example.com/nope")


class TestNormalizeGrid:
    def test_trims_empty_trailing_rows_and_cols(self):
        grid = normalize_grid(
            [["a", "b", ""], ["1", "2", ""], ["", "", ""], ["", "", ""]]
        )
        assert grid == [["a", "b"], ["1", "2"]]

    def test_pads_ragged_rows(self):
        grid = normalize_grid([["h1", "h2"], ["only"]])
        assert grid == [["h1", "h2"], ["only", ""]]

    def test_all_empty(self):
        assert normalize_grid([["", ""], [""]]) == []

    def test_keeps_interior_empty_rows(self):
        grid = normalize_grid([["h"], ["a"], [""], ["b"]])
        assert grid == [["h"], ["a"], [""], ["b"]]


class TestParseCsvGrid:
    def test_quoted_csv(self):
        text = 'name,note\n"a,b","line\nbreak"\n'
        grid = parse_csv_grid(text)
        assert grid[1] == ["a,b", "line\nbreak"]

    def test_cjk(self):
        grid = parse_csv_grid("品名,价格\n叉车,100\n")
        assert grid == [["品名", "价格"], ["叉车", "100"]]


class TestBuildHeaders:
    def test_empty_cells_named(self):
        assert build_headers(["A", "", "C"]) == ["A", "col_2", "C"]

    def test_duplicates_suffixed(self):
        assert build_headers(["X", "X"]) == ["X", "X_2"]
