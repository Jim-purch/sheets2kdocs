from app.sync_engine import apply_mapping, coerce, compute_plan


def _rec(rid, fields):
    return {"id": rid, "fields": fields}


class TestCoerce:
    def test_disabled_keeps_strings(self):
        assert coerce("123", False) == "123"
        assert coerce("1.5", False) == "1.5"

    def test_enabled_converts(self):
        assert coerce("123", True) == 123
        assert coerce("-4", True) == -4
        assert coerce("1.5", True) == 1.5

    def test_leading_zero_preserved(self):
        assert coerce("007", True) == "007"
        assert coerce("0", True) == 0  # plain zero is safe
        assert coerce("0123", True) == "0123"

    def test_non_numeric_untouched(self):
        assert coerce("abc", True) == "abc"
        assert coerce("1.2.3", True) == "1.2.3"
        assert coerce("", True) == ""


class TestApplyMapping:
    def test_identity_when_empty(self):
        row = {"A": "1", "B": "2"}
        assert apply_mapping(row, {}) == {"A": "1", "B": "2"}

    def test_renames_and_skips(self):
        row = {"名称": "x", "价格": "9", "备注": "y"}
        mapping = {"名称": "Name", "价格": "Price", "备注": ""}
        assert apply_mapping(row, mapping) == {"Name": "x", "Price": "9"}

    def test_missing_source_ignored(self):
        assert apply_mapping({"A": "1"}, {"B": "B"}) == {}


class TestComputePlanReplace:
    def test_replace_creates_and_deletes_all(self):
        src = [{"A": "1"}, {"A": "2"}]
        target = [_rec("r1", {"A": "9"})]
        plan = compute_plan(src, target, "replace", "")
        assert plan.creates == [{"A": "1"}, {"A": "2"}]
        assert plan.deletes == ["r1"]
        assert plan.updates == []


class TestComputePlanUpsert:
    def test_create_update_delete(self):
        src = [
            {"Key": "A", "Val": "1"},   # changed (target Val=99)
            {"Key": "B", "Val": "2"},   # unchanged
            {"Key": "C", "Val": "3"},   # new
        ]
        target = [
            _rec("r1", {"Key": "A", "Val": "99"}),
            _rec("r2", {"Key": "B", "Val": "2"}),
            _rec("r4", {"Key": "Z", "Val": "gone"}),  # to delete
        ]
        plan = compute_plan(src, target, "upsert", "Key")
        assert plan.creates == [{"Key": "C", "Val": "3"}]
        assert plan.updates == [{"id": "r1", "fields": {"Val": "1"}}]
        assert plan.deletes == ["r4"]
        assert plan.unchanged == 1

    def test_number_vs_string_equal(self):
        src = [{"Key": "A", "Num": 5}]
        target = [_rec("r1", {"Key": "A", "Num": "5"})]
        plan = compute_plan(src, target, "upsert", "Key")
        assert plan.updates == []
        assert plan.unchanged == 1

    def test_missing_key_created(self):
        src = [{"Key": "", "Val": "x"}, {"Key": "A", "Val": "1"}]
        target = [_rec("r1", {"Key": "A", "Val": "1"})]
        plan = compute_plan(src, target, "upsert", "Key")
        assert len(plan.creates) == 1
        assert plan.creates[0]["Val"] == "x"
        assert plan.updates == []
        assert plan.deletes == []

    def test_requires_key_field(self):
        import pytest

        from app.sync_engine import SyncError

        with pytest.raises(SyncError):
            compute_plan([{"A": "1"}], [], "upsert", "")

    def test_new_field_on_existing_record_updated(self):
        src = [{"Key": "A", "Extra": "new"}]
        target = [_rec("r1", {"Key": "A"})]
        plan = compute_plan(src, target, "upsert", "Key")
        assert plan.updates == [{"id": "r1", "fields": {"Extra": "new"}}]
