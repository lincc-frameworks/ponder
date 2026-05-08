import json

from ponder import chunk_cleanup


def _write_chunk_run(root, run_name, job_name="new"):
    run_dir = root / "results" / run_name
    work_dir = root / "work" / run_name
    run_dir.mkdir(parents=True)
    work_dir.mkdir(parents=True)
    output = run_dir / "chunk_00000_rows_0000000_0000001.csv"
    output.write_text("ObjID,value\nA,1\nB,2\n")
    output.with_name(f"{output.stem}_ew.csv").write_text("ObjID,value\nA,1\nB,2\n")
    output.with_suffix(".done").write_text("{}")
    manifest = {
        "job_name": job_name,
        "row_count": 2,
        "chunk_size": 2,
        "digest": run_name.rsplit("_", 1)[-1],
        "chunks": [
            {
                "index": 0,
                "row_start": 0,
                "row_end": 2,
                "output_path": str(output.relative_to(root)),
            }
        ],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    return run_dir, work_dir


def test_consolidate_chunk_artifacts_promotes_and_archives_complete_run(tmp_path):
    run_name = "2026-05-05_job_new_abcdef123456"
    run_dir, work_dir = _write_chunk_run(tmp_path, run_name)
    log = tmp_path / "results" / "2026-05-05_job_new-2026-05-05-p1-sorcha.log"
    log.write_text("log\n")

    messages = chunk_cleanup.consolidate_chunk_artifacts(tmp_path, apply=True)

    visible = tmp_path / "results" / "2026-05-05_job_new.csv"
    assert visible.read_text() == "ObjID,value\nA,1\nB,2\n"
    assert visible.with_name(f"{visible.stem}_ew.csv").exists()
    assert not run_dir.exists()
    assert not work_dir.exists()
    assert (tmp_path / "results" / "chunk_runs" / run_name / "manifest.json").exists()
    assert (tmp_path / "work" / "chunk_runs" / run_name).exists()
    assert (tmp_path / "results" / "logs" / log.name).exists()
    assert any("hard-linked" in message or "copied" in message for message in messages)


def test_consolidate_chunk_artifacts_dry_run_leaves_files_in_place(tmp_path):
    run_name = "2026-05-05_job_new_abcdef123456"
    run_dir, work_dir = _write_chunk_run(tmp_path, run_name)

    messages = chunk_cleanup.consolidate_chunk_artifacts(tmp_path, apply=False)

    assert not (tmp_path / "results" / "2026-05-05_job_new.csv").exists()
    assert run_dir.exists()
    assert work_dir.exists()
    assert any("would combine" in message for message in messages)
    assert any("would move" in message for message in messages)
