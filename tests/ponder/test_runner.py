import gzip
import json

import pandas as pd
import pytest

from ponder import runner


def test_plan_sorcha_chunks_uses_digest_and_row_ranges(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")

    chunks = runner.plan_sorcha_chunks(
        "unchanged_and_new",
        "2026-05-05T01:02:03Z",
        row_count=5,
        chunk_size=2,
        digest="abcdef1234567890",
    )

    assert [(chunk.index, chunk.row_start, chunk.row_end) for chunk in chunks] == [
        (0, 0, 2),
        (1, 2, 4),
        (2, 4, 5),
    ]
    assert "2026-05-05_job_unchanged_and_new_abcdef123456" in str(chunks[0].orbits_path)
    assert chunks[1].output_path.name == "chunk_00001_rows_0000002_0000003.csv"
    assert chunks[1].ew_output_path.name == "chunk_00001_rows_0000002_0000003_ew.csv"


def test_parse_chunk_indices_accepts_indices_and_ranges():
    assert runner.parse_chunk_indices("1,3-5,8") == {1, 3, 4, 5, 8}
    assert runner.parse_chunk_indices(None) is None


def test_chunk_is_complete_requires_marker_and_outputs(tmp_path):
    chunk = runner.SorchaChunk(
        index=0,
        row_start=0,
        row_end=1,
        orbits_path=tmp_path / "chunk_orbits.csv",
        physparams_path=tmp_path / "chunk_physparams.csv",
        output_path=tmp_path / "chunk.csv",
    )

    assert not runner.chunk_is_complete(chunk)

    chunk.output_path.write_text("ObjID\nA\n")
    chunk.ew_output_path.write_text("ObjID\nA\n")
    assert not runner.chunk_is_complete(chunk)

    chunk.done_path.write_text("{}")
    assert runner.chunk_is_complete(chunk)


def test_combine_csv_files_keeps_one_header(tmp_path):
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    output = tmp_path / "combined.csv"
    first.write_text("ObjID,value\nA,1\n")
    second.write_text("ObjID,value\nB,2\n")

    runner.combine_csv_files([first, second], output)

    assert output.read_text() == "ObjID,value\nA,1\nB,2\n"


def test_mpc_catalog_snapshot_gzips_source_and_preserves_row_numbers(tmp_path):
    catalog_path = tmp_path / "mpc.json"
    objects = [{"Principal_desig": "A"}, {"Principal_desig": "B"}]
    catalog_path.write_text(json.dumps(objects))

    snapshot_path = runner.write_mpc_catalog_snapshot(
        catalog_path,
        tmp_path / "results",
        "2026-05-05T01:02:03Z",
    )

    assert snapshot_path.parent == tmp_path / "results" / "catalogs"
    assert snapshot_path.name.startswith("2026-05-05_mpc_")
    with gzip.open(snapshot_path, "rt") as file:
        assert json.load(file) == objects

    loaded = runner.annotate_catalog_row_numbers(runner.read_json_catalog(snapshot_path))
    assert [obj[runner.CATALOG_ROW_COLUMN] for obj in loaded] == [0, 1]


def test_audit_combined_outputs_reports_missing_object_timestamps(tmp_path):
    chunk = runner.SorchaChunk(
        index=0,
        row_start=0,
        row_end=2,
        orbits_path=tmp_path / "work" / "chunk_orbits.csv",
        physparams_path=tmp_path / "work" / "chunk_physparams.csv",
        output_path=tmp_path / "results" / "job" / "chunk.csv",
    )
    chunk.output_path.parent.mkdir(parents=True)
    chunk.output_path.write_text("ObjID,fieldMJD_TAI,value\nA,1,x\nB,2,y\nB,2,z\n")
    chunk.ew_output_path.write_text("ObjID,fieldMJD_TAI\nA,1\nB,2\n")

    final_output = tmp_path / "results" / "2026-05-05_job_test.csv"
    final_output.write_text("ObjID,fieldMJD_TAI,value\nA,1,x\nB,2,y\n")
    final_output.with_name(f"{final_output.stem}_ew.csv").write_text("ObjID,fieldMJD_TAI\nA,1\nB,2\n")
    catalog_rows = pd.DataFrame(
        {
            "Principal_desig": ["A", "B"],
            runner.CATALOG_ROW_COLUMN: [10, 11],
        }
    )

    audit_path, missing_path, missing_rows = runner.audit_combined_outputs(
        [chunk],
        final_output,
        catalog_rows,
    )

    assert missing_rows == 1
    summary = pd.read_csv(audit_path)
    assert summary["status"].tolist() == ["missing", "ok"]
    assert summary["missing_rows"].tolist() == [1, 0]

    missing = pd.read_csv(missing_path)
    assert missing["output_name"].tolist() == ["detections"]
    assert missing["object_id"].tolist() == ["B"]
    assert missing["timestamp"].astype(str).tolist() == ["2"]
    assert missing["source_count"].tolist() == [2]
    assert missing["combined_count"].tolist() == [1]
    assert missing["missing_count"].tolist() == [1]
    assert missing[runner.CATALOG_ROW_COLUMN].tolist() == [11]


def test_run_sorcha_chunks_resumes_completed_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")
    calls = []

    def fake_run_sorcha(orbits, physparams, output, db, config, timeout=None):
        calls.append(output.name)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("ObjID\nA\n")
        output.with_name(f"{output.stem}_ew.csv").write_text("ObjID\nA\n")

    monkeypatch.setattr(runner, "run_sorcha", fake_run_sorcha)
    orbs = pd.DataFrame({"ObjID": ["A", "B", "C"], "a": [1.0, 2.0, 3.0]})
    phys = pd.DataFrame({"ObjID": ["A", "B", "C"], "H_r": [10.0, 11.0, 12.0]})

    runner.run_sorcha_chunks(
        orbs,
        phys,
        "unchanged_and_new",
        "2026-05-05T01:02:03Z",
        db="pointings.db",
        config="config.ini",
        chunk_size=2,
        workers=1,
    )
    assert len(calls) == 2

    runner.run_sorcha_chunks(
        orbs,
        phys,
        "unchanged_and_new",
        "2026-05-05T01:02:03Z",
        db="pointings.db",
        config="config.ini",
        chunk_size=2,
        workers=1,
    )
    assert len(calls) == 2
    assert (tmp_path / "results" / "2026-05-05_job_unchanged_and_new.csv").exists()


def test_run_sorcha_chunks_can_run_only_selected_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")
    calls = []

    def fake_run_sorcha(orbits, physparams, output, db, config, timeout=None):
        calls.append(output.name)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("ObjID\nA\n")
        output.with_name(f"{output.stem}_ew.csv").write_text("ObjID\nA\n")

    monkeypatch.setattr(runner, "run_sorcha", fake_run_sorcha)
    orbs = pd.DataFrame({"ObjID": ["A", "B", "C", "D"], "a": [1.0, 2.0, 3.0, 4.0]})
    phys = pd.DataFrame({"ObjID": ["A", "B", "C", "D"], "H_r": [10.0, 11.0, 12.0, 13.0]})

    completed = runner.run_sorcha_chunks(
        orbs,
        phys,
        "unchanged_and_new",
        "2026-05-05T01:02:03Z",
        db="pointings.db",
        config="config.ini",
        chunk_size=2,
        workers=1,
        only_chunks={1},
    )

    assert completed is False
    assert len(calls) == 1
    assert calls[0].startswith("chunk_00001")
    assert not (tmp_path / "results" / "2026-05-05_job_unchanged_and_new.csv").exists()


def test_read_ignore_ids_and_filter_objects(tmp_path):
    ignore_path = tmp_path / "ignore.txt"
    ignore_path.write_text("B\n# comment\nC,D\n")
    objects = [
        {"Principal_desig": "A"},
        {"Principal_desig": "B"},
        {"Principal_desig": "C"},
        {"Principal_desig": "D"},
        {"Principal_desig": "E"},
    ]

    ignore_ids = runner.read_ignore_ids(ignore_path, ["E"])
    kept = runner.filter_ignored_objects(objects, ignore_ids)

    assert [obj["Principal_desig"] for obj in kept] == ["A"]


def test_failed_chunks_write_summary_and_catalog_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")

    def fake_run_sorcha(orbits, physparams, output, db, config, timeout=None):
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.name.startswith("chunk_00001_rows"):
            raise TimeoutError("parent timeout")
        output.write_text("ObjID\nA\n")
        output.with_name(f"{output.stem}_ew.csv").write_text("ObjID\nA\n")

    monkeypatch.setattr(runner, "run_sorcha", fake_run_sorcha)
    orbs = pd.DataFrame({"ObjID": ["A", "B", "C", "D"], "a": [1.0, 2.0, 3.0, 4.0]})
    phys = pd.DataFrame({"ObjID": ["A", "B", "C", "D"], "H_r": [10.0, 11.0, 12.0, 13.0]})
    catalog_rows = pd.DataFrame(
        {
            "Principal_desig": ["A", "B", "C", "D"],
            "Arc_length": [10, 10, 2, 2],
            "U": [1, 1, 9, 9],
        }
    )

    with pytest.raises(RuntimeError, match="chunk 00001"):
        runner.run_sorcha_chunks(
            orbs,
            phys,
            "unchanged_and_new",
            "2026-05-05T01:02:03Z",
            db="pointings.db",
            config="config.ini",
            chunk_size=2,
            workers=1,
            catalog_rows=catalog_rows,
        )

    failure_paths = list((tmp_path / "results").glob("*/failures.csv"))
    catalog_paths = list((tmp_path / "results").glob("*/failed_catalog_rows.csv"))
    assert len(failure_paths) == 1
    assert len(catalog_paths) == 1

    failures = pd.read_csv(failure_paths[0])
    failed_catalog = pd.read_csv(catalog_paths[0])
    assert failures.loc[0, "chunk"] == 1
    assert "parent timeout" in failures.loc[0, "error"]
    assert failed_catalog["chunk"].tolist() == [1, 1]
    assert failed_catalog["Principal_desig"].tolist() == ["C", "D"]
    assert "Arc_length" in failed_catalog.columns


def test_failed_chunks_can_run_debug_subchunks(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")

    def fake_run_sorcha(orbits, physparams, output, db, config, timeout=None):
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.name.startswith("chunk_00001_rows") or "debug_0001" in output.name:
            raise TimeoutError("timeout")
        output.write_text("ObjID\nA\n")
        output.with_name(f"{output.stem}_ew.csv").write_text("ObjID\nA\n")

    monkeypatch.setattr(runner, "run_sorcha", fake_run_sorcha)
    orbs = pd.DataFrame({"ObjID": ["A", "B", "C", "D"], "a": [1.0, 2.0, 3.0, 4.0]})
    phys = pd.DataFrame({"ObjID": ["A", "B", "C", "D"], "H_r": [10.0, 11.0, 12.0, 13.0]})
    catalog_rows = pd.DataFrame({"Principal_desig": ["A", "B", "C", "D"]})

    with pytest.raises(RuntimeError, match="chunk 00001"):
        runner.run_sorcha_chunks(
            orbs,
            phys,
            "unchanged_and_new",
            "2026-05-05T01:02:03Z",
            db="pointings.db",
            config="config.ini",
            chunk_size=2,
            workers=1,
            catalog_rows=catalog_rows,
            debug_failed_chunk_size=1,
        )

    report_paths = list((tmp_path / "results").glob("*/debug/subchunk_debug_report.csv"))
    catalog_paths = list((tmp_path / "results").glob("*/debug/failed_subchunk_catalog_rows.csv"))
    assert len(report_paths) == 1
    assert len(catalog_paths) == 1

    report = pd.read_csv(report_paths[0])
    failed_catalog = pd.read_csv(catalog_paths[0])
    assert report["status"].tolist() == ["done", "failed"]
    assert failed_catalog["parent_chunk"].tolist() == [1]
    assert failed_catalog["subchunk"].tolist() == [1]
    assert failed_catalog["Principal_desig"].tolist() == ["D"]


def test_force_debug_chunking_bypasses_parent_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")
    calls = []

    def fake_run_sorcha(orbits, physparams, output, db, config, timeout=None):
        calls.append(output.name)
        assert "debug" in str(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("ObjID\nA\n")
        output.with_name(f"{output.stem}_ew.csv").write_text("ObjID\nA\n")

    monkeypatch.setattr(runner, "run_sorcha", fake_run_sorcha)
    orbs = pd.DataFrame({"ObjID": ["A", "B", "C", "D"]})
    phys = pd.DataFrame({"ObjID": ["A", "B", "C", "D"]})
    catalog_rows = pd.DataFrame({"Principal_desig": ["A", "B", "C", "D"]})

    completed = runner.run_sorcha_chunks(
        orbs,
        phys,
        "unchanged_and_new",
        "2026-05-05T01:02:03Z",
        db="pointings.db",
        config="config.ini",
        chunk_size=2,
        workers=1,
        only_chunks={1},
        catalog_rows=catalog_rows,
        debug_failed_chunk_size=1,
        force_debug_chunking=True,
    )

    assert completed is False
    assert len(calls) == 2
    assert all("debug" in name for name in calls)


def test_force_debug_chunking_requires_selected_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")
    orbs = pd.DataFrame({"ObjID": ["A", "B"]})
    phys = pd.DataFrame({"ObjID": ["A", "B"]})

    with pytest.raises(ValueError, match="selected chunks"):
        runner.run_sorcha_chunks(
            orbs,
            phys,
            "unchanged_and_new",
            "2026-05-05T01:02:03Z",
            db="pointings.db",
            config="config.ini",
            chunk_size=2,
            workers=1,
            debug_failed_chunk_size=1,
            force_debug_chunking=True,
        )


def test_isolate_failing_rows_finds_single_bad_row(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")

    def fake_run_sorcha(orbits, physparams, output, db, config, timeout=None):
        input_rows = pd.read_csv(orbits)
        output.parent.mkdir(parents=True, exist_ok=True)
        if "C" in input_rows["ObjID"].tolist():
            raise TimeoutError("bad row")
        output.write_text("ObjID\nA\n")
        output.with_name(f"{output.stem}_ew.csv").write_text("ObjID\nA\n")

    monkeypatch.setattr(runner, "run_sorcha", fake_run_sorcha)
    orbs = pd.DataFrame({"ObjID": ["A", "B", "C", "D"]})
    phys = pd.DataFrame({"ObjID": ["A", "B", "C", "D"]})
    catalog_rows = pd.DataFrame({"Principal_desig": ["A", "B", "C", "D"]})

    runner.run_sorcha_chunks(
        orbs,
        phys,
        "unchanged_and_new",
        "2026-05-05T01:02:03Z",
        db="pointings.db",
        config="config.ini",
        chunk_size=4,
        workers=1,
        only_chunks={0},
        catalog_rows=catalog_rows,
        debug_failed_chunk_size=2,
        force_debug_chunking=True,
        isolate_failing_rows=True,
    )

    debug_dir = next((tmp_path / "results").glob("*/debug"))
    isolation = pd.read_csv(debug_dir / "isolation_report.csv")
    failing_rows = pd.read_csv(debug_dir / "failing_rows.csv")
    timing = pd.read_csv(debug_dir / "debug_timing_summary.csv")

    assert isolation["debug_level"].tolist() == [1, 1, 2, 2]
    assert failing_rows["Principal_desig"].tolist() == ["C"]
    assert failing_rows["failure_input_row"].tolist() == [2]
    assert failing_rows["failure_row_count"].tolist() == [1]
    for column in ["started_at", "completed_at", "duration_seconds", "sorcha_seconds", "resumed"]:
        assert column in isolation.columns
    assert {"debug_level", "row_count", "mean_sorcha_seconds"}.issubset(timing.columns)


def test_isolate_failing_rows_resumes_failed_debug_ranges(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")

    def first_run_sorcha(orbits, physparams, output, db, config, timeout=None):
        input_rows = pd.read_csv(orbits)
        output.parent.mkdir(parents=True, exist_ok=True)
        if "C" in input_rows["ObjID"].tolist():
            raise TimeoutError("bad row")
        output.write_text("ObjID\nA\n")
        output.with_name(f"{output.stem}_ew.csv").write_text("ObjID\nA\n")

    monkeypatch.setattr(runner, "run_sorcha", first_run_sorcha)
    orbs = pd.DataFrame({"ObjID": ["A", "B", "C", "D"]})
    phys = pd.DataFrame({"ObjID": ["A", "B", "C", "D"]})
    catalog_rows = pd.DataFrame({"Principal_desig": ["A", "B", "C", "D"]})

    runner.run_sorcha_chunks(
        orbs,
        phys,
        "unchanged_and_new",
        "2026-05-05T01:02:03Z",
        db="pointings.db",
        config="config.ini",
        chunk_size=4,
        workers=1,
        only_chunks={0},
        catalog_rows=catalog_rows,
        debug_failed_chunk_size=2,
        force_debug_chunking=True,
    )

    second_run_calls = []

    def second_run_sorcha(orbits, physparams, output, db, config, timeout=None):
        second_run_calls.append(output.name)
        assert "chunk_00000_debug_0001" not in output.name
        input_rows = pd.read_csv(orbits)
        output.parent.mkdir(parents=True, exist_ok=True)
        if "C" in input_rows["ObjID"].tolist():
            raise TimeoutError("bad row")
        output.write_text("ObjID\nA\n")
        output.with_name(f"{output.stem}_ew.csv").write_text("ObjID\nA\n")

    monkeypatch.setattr(runner, "run_sorcha", second_run_sorcha)
    runner.run_sorcha_chunks(
        orbs,
        phys,
        "unchanged_and_new",
        "2026-05-05T01:02:03Z",
        db="pointings.db",
        config="config.ini",
        chunk_size=4,
        workers=1,
        only_chunks={0},
        catalog_rows=catalog_rows,
        debug_failed_chunk_size=2,
        force_debug_chunking=True,
        isolate_failing_rows=True,
    )

    debug_dir = next((tmp_path / "results").glob("*/debug"))
    failing_rows = pd.read_csv(debug_dir / "failing_rows.csv")
    assert failing_rows["Principal_desig"].tolist() == ["C"]
    assert any("isolate_l02" in name for name in second_run_calls)


def test_isolate_failing_rows_reports_multiple_bad_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")

    def fake_run_sorcha(orbits, physparams, output, db, config, timeout=None):
        input_rows = pd.read_csv(orbits)
        output.parent.mkdir(parents=True, exist_ok=True)
        if set(input_rows["ObjID"]) & {"C", "D"}:
            raise TimeoutError("bad row")
        output.write_text("ObjID\nA\n")
        output.with_name(f"{output.stem}_ew.csv").write_text("ObjID\nA\n")

    monkeypatch.setattr(runner, "run_sorcha", fake_run_sorcha)
    orbs = pd.DataFrame({"ObjID": ["A", "B", "C", "D"]})
    phys = pd.DataFrame({"ObjID": ["A", "B", "C", "D"]})
    catalog_rows = pd.DataFrame({"Principal_desig": ["A", "B", "C", "D"]})

    runner.run_sorcha_chunks(
        orbs,
        phys,
        "unchanged_and_new",
        "2026-05-05T01:02:03Z",
        db="pointings.db",
        config="config.ini",
        chunk_size=4,
        workers=1,
        only_chunks={0},
        catalog_rows=catalog_rows,
        debug_failed_chunk_size=2,
        force_debug_chunking=True,
        isolate_failing_rows=True,
    )

    debug_dir = next((tmp_path / "results").glob("*/debug"))
    failing_rows = pd.read_csv(debug_dir / "failing_rows.csv")
    assert failing_rows["Principal_desig"].tolist() == ["C", "D"]


def test_isolate_failing_rows_reports_group_only_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")

    def fake_run_sorcha(orbits, physparams, output, db, config, timeout=None):
        input_rows = pd.read_csv(orbits)
        output.parent.mkdir(parents=True, exist_ok=True)
        if len(input_rows) == 2 and "C" in input_rows["ObjID"].tolist():
            raise TimeoutError("group-only")
        output.write_text("ObjID\nA\n")
        output.with_name(f"{output.stem}_ew.csv").write_text("ObjID\nA\n")

    monkeypatch.setattr(runner, "run_sorcha", fake_run_sorcha)
    orbs = pd.DataFrame({"ObjID": ["A", "B", "C", "D"]})
    phys = pd.DataFrame({"ObjID": ["A", "B", "C", "D"]})
    catalog_rows = pd.DataFrame({"Principal_desig": ["A", "B", "C", "D"]})

    runner.run_sorcha_chunks(
        orbs,
        phys,
        "unchanged_and_new",
        "2026-05-05T01:02:03Z",
        db="pointings.db",
        config="config.ini",
        chunk_size=4,
        workers=1,
        only_chunks={0},
        catalog_rows=catalog_rows,
        debug_failed_chunk_size=2,
        force_debug_chunking=True,
        isolate_failing_rows=True,
    )

    debug_dir = next((tmp_path / "results").glob("*/debug"))
    group_failures = pd.read_csv(debug_dir / "group_failures.csv")
    group_catalog_rows = pd.read_csv(debug_dir / "group_failure_catalog_rows.csv")
    failing_rows = pd.read_csv(debug_dir / "failing_rows.csv")

    assert group_failures["failure_type"].tolist() == ["group_failed_children_passed"]
    assert group_failures["row_start"].tolist() == [2]
    assert group_failures["row_end"].tolist() == [4]
    assert group_catalog_rows["group_failure_failure_type"].tolist() == [
        "group_failed_children_passed",
        "group_failed_children_passed",
    ]
    assert group_catalog_rows["group_failure_input_row"].tolist() == [2, 3]
    assert group_catalog_rows["Principal_desig"].tolist() == ["C", "D"]
    assert failing_rows.empty
