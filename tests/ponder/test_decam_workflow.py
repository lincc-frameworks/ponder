"""Offline lifecycle checks; external kernels and scheduler calls are replaced at their boundaries."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from test_nightly_publish import prepare as prepare_publication
from test_nightly_publish import run  # noqa: F401 -- shared publication fixture
from test_predictions import asteroid, comet

from ponder import decam_run as workflow
from ponder import nightly_publish, nightly_sync
from ponder.decam_pointings import SCHEMA, normalize
from ponder.predictions import OUTPUT_SCHEMA, atomic_json, file_hash


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    rows = json.loads((Path(__file__).parent / "data/noirlab_decam.json").read_text())["rows"]
    catalog = tmp_path / "catalog.parquet"
    pq.write_table(pa.Table.from_pylist([normalize(rows[0])[0]], schema=SCHEMA), catalog)
    auxiliary = tmp_path / "auxiliary"
    auxiliary.mkdir()
    (auxiliary / "kernel").write_bytes(b"frozen")
    catalogs = {}
    for mode, row in [("comet", comet()), ("non_comet", asteroid())]:
        path = tmp_path / f"{mode}.json"
        path.write_text(json.dumps([row]))
        catalogs[mode] = path
    root = tmp_path / "run"
    with monkeypatch.context() as patch:
        patch.setattr(workflow.subprocess, "check_output", lambda *a, **k: "test environment\n")
        manifest = workflow.prepare(root, catalog, auxiliary, catalogs=catalogs)
    return root, catalog, auxiliary, catalogs, manifest


def test_preparation_freezes_inputs_and_rejects_drift(prepared):
    root, catalog, auxiliary, catalogs, manifest = prepared
    assert manifest["exposure_counts"] == {"catalog": 1, "usable": 1, "excluded": 0}
    assert len(manifest["chunks"]) == 2
    assert {c["object_type"] for c in manifest["chunks"]} == {"comet", "non_comet"}
    assert manifest["observatory"] == "W84"
    assert (manifest["radius_deg"], manifest["buffer_deg"]) == (1.3, 0.2)
    assert workflow.prepare(root, catalog, auxiliary, catalogs=catalogs) == manifest
    with pytest.raises(RuntimeError, match="configuration differs"):
        workflow.prepare(root, catalog, auxiliary, catalogs=catalogs, filter_orbits=False)
    with pytest.raises(RuntimeError, match="populations differ"):
        workflow.prepare(root, catalog, auxiliary, catalogs={"comet": catalogs["comet"]})
    catalogs["comet"].write_text("[]")
    with pytest.raises(RuntimeError, match="Orbit catalog changed"):
        workflow.prepare(root, catalog, auxiliary, catalogs=catalogs)
    assert file_hash(manifest["db"]) == manifest["db_sha256"]


def build_geometry(root, monkeypatch):
    calls = []

    def fake_geometry(db, auxiliary, target):
        calls.append(db)
        pd.DataFrame({"observationId": [1]}).to_parquet(target)

    monkeypatch.setattr(workflow, "prepare_geometry", fake_geometry)
    workflow.geometry(root)
    workflow.geometry(root)
    assert len(calls) == 1


def test_geometry_and_worker_reject_modified_inputs(prepared, monkeypatch):
    root, _, auxiliary, _, manifest = prepared
    build_geometry(root, monkeypatch)
    atomic_json(
        root / "execution.json", {"source_hash": workflow.source_hash(), "tasks": [[0, 1]], "workers": 2}
    )
    done = []
    monkeypatch.setattr(workflow, "checkpoint", lambda r, i: done.append(i) or {"outputs": [{"rows": i}]})
    workflow.worker(root, 0)
    assert sorted(done) == [0, 1]
    (auxiliary / "kernel").write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="Auxiliary data changed"):
        workflow.worker(root, 0)
    (auxiliary / "kernel").write_bytes(b"frozen")
    (root / "geometry.parquet").write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="Prepared geometry changed"):
        workflow.worker(root, 0)
    with pytest.raises(RuntimeError, match="Geometry fingerprint mismatch"):
        workflow.geometry(root)


def test_submission_is_pilot_gated_and_preserves_execution(prepared, monkeypatch):
    root, _, _, _, manifest = prepared
    with pytest.raises(RuntimeError, match="passing compute-node pilot"):
        workflow.submit(root)
    atomic_json(root / "pilot.json", {"passed": True, "manifest_sha256": file_hash(root / "manifest.json")})
    with pytest.raises(ValueError):
        workflow.submit(root, workers=9)
    commands = []
    monkeypatch.setattr(
        workflow.subprocess, "check_output", lambda cmd, **kw: commands.append(cmd) or "123;cluster\n"
    )
    submitted = workflow.submit(root, workers=2, group_size=1)
    assert submitted == {"job_id": "123;cluster", "tasks": 2}
    execution = workflow.load(root / "execution.json")
    assert execution["tasks"] == [[0], [1]]
    assert "#SBATCH --array=0-1%4" in (root / "worker.sbatch").read_text()
    assert (root / "code/ponder/decam_run.py").exists()
    with pytest.raises(RuntimeError, match="already submitted"):
        workflow.submit(root)
    result = workflow.queue_finalizer(root, submitted)
    assert result["finalizer_job_id"] == "123;cluster"
    assert "--dependency=afterany:123" in (root / "finalize.sbatch").read_text()
    assert len(commands) == 2
    assert workflow.status(root)["checkpoints_done"] == 0


def test_benchmark_refuses_unsafe_resource_estimates(prepared, monkeypatch):
    root, *_ = prepared
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    with pytest.raises(RuntimeError, match="compute node"):
        workflow.benchmark(root)
    monkeypatch.setenv("SLURM_JOB_ID", "123")
    metrics = {"max_rss_kb": 1024**2, "elapsed_seconds": 20, "orbits": 250}
    monkeypatch.setattr(workflow, "checkpoint", lambda *a: {"outputs": [metrics]})
    sizing = workflow.benchmark(root)
    assert sizing["workers"] == 8 and sizing["group_size"] == 20
    assert sizing["checkpoint_indices"] == [0, 1]
    metrics["max_rss_kb"] = 32 * 1024**2
    with pytest.raises(RuntimeError, match="memory target"):
        workflow.benchmark(root)
    metrics.update(max_rss_kb=1, elapsed_seconds=100000)
    with pytest.raises(RuntimeError, match="runtime"):
        workflow.benchmark(root)


def test_leaf_writes_checksummed_empty_results_and_classifies_errors(prepared, monkeypatch):
    root, _, _, _, manifest = prepared
    build_geometry(root, monkeypatch)
    orbit_path = manifest["chunks"][0]["path"]
    out = root / "result.parquet"
    monkeypatch.setattr(workflow, "predict", lambda *a: pa.Table.from_pylist([], schema=OUTPUT_SCHEMA))
    assert workflow.leaf(root, orbit_path, out) == 0
    metrics = workflow.load(out.with_suffix(".metrics.json"))
    assert metrics["rows"] == 0 and metrics["orbits"] == 1
    assert metrics["sha256"] == file_hash(out)

    def fail(*a):
        raise SystemExit("Input elements for orbit are invalid")

    monkeypatch.setattr(workflow, "predict", fail)
    assert workflow.leaf(root, orbit_path, root / "bad.parquet") == 42
    assert not (root / "bad.parquet").exists()

    def infrastructure(*a):
        raise OSError("missing kernel")

    monkeypatch.setattr(workflow, "predict", infrastructure)
    with pytest.raises(OSError, match="missing kernel"):
        workflow.leaf(root, orbit_path, root / "bad.parquet")


def test_sync_published_bundle_and_idempotent_retry(run, tmp_path, monkeypatch):
    work = prepare_publication(run)
    for bucket in range(2):
        nightly_publish.reduce_bucket(work, bucket)
    nightly_publish.seal(run, work)
    (run / "environment.txt").write_text("frozen environment")
    target = tmp_path / "remote/nightly"
    calls = []

    def transport(command, **kwargs):
        if command[0] == "ssh":
            # Execute the exact self-contained remote program locally, preserving the
            # subprocess boundary and its serialized verification contract.
            return subprocess.CompletedProcess(command, 0, stdout=execute_remote(kwargs["input"]))
        assert command[0] == "rsync"
        assert "--checksum" in command and "--delete" not in command
        calls.append(command)
        source = Path(command[-2])
        dest = Path(command[-1].split(":", 1)[1])
        if "--files-from=" in " ".join(command):
            names = (
                Path(next(x.split("=", 1)[1] for x in command if x.startswith("--files-from=")))
                .read_text()
                .splitlines()
            )
            for name in names:
                shutil.copy2(source / name, dest / name)
        else:
            shutil.copytree(source, dest, dirs_exist_ok=True)
        return SimpleNamespace(returncode=0)

    def execute_remote(program):
        import contextlib
        import io

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec(program, {})
        return output.getvalue()

    monkeypatch.setattr(nightly_sync.subprocess, "run", transport)
    destination = "test@host:" + str(target)
    report = nightly_sync.sync(run, destination)
    assert report["passed"] and report["nights"] == 3 and report["rows"] == 4
    assert len(calls) == 2
    assert (target / "_provenance/failed_orbits.json").exists()
    assert nightly_sync.sync(run, destination)["passed"]
    assert len(calls) == 2
    (run / "nightly/2024-01-01.parquet").write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="Local nightly checksum mismatch"):
        nightly_sync.sync(run, destination)


def test_geometry_preserves_midpoint_and_scales_observatory_vectors(prepared, monkeypatch):
    import numpy as np
    import spiceypy
    from astropy.time import Time
    from sorcha.ephemeris import simulation_geometry, simulation_parsing, simulation_setup
    from sorcha.ephemeris.simulation_constants import AU_KM

    from ponder import predictions

    root, _, auxiliary, _, manifest = prepared
    ephem = SimpleNamespace(
        jd_ref=2451545.0, get_particle=lambda *a: SimpleNamespace(x=1, y=2, z=3, vx=4, vy=5, vz=6)
    )
    monkeypatch.setattr(simulation_setup, "create_assist_ephemeris", lambda *a: (ephem, 1, 1))
    monkeypatch.setattr(simulation_setup, "furnish_spiceypy", lambda *a: None)
    monkeypatch.setattr(simulation_parsing, "Observatory", lambda *a: object())
    calls = []

    def rates(seconds, code, **kw):
        calls.append((seconds, code))
        return np.array([AU_KM, 2 * AU_KM, 3 * AU_KM]), np.array([1.0, 2.0, 3.0])

    monkeypatch.setattr(simulation_geometry, "barycentricObservatoryRates", rates)
    cleaned = []
    monkeypatch.setattr(spiceypy, "kclear", lambda: cleaned.append(True))
    output = root / "geometry.parquet"
    predictions.prepare_geometry(manifest["db"], auxiliary, output)
    frame = pd.read_parquet(output)
    assert len(frame) == 1 and calls[0][1] == "W84"
    assert (
        frame.fieldJD_TDB.iloc[0]
        == Time(frame.observationMidpointMJD_TAI.iloc[0], format="mjd", scale="tai").tdb.jd
    )
    assert frame.r_obs_x.iloc[0] == 1
    assert frame.v_obs_x.iloc[0] == pytest.approx(86400 / AU_KM)
    assert frame.r_sun_x.iloc[0] == 1 and cleaned == [True]
    assert np.linalg.norm(frame[["visit_vector_x", "visit_vector_y", "visit_vector_z"]]) == pytest.approx(1)


def synthetic_ephemeris(orbits, pointings, auxiliary):
    from ponder.predictions import EPHEMERIS_COLUMNS

    rows = []
    for _, pointing in pointings.iterrows():
        for _, orbit in orbits.iterrows():
            row = dict.fromkeys(EPHEMERIS_COLUMNS, 0.0)
            row.update(
                ObjID=orbit.ObjID,
                FieldID=str(pointing.observationId),
                fieldMJD_TAI=pointing.observationMidpointMJD_TAI,
                RA_deg=10.0,
                Dec_deg=0.0,
                RARateCosDec_deg_day=2.0,
                DecRate_deg_day=1.0,
            )
            rows.append(row)
    return pd.DataFrame(rows, columns=EPHEMERIS_COLUMNS)


def test_pilot_edge_cases_and_disagreement_gate(prepared, monkeypatch):
    from ponder import decam_pilot
    from ponder.predictions import enrich_ephemeris

    root, _, _, _, manifest = prepared
    pointing = pd.DataFrame(
        [
            dict(
                observationId=1,
                FieldID="1",
                observing_date="2024-01-01",
                observationMidpointMJD_TAI=60000.0,
                fieldRA_deg=10.0,
                fieldDec_deg=0.0,
                optFilter="g",
                archive_filename="image.fits",
                visit_vector_x=1.0,
                visit_vector_y=0.0,
                visit_vector_z=0.0,
            )
        ]
    )
    pointing.to_parquet(root / "geometry.parquet")
    monkeypatch.setenv("SLURM_JOB_ID", "123")
    monkeypatch.setattr(decam_pilot, "direct_ephemeris", synthetic_ephemeris)

    def indexed(orbits, pointings, auxiliary):
        return enrich_ephemeris(synthetic_ephemeris(orbits, pointings, auxiliary), orbits, pointings)

    monkeypatch.setattr(decam_pilot, "predict", indexed)
    result = decam_pilot.pilot(root, manifest)
    assert result["passed"] and result["real_matches"] == 2
    assert result["edge_matches"] == 8  # two populations at center/inside for two targets
    assert result["max_position_error_arcsec"] == 0
    assert result["max_sampled_rate_deg_day"] == pytest.approx(5**0.5)

    def missing(orbits, pointings, auxiliary):
        return indexed(orbits, pointings, auxiliary).slice(0, 1)

    monkeypatch.setattr(decam_pilot, "predict", missing)
    with pytest.raises(RuntimeError, match="candidate keys"):
        decam_pilot.pilot(root, manifest)


def test_prediction_restores_sorcha_state_after_success_and_failure(prepared, monkeypatch):
    from sorcha.ephemeris import simulation_driver

    from ponder import predictions

    root, _, auxiliary, _, manifest = prepared
    orbits = pd.read_parquet(manifest["chunks"][0]["path"])
    pointing = pd.DataFrame(
        [
            dict(
                observationId=1,
                observationMidpointMJD_TAI=60000.0,
                fieldRA_deg=10.0,
                fieldDec_deg=0.0,
                optFilter="g",
                observing_date="2024-01-01",
                archive_filename="image.fits",
            )
        ]
    )
    original = simulation_driver.np

    def create(orbital, pointings, *args):
        assert "object_type" not in orbital
        assert simulation_driver.np is not original
        return synthetic_ephemeris(orbital, pointings, auxiliary)

    monkeypatch.setattr(simulation_driver, "create_ephemeris", create)
    result = predictions.predict(orbits, pointing, auxiliary)
    assert result.num_rows == 1 and result["object_type"].to_pylist() == ["comet"]
    assert simulation_driver.np is original

    def fail(*args):
        raise RuntimeError("integration failed")

    monkeypatch.setattr(simulation_driver, "create_ephemeris", fail)
    with pytest.raises(RuntimeError, match="integration failed"):
        predictions.predict(orbits, pointing, auxiliary)
    assert simulation_driver.np is original


def test_download_retries_without_reusing_partial_data(tmp_path, monkeypatch):
    attempts = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield b"catalog"
            if len(attempts) == 1:
                raise workflow.requests.ConnectionError("interrupted")
            yield b"-complete"

    def get(*args, **kwargs):
        attempts.append(True)
        return Response()

    monkeypatch.setattr(workflow.requests, "get", get)
    monkeypatch.setattr(workflow.time, "sleep", lambda *a: None)
    path = tmp_path / "catalog.gz"
    workflow.download("https://example.test/catalog", path)
    assert path.read_bytes() == b"catalog-complete" and len(attempts) == 2
    workflow.download("https://example.test/catalog", path)
    assert len(attempts) == 2
    assert not path.with_suffix(".gz.part").exists()


@pytest.mark.parametrize(
    "command,extra,expected",
    [
        ("prepare", ["--catalog", "catalog.parquet"], "prepare"),
        ("pipeline", ["--catalog", "catalog.parquet"], "pipeline"),
        ("stage", [], "stage"),
        ("geometry", [], "geometry"),
        ("benchmark", [], "benchmark"),
        ("submit", ["--workers", "2", "--group-size", "3"], "submit"),
        ("worker", ["--task-index", "4"], "worker"),
        ("checkpoint", ["--chunk", "5"], "checkpoint"),
        ("leaf", ["--orbits", "orbits.parquet", "--output", "leaf.parquet"], "leaf"),
        ("resubmit", [], "resubmit"),
        ("status", [], "status"),
        ("finalize", [], "finalize"),
        ("complete", [], "complete"),
    ],
)
def test_cli_dispatches_validated_arguments(tmp_path, monkeypatch, command, extra, expected):
    calls = []
    monkeypatch.setattr(
        workflow, expected, lambda *args: calls.append(args) or (0 if command == "leaf" else {})
    )
    assert workflow.main([command, "--run-dir", str(tmp_path), *extra]) == 0
    assert len(calls) == 1 and calls[0][0] == tmp_path.resolve()
    if command == "submit":
        assert calls[0][1:] == (2, 3)
    elif command == "worker":
        assert calls[0][1:] == (4,)
    elif command == "checkpoint":
        assert calls[0][1:] == (5,)
    elif command in ("prepare", "pipeline"):
        assert calls[0][1] == Path("catalog.parquet")
    elif command == "leaf":
        assert calls[0][1:3] == (Path("orbits.parquet"), Path("leaf.parquet"))


@pytest.mark.parametrize("command", ["prepare", "pipeline", "worker", "checkpoint", "leaf"])
def test_cli_rejects_incomplete_commands(tmp_path, command):
    with pytest.raises(SystemExit) as exc:
        workflow.main([command, "--run-dir", str(tmp_path)])
    assert exc.value.code == 2


def test_pipeline_never_submits_after_failed_pilot(prepared, monkeypatch):
    from ponder import decam_pilot

    root, catalog, auxiliary, _, manifest = prepared
    events = []
    monkeypatch.setattr(workflow, "prepare", lambda *a: manifest)
    monkeypatch.setattr(workflow, "geometry", lambda *a: events.append("geometry"))

    def fail(*a):
        raise RuntimeError("pilot disagreement")

    monkeypatch.setattr(decam_pilot, "pilot", fail)
    monkeypatch.setattr(workflow, "submit", lambda *a: events.append("submit") or {"job_id": "12"})
    with pytest.raises(RuntimeError, match="pilot disagreement"):
        workflow.pipeline(root, catalog, auxiliary)
    assert events == ["geometry"]
    monkeypatch.setattr(decam_pilot, "pilot", lambda *a: events.append("pilot"))
    monkeypatch.setattr(workflow, "benchmark", lambda *a: {"workers": 2, "group_size": 1})
    monkeypatch.setattr(workflow, "queue_finalizer", lambda r, s: {**s, "queued": True})
    assert workflow.pipeline(root, catalog, auxiliary) == {"job_id": "12", "queued": True}
    assert events[-3:] == ["geometry", "pilot", "submit"]
    monkeypatch.setattr(workflow, "submit", lambda *a: {})
    monkeypatch.setattr(workflow, "finalize", lambda *a: {"published": True})
    assert workflow.pipeline(root, catalog, auxiliary) == {"published": True}
