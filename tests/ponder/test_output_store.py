import pandas as pd
import pytest

from ponder import output_store, runner

CONTEXT = {"db": "pointings.db", "comet": False, "config_sha256": "test"}


def update(paths, output, context=CONTEXT):
    output_store.upsert_observations(
        paths, output, context, runner.OUTPUT_ID_COLUMNS, runner.OUTPUT_TIMESTAMP_COLUMNS
    )


def write_rows(path, rows):
    pd.DataFrame(rows, columns=["ObjID", "fieldMJD_TAI", "FieldID", "value"]).to_parquet(path, index=False)
    return path


def test_upsert_preserves_visits_multiplicity_and_lexical_ids(tmp_path, monkeypatch):
    first = write_rows(
        tmp_path / "first.parquet",
        [("001", 2.0, 11, "old"), ("001", 2.0, 12, "other visit"), ("1", 1.0, 10, "other object")],
    )
    master = tmp_path / "nested" / "master.parquet"
    update([first], master)
    second = write_rows(
        tmp_path / "second.parquet",
        [("001", 2.0, 11, "new a"), ("001", 2.0, 11, "new b"), ("B", 3.0, 13, "new object")],
    )
    # The update must not materialize the old master or new products in pandas.
    with monkeypatch.context() as patch:
        patch.setattr(pd, "read_parquet", lambda *a, **k: pytest.fail("unbounded pandas read"))
        update([second], master)
        update([second], master)
    actual = pd.read_parquet(master)
    assert len(actual) == 5
    assert actual.fieldMJD_TAI.tolist() == sorted(actual.fieldMJD_TAI)
    assert set(actual.value) == {"other object", "other visit", "new a", "new b", "new object"}
    assert set(actual.ObjID) == {"001", "1", "B"}
    output_store.validate_observation_context(master, CONTEXT)


def test_zero_output_and_no_jobs_leave_master_untouched(tmp_path):
    empty = tmp_path / "empty.parquet"
    pd.DataFrame().to_parquet(empty)
    master = tmp_path / "master.parquet"
    update([], master)
    update([empty], master)
    assert not master.exists()
    first = write_rows(tmp_path / "first.parquet", [("A", 1.0, 1, "old")])
    update([first], master)
    before = master.read_bytes()
    update([empty], master)
    assert master.read_bytes() == before


@pytest.mark.parametrize("failure", ["missing", "schema", "null", "nonfinite", "context", "replace"])
def test_failed_update_preserves_master_and_cleans_temporary_files(tmp_path, monkeypatch, failure):
    first = write_rows(tmp_path / "first.parquet", [("A", 1.0, 1, "old")])
    master = tmp_path / "master.parquet"
    update([first], master)
    before = master.read_bytes()
    incoming = tmp_path / "incoming.parquet"
    context = CONTEXT
    if failure == "schema":
        pd.DataFrame({"ObjID": ["A"], "MJD": [1.0]}).to_parquet(incoming)
    elif failure != "missing":
        obj = None if failure == "null" else "A"
        time = float("inf") if failure == "nonfinite" else 1.0
        write_rows(incoming, [(obj, time, 1, "new")])
    if failure == "context":
        context = {**CONTEXT, "comet": True}
    if failure == "replace":

        def fail_replace(*args):
            raise OSError("simulated interruption before promotion")

        monkeypatch.setattr(output_store.os, "replace", fail_replace)
    with pytest.raises((OSError, ValueError)):
        update([incoming], master, context)
    assert master.read_bytes() == before
    assert not master.with_name(master.name + ".tmp").exists()
    assert not master.with_name(master.name + ".lock").exists()
    assert not list(tmp_path.glob("ponder-output-*"))


def test_concurrent_writer_fails_without_removing_other_lock(tmp_path):
    master = tmp_path / "master.parquet"
    lock = tmp_path / "master.parquet.lock"
    lock.mkdir()
    with pytest.raises(RuntimeError, match="locked"):
        update([], master)
    assert lock.is_dir()


def test_rejects_master_without_context(tmp_path):
    master = write_rows(tmp_path / "master.parquet", [("A", 1.0, 1, "old")])
    with pytest.raises(ValueError, match="context"):
        update([], master)


def test_union_preserves_additional_columns(tmp_path):
    master = tmp_path / "master.parquet"
    first = write_rows(tmp_path / "first.parquet", [("A", 1.0, 1, "old")])
    update([first], master)
    incoming = tmp_path / "incoming.parquet"
    pd.DataFrame({"ObjID": ["B"], "fieldMJD_TAI": [2.0], "FieldID": [2], "extra": [42]}).to_parquet(
        incoming, index=False
    )
    update([incoming], master)
    result = pd.read_parquet(master)
    assert result.columns.isin(["ObjID", "fieldMJD_TAI", "FieldID", "value", "extra"]).all()
    assert result.loc[result.ObjID == "B", "extra"].item() == 42
    assert result.loc[result.ObjID == "A", "value"].item() == "old"
