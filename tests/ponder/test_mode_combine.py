import json
import runpy
from pathlib import Path

import pandas as pd

from ponder import runner
from ponder.mode_combine import combine_audited_runs, latest_audited_run

BACKFILL = runpy.run_path(
    str(Path(__file__).parents[2] / "scripts" / "backfill_rcc_object_mode.py")
)["backfill_file"]


def _run_mode(tmp_path, monkeypatch, mode, object_id):
    mode_root = tmp_path / f"{mode}s"
    monkeypatch.setattr(runner, "WORK_DIR", mode_root / "work")
    monkeypatch.setattr(runner, "RESULTS_DIR", mode_root / "results")

    def fake_run_sorcha(orbits, physparams, output, db, config, timeout=None):
        output.parent.mkdir(parents=True, exist_ok=True)
        row = {"ObjID": object_id, "fieldMJD_TAI": 61000.0, "optFilter": "r"}
        pd.DataFrame([row]).to_csv(output, index=False)
        pd.DataFrame([row]).to_csv(
            output.with_name(f"{output.stem}_ew.csv"), index=False
        )

    monkeypatch.setattr(runner, "run_sorcha", fake_run_sorcha)
    orbits = pd.DataFrame({"ObjID": [object_id], "a": [1.0]})
    physical = pd.DataFrame({"ObjID": [object_id], "H_r": [10.0]})
    completed = runner.run_sorcha_chunks(
        orbits,
        physical,
        "new",
        "2026-08-24T01:02:03Z",
        db="pointings.db",
        config="config.ini",
        chunk_size=10,
        workers=1,
        catalog_rows=pd.DataFrame({"ObjID": [object_id]}),
        context_digest=f"{mode}-context",
        object_mode=mode,
    )

    assert completed is True
    visible = mode_root / "results" / "2026-08-24_job_new.parquet"
    assert visible.exists()
    assert set(pd.read_parquet(visible)["object_mode"]) == {mode}
    manifest_path = next((mode_root / "results" / "chunk_runs").glob("*/manifest.json"))
    assert json.loads(manifest_path.read_text())["object_mode"] == mode
    return mode_root / "results"


def test_separate_mode_roots_combine_to_audited_mode_labelled_pair(
    tmp_path, monkeypatch
):
    asteroid_results = _run_mode(tmp_path, monkeypatch, "asteroid", "A123")
    comet_results = _run_mode(tmp_path, monkeypatch, "comet", "C/2020 U4")

    asteroid_run = latest_audited_run(asteroid_results, "asteroid")
    comet_run = latest_audited_run(comet_results, "comet")
    combined_root = tmp_path / "combined" / "results"
    detections, ephemeris, audit = combine_audited_runs(
        asteroid_run,
        comet_run,
        combined_root,
        run_date="2026-08-24",
    )

    for path in (detections, ephemeris):
        combined = pd.read_parquet(path)
        assert set(combined["object_mode"]) == {"asteroid", "comet"}
        assert set(combined["ObjID"]) == {"A123", "C/2020 U4"}
    audit_df = pd.read_csv(audit)
    assert set(audit_df["status"]) == {"ok"}
    assert set(audit_df["asteroid_rows"]) == {1}
    assert set(audit_df["comet_rows"]) == {1}
    assert (combined_root / "2026-08-24_job_new.parquet").exists()
    assert (combined_root / "2026-08-24_job_new_ew.parquet").exists()


def test_backfill_rcc_object_mode_is_atomic_and_idempotent(tmp_path):
    path = tmp_path / "ponder_results.parquet"
    pd.DataFrame({"ObjID": ["A", "B"]}).to_parquet(path, index=False)

    assert BACKFILL(path, "asteroid", apply=False) == "would_label"
    assert "object_mode" not in pd.read_parquet(path).columns
    assert BACKFILL(path, "asteroid", apply=True) == "labelled"
    assert set(pd.read_parquet(path)["object_mode"]) == {"asteroid"}
    assert BACKFILL(path, "asteroid", apply=True) == "already_labelled"
