import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from astropy.time import Time

from ponder.decam_db import export_pointings, read_db
from ponder.decam_pointings import SCHEMA, normalize
from ponder.decam_run import completed_checkpoint, finalize
from ponder.predictions import (
    EPHEMERIS_COLUMNS,
    OUTPUT_SCHEMA,
    atomic_json,
    convert_orbit,
    enrich_ephemeris,
    file_hash,
    prepare_orbits,
    separation,
)


@pytest.fixture
def exposure():
    rows = json.loads((Path(__file__).parent / "data/noirlab_decam.json").read_text())["rows"]
    return normalize(rows[0])[0]


def make_db(tmp_path, rows):
    catalog = tmp_path / "pointings.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), catalog)
    db = tmp_path / "pointings.db"
    counts = export_pointings(catalog, db)
    return db, counts


def test_export_keeps_unusual_filters_and_nonfatal_flags(tmp_path, exposure):
    exposure.update(filter="N662 unusual filter", quality_flags=["invalid_updated", "invalid_release_date"])
    db, counts = make_db(tmp_path, [exposure])
    assert counts == {"catalog": 1, "usable": 1, "excluded": 0}
    with read_db(db) as con:
        start, midpoint, filt, night = con.execute(
            "SELECT observationStartMJD,observationMidpointMJD_TAI,filter,observing_date FROM observations"
        ).fetchone()
    assert start == pytest.approx(Time(exposure["start_utc"], scale="utc").tai.mjd, abs=1e-10)
    assert midpoint == pytest.approx(Time(exposure["midpoint_utc"], scale="utc").tai.mjd, abs=1e-10)
    assert filt == "N662 unusual filter"
    assert night == "2024-01-01"  # exposure UTC date is January 2


def test_leap_second_midpoint(tmp_path, exposure):
    exposure.update(
        start_utc="2016-12-31T23:59:59Z",
        midpoint_utc="2016-12-31T23:59:60Z",
        exposure_seconds=2,
        observing_date="2016-12-31",
    )
    db, counts = make_db(tmp_path, [exposure])
    with read_db(db) as con:
        start, mid = con.execute(
            "SELECT observationStartMJD,observationMidpointMJD_TAI FROM observations"
        ).fetchone()
    assert counts["usable"] == 1
    assert (mid - start) * 86400 == pytest.approx(1, abs=1e-6)


def test_invalid_exposures_are_accounted_and_duplicate_ids_fail(tmp_path, exposure):
    invalid = {**exposure, "exposure_id": 99, "ra_deg": None}
    db, counts = make_db(tmp_path, [exposure, invalid])
    assert counts == {"catalog": 2, "usable": 1, "excluded": 1}
    with read_db(db) as con:
        assert con.execute("SELECT * FROM nights").fetchall() == [("2024-01-01", 2, 1)]
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        make_db(tmp_path, [exposure, exposure])
    # The previous published DB survives failed replacement.
    with read_db(db) as con:
        assert con.execute("SELECT count(*) FROM observations").fetchone()[0] == 1


def asteroid():
    return dict(
        Principal_desig="2024 AA",
        Number="(12345)",
        a=2.3,
        e=0.1,
        i=7,
        Node=32,
        Peri=68,
        M=16,
        Epoch=2461200.5,
        U=2,
    )


def comet():
    return dict(
        Designation_and_name="Test comet",
        Provisional_packed_desig="CJ00A010",
        Perihelion_dist=0.8,
        e=1.01,
        i=90,
        Node=30,
        Peri=42,
        Epoch_year=2024,
        Epoch_month=1,
        Epoch_day=1,
        Year_of_perihelion=2024,
        Month_of_perihelion=2,
        Day_of_perihelion=2.125,
    )


def test_orbits_do_not_require_photometry_and_keep_distinct_solutions(tmp_path):
    a, reason = convert_orbit(asteroid(), 0)
    assert reason is None and a["FORMAT"] == "KEP"
    assert a["epochMJD_TDB"] == pytest.approx(Time(2461200.5, format="jd", scale="tt").tdb.mjd)
    faint = {**asteroid(), "H": 100}
    assert convert_orbit(faint, 1)[0] is not None
    c, reason = convert_orbit(comet(), 0, comet=True)
    assert reason is None and c["FORMAT"] == "COM"
    other, _ = convert_orbit(comet(), 1, comet=True)
    assert c["ObjID"] != other["ObjID"]
    assert c["object_designation"] == other["object_designation"]
    path = tmp_path / "comets.json"
    path.write_text(json.dumps([comet(), comet(), {**comet(), "Epoch_year": None}]))
    chunks, counts = prepare_orbits(path, tmp_path / "inputs", comet=True, chunk_size=1)
    assert len(chunks) == 2
    assert counts == {"input": 3, "accepted": 2, "excluded": 1}


def test_perturbers_filters_and_invalid_orbits():
    assert convert_orbit({**asteroid(), "Number": "(1)"}, 0)[1] == "unsupported_massive_perturber"
    assert convert_orbit({**asteroid(), "U": 9}, 0)[1] == "default_orbit_filter"
    assert convert_orbit({**asteroid(), "a": float("nan")}, 0)[1].startswith("invalid_orbit")
    assert convert_orbit({**comet(), "Epoch_month": 13}, 0, True)[1].startswith("invalid_orbit")


def test_enrichment_joins_ids_retains_populations_and_applies_radius():
    orbits = pd.DataFrame([convert_orbit(asteroid(), 0)[0], convert_orbit(comet(), 0, True)[0]])
    pointing = pd.DataFrame(
        [
            dict(
                observationId=1,
                fieldRA_deg=0,
                fieldDec_deg=0,
                optFilter="N662",
                observing_date="2024-01-01",
                archive_filename="test.fits",
                observationMidpointMJD_TAI=60311.0,
            )
        ]
    )
    rows = []
    for oid, ra in zip(orbits.ObjID, [359.0, 1.2]):
        row = {c: 0.0 for c in EPHEMERIS_COLUMNS}
        row.update(ObjID=oid, FieldID="1", fieldMJD_TAI=60311.0, RA_deg=ra)
        rows.append(row)
    result = enrich_ephemeris(pd.DataFrame(rows), orbits, pointing).to_pandas()
    assert set(result.object_type) == {"comet", "non_comet"}
    assert set(result["filter"]) == {"N662"}
    assert list(result.field_separation_deg) == pytest.approx([1, 1.2])
    rows[1]["RA_deg"] = 1.31
    assert enrich_ephemeris(pd.DataFrame(rows), orbits, pointing).num_rows == 1
    rows[0]["FieldID"] = "999"
    with pytest.raises(ValueError, match="unknown exposure"):
        enrich_ephemeris(pd.DataFrame(rows), orbits, pointing)


def test_spherical_separation_pole_wrap():
    assert separation(359, 0, 1, 0) == pytest.approx(2)
    assert separation(0, 90, 180, 90) == pytest.approx(0, abs=1e-10)


def test_direction_cosine_roundoff_does_not_drop_field_center():
    from ponder.predictions import _SafeTrigonometry

    safe = _SafeTrigonometry()
    assert safe.arccos(np.nextafter(1.0, 2.0)) == 0.0
    assert safe.arccos(np.nextafter(-1.0, -2.0)) == np.pi
    with pytest.raises(ValueError, match="direction cosine"):
        safe.arccos(1.1)


def test_finalize_empty_nights_mixed_outputs_resume_and_incomplete(tmp_path, exposure):
    db, counts = make_db(
        tmp_path, [exposure, {**exposure, "exposure_id": 99, "observing_date": "2024-01-03"}]
    )
    manifest = {
        "db": str(db),
        "chunks": [{"index": 0, "sha256": "abc"}],
        "exposure_counts": counts,
        "orbit_counts": {"comet": {"excluded": 0}},
    }
    atomic_json(tmp_path / "manifest.json", manifest)
    with pytest.raises(RuntimeError, match="unfinished"):
        finalize(tmp_path)
    rows = []
    for mode in ("comet", "non_comet"):
        row = {c: 0.0 for c in EPHEMERIS_COLUMNS}
        row.update(
            ObjID="same designation",
            FieldID=str(exposure["exposure_id"]),
            exposure_id=exposure["exposure_id"],
            observing_date="2024-01-01",
            object_type=mode,
            orbit_solution_id=mode,
            object_designation="same designation",
            ponder_catalog_row=0,
            filter="g",
            archive_filename="test.fits",
            field_separation_deg=0.0,
        )
        rows.append(row)
    output = tmp_path / "leaf.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=OUTPUT_SCHEMA), output)
    marker = {
        "input_sha256": "abc",
        "outputs": [{"path": str(output), "sha256": file_hash(output), "rows": 2}],
        "excluded_orbits": [],
    }
    atomic_json(tmp_path / "checkpoints/000000/done.json", marker)
    report = finalize(tmp_path)
    assert report["rows"] == 2 and report["nights"] == 2
    assert pq.read_table(tmp_path / "nightly/2024-01-03.parquet").num_rows == 0
    assert set(pq.read_table(tmp_path / "nightly/2024-01-01.parquet")["object_type"].to_pylist()) == {
        "comet",
        "non_comet",
    }
    assert finalize(tmp_path) == report
    output.write_bytes(b"corrupted")
    with pytest.raises(RuntimeError, match="Corrupt checkpoint"):
        completed_checkpoint(tmp_path, 0, manifest)


def checkpoint_fixture(tmp_path):
    from ponder.predictions import ORBIT_SCHEMA

    rows = [convert_orbit(asteroid(), i)[0] for i in range(2)]
    path = tmp_path / "orbits.parquet"
    pq.write_table(pa.Table.from_pylist(rows, schema=ORBIT_SCHEMA), path)
    manifest = {"chunks": [{"index": 0, "path": str(path), "sha256": file_hash(path), "rows": 2}]}
    atomic_json(tmp_path / "manifest.json", manifest)
    return manifest


def fake_leaf_success(command, **kwargs):
    from types import SimpleNamespace
    from ponder.decam_run import source_hash

    orbit_path = Path(command[command.index("--orbits") + 1])
    output = Path(command[command.index("--output") + 1])
    pq.write_table(pa.Table.from_pylist([], schema=OUTPUT_SCHEMA), output)
    atomic_json(
        output.with_suffix(".metrics.json"),
        {
            "rows": 0,
            "orbits": pq.read_metadata(orbit_path).num_rows,
            "sha256": file_hash(output),
            "elapsed_seconds": 1,
            "max_rss_kb": 1000,
            "source_hash": source_hash(),
            "input_sha256": file_hash(orbit_path),
        },
    )
    return SimpleNamespace(returncode=0)


def test_checkpoint_zero_matches_resumes_without_reexecution(tmp_path, monkeypatch):
    from ponder import decam_run

    manifest = checkpoint_fixture(tmp_path)
    monkeypatch.setattr(decam_run.subprocess, "run", fake_leaf_success)
    result = decam_run.checkpoint(tmp_path, 0)
    assert result["outputs"][0]["rows"] == 0
    monkeypatch.setattr(decam_run.subprocess, "run", lambda *a, **k: pytest.fail("reran completed range"))
    assert decam_run.checkpoint(tmp_path, 0) == result
    assert completed_checkpoint(tmp_path, 0, manifest) == result


def test_checkpoint_infrastructure_failure_is_not_an_orbit_exclusion(tmp_path, monkeypatch):
    from ponder import decam_run
    from types import SimpleNamespace

    checkpoint_fixture(tmp_path)
    monkeypatch.setattr(decam_run.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=-9))
    with pytest.raises(RuntimeError, match="infrastructure"):
        decam_run.checkpoint(tmp_path, 0)
    assert not (tmp_path / "checkpoints/000000/done.json").exists()
    monkeypatch.setattr(decam_run.subprocess, "run", fake_leaf_success)
    assert decam_run.checkpoint(tmp_path, 0)["excluded_orbits"] == []


def test_checkpoint_isolates_only_persistent_individual_failure(tmp_path, monkeypatch):
    from ponder import decam_run
    from types import SimpleNamespace

    checkpoint_fixture(tmp_path)

    def run(command, **kwargs):
        frame = pd.read_parquet(command[command.index("--orbits") + 1])
        if len(frame) > 1 or frame.iloc[0].ponder_catalog_row == 0:
            return SimpleNamespace(returncode=42)
        return fake_leaf_success(command, **kwargs)

    monkeypatch.setattr(decam_run.subprocess, "run", run)
    result = decam_run.checkpoint(tmp_path, 0)
    assert len(result["excluded_orbits"]) == 1
    assert result["excluded_orbits"][0]["ponder_catalog_row"] == 0
    assert sum(r["orbits"] for r in result["outputs"]) == 1


def test_completion_retries_missing_work_without_canceling_itself(tmp_path, monkeypatch):
    from ponder import decam_run
    from types import SimpleNamespace

    checkpoint_fixture(tmp_path)
    atomic_json(tmp_path / "execution.json", {"tasks": [[0]]})
    atomic_json(tmp_path / "submission.json", {"job_id": "100", "finalizer_job_id": "101"})
    monkeypatch.setenv("SLURM_JOB_ID", "101")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert command[0] == "squeue"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    ids = iter(["200\n", "201\n"])
    monkeypatch.setattr(decam_run.subprocess, "run", run)
    monkeypatch.setattr(decam_run.subprocess, "check_output", lambda *a, **k: next(ids))
    report = decam_run.complete(tmp_path)
    assert report["job_id"] == "200"
    assert report["finalizer_job_id"] == "201"
    assert report["previous_submissions"] == ["100"]
    assert "--dependency=afterany:200" in (tmp_path / "finalize.sbatch").read_text()
    assert "decam_run complete" in (tmp_path / "finalize.sbatch").read_text()
    assert not (tmp_path / "publication.json").exists()
    atomic_json(tmp_path / "submission.json", {"job_id": "300", "previous_submissions": ["100", "200"]})
    with pytest.raises(RuntimeError, match="retries exhausted"):
        decam_run.complete(tmp_path)
    assert (tmp_path / "needs_attention.json").exists()
