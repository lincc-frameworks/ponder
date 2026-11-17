import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from ponder import output_store, runner


@pytest.fixture
def cycle(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")
    db = tmp_path / "pointings.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE observations (observationId INTEGER, observationStartMJD REAL)")
        con.execute("INSERT INTO observations VALUES (1, 10.0)")
    catalog, config = tmp_path / "objects.json", tmp_path / "config.ini"
    catalog.write_text(
        json.dumps(
            [
                {
                    "Principal_desig": "001",
                    "a": 2,
                    "e": 0.1,
                    "i": 1,
                    "Node": 2,
                    "Peri": 3,
                    "M": 4,
                    "Epoch": 2461000.5,
                    "H": 15,
                    "U": 0,
                }
            ]
        )
    )
    config.write_text("[INPUT]\n")

    def fake_sorcha(orbits, physparams, output, db, config, timeout=None):
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        # A detection-free run can still have geometrically eligible ephemeris rows.
        output.write_text("ObjID,fieldMJD_TAI\n")
        output.with_name(output.stem + "_ew.csv").write_text("ObjID,fieldMJD_TAI,FieldID\n001,10,1\n")

    monkeypatch.setattr(runner, "run_sorcha", fake_sorcha)
    return db, catalog, config


@pytest.mark.parametrize("chunk_size", [0, 10])
def test_custom_master_path_and_repeat_cycle(cycle, tmp_path, chunk_size):
    master = tmp_path / "custom" / "observations.parquet"
    runner.run_ponder(*cycle, comet=False, main_orbit_file=master, chunk_size=chunk_size)
    rows = pd.read_parquet(master)
    assert rows.ObjID.tolist() == ["001"]
    assert not (tmp_path / "results" / "main_observations.parquet").exists()
    before = master.read_bytes()
    runner.run_ponder(*cycle, comet=False, main_orbit_file=master, chunk_size=chunk_size)
    assert master.read_bytes() == before
    state, hashes = runner.state_files_for_run(cycle[0], False)
    assert json.loads(state.read_text())["last_mjd"] == 10
    assert "001" in json.loads(hashes.read_text())


def test_existing_python_api_does_not_require_consolidation(cycle, tmp_path):
    runner.run_ponder(*cycle, False, False, chunk_size=10)
    state, hashes = runner.state_files_for_run(cycle[0], False)
    assert state.exists() and hashes.exists()
    assert not (tmp_path / "results" / "main_observations.parquet").exists()


@pytest.mark.parametrize("existing", [False, True])
def test_partial_run_does_not_update_master_or_state(cycle, tmp_path, existing):
    master = tmp_path / "master.parquet"
    state, hashes = runner.state_files_for_run(cycle[0], False)
    if existing:
        runner.run_ponder(*cycle, comet=False, main_orbit_file=master, chunk_size=10)
        # Force this object's predictions to run again while retaining the master.
        hashes.write_text("{}")
    before = {p: p.read_bytes() if p.exists() else None for p in (master, state, hashes)}
    runner.run_ponder(*cycle, comet=False, main_orbit_file=master, chunk_size=10, only_chunks="0")
    for path, content in before.items():
        assert (path.read_bytes() if path.exists() else None) == content


def test_failed_promotion_does_not_advance_state(cycle, tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("master write failed")

    monkeypatch.setattr(output_store, "upsert_observations", fail)
    with pytest.raises(OSError, match="master write failed"):
        runner.run_ponder(*cycle, comet=False, main_orbit_file=tmp_path / "master.parquet", chunk_size=10)
    assert not any(p.exists() for p in runner.state_files_for_run(cycle[0], False))


def test_successful_zero_output_advances_state(cycle, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "run_sorcha", lambda *a, **k: None)
    master = tmp_path / "master.parquet"
    runner.run_ponder(*cycle, comet=False, main_orbit_file=master, chunk_size=10)
    state, hashes = runner.state_files_for_run(cycle[0], False)
    assert json.loads(state.read_text())["last_mjd"] == 10
    assert "001" in json.loads(hashes.read_text())
    assert not master.exists()


def test_audit_failure_blocks_master_and_state(cycle, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "audit_combined_outputs", lambda *a, **k: ("audit.csv", "missing.csv", 1))
    with pytest.raises(RuntimeError, match="audit failed"):
        runner.run_ponder(*cycle, comet=False, main_orbit_file=tmp_path / "master.parquet", chunk_size=10)
    assert not any(p.exists() for p in runner.state_files_for_run(cycle[0], False))
    assert not (tmp_path / "master.parquet").exists()


def test_incomplete_job_blocks_master_and_state(cycle, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "run_id_set", lambda *a, **k: False)
    runner.run_ponder(*cycle, comet=False, main_orbit_file=tmp_path / "master.parquet", chunk_size=10)
    assert not any(p.exists() for p in runner.state_files_for_run(cycle[0], False))
    assert not (tmp_path / "master.parquet").exists()


def test_consolidation_reads_ephemeris_even_without_detection_file(cycle, tmp_path, monkeypatch):
    def only_ephemeris(orbits, physparams, output, db, config, timeout=None):
        output.with_name(output.stem + "_ew.csv").write_text("ObjID,fieldMJD_TAI\n001,10\n")

    monkeypatch.setattr(runner, "run_sorcha", only_ephemeris)
    master = tmp_path / "master.parquet"
    runner.run_ponder(*cycle, comet=False, main_orbit_file=master, chunk_size=10)
    assert pd.read_parquet(master).ObjID.tolist() == ["001"]
