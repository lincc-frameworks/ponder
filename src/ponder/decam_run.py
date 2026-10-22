"""Reproducible DECam prediction preparation, Slurm execution and nightly publication."""

import argparse
import importlib.metadata
import json
import os
import resource
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests

from .decam_db import export_pointings, read_db
from .decam_pointings import writer_lock
from .predictions import (
    OUTPUT_SCHEMA,
    atomic_json,
    file_hash,
    predict,
    prepare_geometry,
    prepare_orbits,
    sorcha_context,
)
from .utils import MPCCOM_URL, MPCORB_URL

THREAD_ENV = {
    key: "1"
    for key in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "NUMBA_NUM_THREADS",
    )
}


def now():
    return datetime.now(timezone.utc).isoformat()


def load(path):
    return json.loads(Path(path).read_text())


def source_hash():
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def download(url, path):
    """Freeze a complete download; an interrupted partial file is never reused."""
    path = Path(path)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    for attempt in range(4):
        try:
            with requests.get(url, stream=True, timeout=(30, 180)) as response:
                response.raise_for_status()
                with temporary.open("wb") as out:
                    for block in response.iter_content(1024 * 1024):
                        out.write(block)
                    out.flush()
                    os.fsync(out.fileno())
            temporary.replace(path)
            return
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2**attempt)


def stage(run, auxiliary):
    import spiceypy
    from sorcha.ephemeris.simulation_data_files import make_retriever
    from sorcha.ephemeris.simulation_setup import create_assist_ephemeris, furnish_spiceypy
    from sorcha.utilities.retrieve_ephemeris_data_files import _decompress

    run, auxiliary = Path(run).resolve(), Path(auxiliary).resolve()
    run.mkdir(parents=True, exist_ok=True)
    auxiliary.mkdir(parents=True, exist_ok=True)
    with writer_lock(run / "stage"):
        for name, url in (("asteroids", MPCORB_URL), ("comets", MPCCOM_URL)):
            print(f"Staging {name}", flush=True)
            download(url, run / "inputs" / f"{name}.json.gz")
        with writer_lock(auxiliary / "bootstrap"):
            args, configs = sorcha_context(auxiliary)
            retriever = make_retriever(configs.auxiliary, str(auxiliary))
            retriever.fetch(configs.auxiliary.observatory_codes_compressed, processor=_decompress)
            create_assist_ephemeris(args, configs.auxiliary)
            furnish_spiceypy(args, configs.auxiliary)
            spiceypy.kclear()
        atomic_json(run / "staged.json", {"staged_utc": now(), "auxiliary": str(auxiliary)})


def prepare(run, catalog, auxiliary, catalogs=None, filter_orbits=True, pointing_db=None):
    run, auxiliary = map(lambda p: Path(p).resolve(), (run, auxiliary))
    run.mkdir(parents=True, exist_ok=True)
    with writer_lock(run / "prepare"):
        if (run / "manifest.json").exists():
            existing = load(run / "manifest.json")
            if pointing_db is not None and file_hash(pointing_db) != existing["db_sha256"]:
                raise RuntimeError("Run directory belongs to a different pointing database")
            if catalog is not None and file_hash(catalog) != existing["catalog_sha256"]:
                raise RuntimeError("Run directory belongs to a different archive generation")
            if str(auxiliary) != existing["auxiliary"] or filter_orbits != existing["filter_orbits"]:
                raise RuntimeError("Run configuration differs; choose a new run directory")
            if catalogs is not None:
                if set(catalogs) != set(existing["catalogs"]):
                    raise RuntimeError("Run object populations differ; choose a new run directory")
                for mode, path in catalogs.items():
                    if file_hash(path) != existing["catalogs"][mode]["sha256"]:
                        raise RuntimeError("Orbit catalog changed; choose a new run directory")
            return existing
        inputs = run / "inputs"
        inputs.mkdir(exist_ok=True)
        db = inputs / "pointings.db"
        archive_metadata = {}
        catalog_sha256 = None
        if pointing_db is None:
            frozen_catalog = inputs / "pointings.parquet"
            if not frozen_catalog.exists():
                try:
                    os.link(catalog, frozen_catalog)
                except OSError:
                    shutil.copy2(catalog, frozen_catalog)
            counts = export_pointings(frozen_catalog, db)
            catalog_sha256 = file_hash(frozen_catalog)
            archive_metadata = {
                k.decode(): v.decode() for k, v in (pq.read_schema(frozen_catalog).metadata or {}).items()
            }
        else:
            shutil.copy2(pointing_db, db)
            with read_db(db) as con:
                archive_metadata = dict(con.execute("SELECT * FROM metadata"))
                counts = json.loads(archive_metadata["export_counts"])
        chunks, orbit_counts, sources = [], {}, {}
        catalogs = catalogs or {"non_comet": inputs / "asteroids.json.gz", "comet": inputs / "comets.json.gz"}
        for mode, path in catalogs.items():
            path = Path(path).resolve()
            sources[mode] = {"path": str(path), "sha256": file_hash(path)}
            planned, orbit_counts[mode] = prepare_orbits(
                path, inputs / mode, comet=mode == "comet", filter_orbits=filter_orbits
            )
            for chunk in planned:
                chunk.update(index=len(chunks), object_type=mode)
                chunks.append(chunk)
        auxiliaries = {
            p.name: file_hash(p)
            for p in sorted(auxiliary.iterdir())
            if p.is_file() and not p.name.endswith((".lock", ".part", ".tmp"))
        }
        manifest = {
            "schema_version": 1,
            "created_utc": now(),
            "run_dir": str(run),
            "db": str(db),
            "db_sha256": file_hash(db),
            "catalog_sha256": catalog_sha256,
            "archive_metadata": archive_metadata,
            "auxiliary": str(auxiliary),
            "auxiliary_sha256": auxiliaries,
            "catalogs": sources,
            "orbit_counts": orbit_counts,
            "exposure_counts": counts,
            "chunks": chunks,
            "radius_deg": 1.3,
            "buffer_deg": 0.2,
            "observatory": "W84",
            "filter_orbits": filter_orbits,
            "versions": {
                p: importlib.metadata.version(p)
                for p in ("sorcha", "assist", "rebound", "astropy", "pyarrow", "pandas", "numpy")
            },
        }
        (run / "environment.txt").write_text(
            subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True)
        )
        atomic_json(run / "manifest.json", manifest)
        return manifest


def geometry(run):
    run = Path(run)
    manifest = load(run / "manifest.json")
    target = run / "geometry.parquet"
    with writer_lock(target):
        if target.exists() and target.with_suffix(".json").exists():
            saved = load(target.with_suffix(".json"))
            if saved["db_sha256"] == manifest["db_sha256"] and saved["sha256"] == file_hash(target):
                return
            raise RuntimeError("Geometry fingerprint mismatch")
        if file_hash(manifest["db"]) != manifest["db_sha256"]:
            raise RuntimeError("Pointing database changed after preparation")
        prepare_geometry(manifest["db"], manifest["auxiliary"], target)
        atomic_json(
            target.with_suffix(".json"), {"sha256": file_hash(target), "db_sha256": manifest["db_sha256"]}
        )


def leaf(run, orbit_path, output, geometry_path=None):
    run, output = Path(run), Path(output)
    manifest = load(run / "manifest.json")
    start = time.monotonic()
    pointings = pd.read_parquet(geometry_path or run / "geometry.parquet")
    orbits = pd.read_parquet(orbit_path)
    try:
        table = predict(orbits, pointings, manifest["auxiliary"])
    except SystemExit as exc:
        # Sorcha reports invalid input elements through sys.exit. Other exits are
        # infrastructure/configuration failures and must stop the task.
        if "Input elements for orbit" in str(exc):
            print(str(exc), file=sys.stderr)
            return 42
        raise
    except Exception as exc:
        from rebound import Encounter, Escape, NoParticles

        if isinstance(exc, (Encounter, Escape, NoParticles)):
            print(str(exc), file=sys.stderr)
            return 42
        raise
    temporary = output.with_suffix(".tmp")
    pq.write_table(table, temporary, compression="zstd", row_group_size=100000)
    temporary.replace(output)
    atomic_json(
        output.with_suffix(".metrics.json"),
        {
            "elapsed_seconds": time.monotonic() - start,
            "max_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "rows": len(table),
            "orbits": len(orbits),
            "sha256": file_hash(output),
            "source_hash": source_hash(),
            "input_sha256": file_hash(orbit_path),
        },
    )
    return 0


def completed_checkpoint(run, index, manifest=None):
    run = Path(run)
    marker = run / "checkpoints" / f"{index:06d}" / "done.json"
    if not marker.exists():
        return None
    result = load(marker)
    if manifest is not None and result["input_sha256"] != manifest["chunks"][index]["sha256"]:
        raise RuntimeError("Checkpoint input changed")
    if result.get("source_hash") not in (None, source_hash()):
        raise RuntimeError("Checkpoint was generated by different source code")
    for item in result["outputs"]:
        if not Path(item["path"]).exists() or file_hash(item["path"]) != item["sha256"]:
            raise RuntimeError(f"Corrupt checkpoint output: {item['path']}")
    return result


def checkpoint(run, index):
    run = Path(run).resolve()
    manifest = load(run / "manifest.json")
    chunk = manifest["chunks"][index]
    directory = run / "checkpoints" / f"{index:06d}"
    directory.mkdir(parents=True, exist_ok=True)
    with writer_lock(directory / "checkpoint"):
        if result := completed_checkpoint(run, index, manifest):
            return result
        if file_hash(chunk["path"]) != chunk["sha256"]:
            raise RuntimeError("Orbit checkpoint input changed")
        outputs, excluded = [], []

        def execute(orbit_path, label):
            output = directory / f"{label}.parquet"
            metrics_path = output.with_suffix(".metrics.json")
            if output.exists() and metrics_path.exists():
                metrics = load(metrics_path)
                if file_hash(output) != metrics["sha256"]:
                    raise RuntimeError("Corrupt leaf output")
                if metrics.get("source_hash") != source_hash() or metrics.get("input_sha256") != file_hash(
                    orbit_path
                ):
                    raise RuntimeError("Partial leaf source or inputs changed; use a new run directory")
                outputs.append({"path": str(output), **metrics})
                return
            command = [
                sys.executable,
                "-m",
                "ponder.decam_run",
                "leaf",
                "--run-dir",
                str(run),
                "--orbits",
                str(orbit_path),
                "--output",
                str(output),
            ]
            env = {**os.environ, **THREAD_ENV, "MPLCONFIGDIR": str(directory / "mpl")}
            with (directory / f"{label}.log").open("a") as log:
                result = subprocess.run(
                    command, stdout=log, stderr=subprocess.STDOUT, env=env, timeout=6 * 3600
                )
            if result.returncode == 0:
                outputs.append({"path": str(output), **load(metrics_path)})
                return
            if result.returncode != 42:
                raise RuntimeError(
                    f"Checkpoint {index}/{label} failed ({result.returncode}); see its log. "
                    "No orbit exclusions recorded for infrastructure/unclassified failures."
                )
            orbits = pd.read_parquet(orbit_path)
            if len(orbits) == 1:
                excluded.append(
                    {
                        "orbit_solution_id": orbits.iloc[0].ObjID,
                        "object_type": orbits.iloc[0].object_type,
                        "ponder_catalog_row": int(orbits.iloc[0].ponder_catalog_row),
                        "reason": "propagation_failure",
                        "log": str(directory / f"{label}.log"),
                    }
                )
                return
            for suffix, part in (
                ("L", orbits.iloc[: len(orbits) // 2]),
                ("R", orbits.iloc[len(orbits) // 2 :]),
            ):
                child = directory / f"{label}{suffix}-orbits.parquet"
                part.to_parquet(child, index=False)
                execute(child, label + suffix)

        execute(chunk["path"], "result")
        result = {
            "input_sha256": chunk["sha256"],
            "finished_utc": now(),
            "outputs": outputs,
            "excluded_orbits": excluded,
            "input_rows": chunk["rows"],
            "source_hash": source_hash(),
        }
        if sum(item["orbits"] for item in outputs) + len(excluded) != chunk["rows"]:
            raise RuntimeError("Orbit checkpoint accounting mismatch")
        atomic_json(directory / "done.json", result)
        return result


def worker(run, task_index):
    run = Path(run)
    execution = load(run / "execution.json")
    if execution["source_hash"] != source_hash():
        raise RuntimeError("Source changed after submission; preserve the frozen worker source")
    manifest = load(run / "manifest.json")
    if any(importlib.metadata.version(name) != version for name, version in manifest["versions"].items()):
        raise RuntimeError("Scientific dependency versions changed after preparation")
    for name, expected in manifest["auxiliary_sha256"].items():
        if file_hash(Path(manifest["auxiliary"]) / name) != expected:
            raise RuntimeError("Auxiliary data changed: " + name)
    if file_hash(run / "geometry.parquet") != load(run / "geometry.json")["sha256"]:
        raise RuntimeError("Prepared geometry changed")
    indices = execution["tasks"][task_index]
    with ThreadPoolExecutor(max_workers=execution["workers"]) as pool:
        futures = [pool.submit(checkpoint, run, i) for i in indices]
        for future in as_completed(futures):
            result = future.result()
            print(f"Checkpoint finished: {sum(x['rows'] for x in result['outputs'])} predictions", flush=True)


def status(run):
    run = Path(run)
    manifest = load(run / "manifest.json")
    markers = list((run / "checkpoints").glob("*/done.json"))
    return {
        "run_dir": str(run),
        "checkpoints_total": len(manifest["chunks"]),
        "checkpoints_done": len(markers),
        "published": (run / "publication.json").exists(),
        "exposure_counts": manifest["exposure_counts"],
        "orbit_counts": manifest["orbit_counts"],
    }


def submit(run, workers=8, group_size=20):
    run = Path(run).resolve()
    manifest = load(run / "manifest.json")
    if not (run / "pilot.json").exists() or not load(run / "pilot.json").get("passed"):
        raise RuntimeError("A passing compute-node pilot is required before production submission")
    if load(run / "pilot.json")["manifest_sha256"] != file_hash(run / "manifest.json"):
        raise RuntimeError("Pilot was run against a different manifest")
    if not 1 <= workers <= 8 or group_size < 1:
        raise ValueError("workers must be 1..8 and group size positive")
    pending = [
        c["index"]
        for c in manifest["chunks"]
        if not (run / "checkpoints" / f"{c['index']:06d}" / "done.json").exists()
    ]
    if not pending:
        return {"message": "All checkpoints already complete"}
    execution_path = run / "execution.json"
    if execution_path.exists():
        raise RuntimeError("Execution already submitted; resume its recorded tasks instead of overwriting it")
    tasks = [pending[i : i + group_size] for i in range(0, len(pending), group_size)]
    # Freeze source used by batch jobs, independent of later editable-checkout work.
    code = run / "code" / "ponder"
    code.mkdir(parents=True, exist_ok=True)
    for source in Path(__file__).parent.glob("*.py"):
        shutil.copy2(source, code / source.name)
    execution = {"tasks": tasks, "workers": workers, "source_hash": source_hash(), "submitted_utc": now()}
    atomic_json(execution_path, execution)
    script = run / "worker.sbatch"
    logs = run / "logs"
    logs.mkdir(exist_ok=True)
    script.write_text(
        "\n".join(
            [
                "#!/bin/bash",
                "#SBATCH --account=trujillo",
                "#SBATCH --partition=core",
                "#SBATCH --cpus-per-task=8",
                "#SBATCH --mem=32G",
                "#SBATCH --time=48:00:00",
                "#SBATCH --job-name=ponder-decam",
                f"#SBATCH --array=0-{len(tasks)-1}%4",
                f"#SBATCH --output={logs}/worker-%A_%a.log",
                "set -euo pipefail",
                "export " + " ".join(f"{k}=1" for k in THREAD_ENV),
                f"export PYTHONPATH={shlex.quote(str(run / 'code'))}",
                f"exec {shlex.quote(sys.executable)} -u -m ponder.decam_run worker --run-dir "
                f'{shlex.quote(str(run))} --task-index "$SLURM_ARRAY_TASK_ID"',
                "",
            ]
        )
    )
    try:
        result = subprocess.check_output(["sbatch", "--parsable", str(script)], text=True).strip()
    except subprocess.CalledProcessError:
        execution_path.unlink()
        raise
    atomic_json(run / "submission.json", {"job_id": result, "tasks": len(tasks), "submitted_utc": now()})
    return {"job_id": result, "tasks": len(tasks)}


def benchmark(run):
    """Measure full-history checkpoints and choose a bounded production layout."""
    if not os.environ.get("SLURM_JOB_ID"):
        raise RuntimeError("Run benchmarks on a compute node")
    run = Path(run)
    manifest = load(run / "manifest.json")
    indices = []
    for mode in ("comet", "non_comet"):
        chunks = [c for c in manifest["chunks"] if c["object_type"] == mode]
        if chunks:
            indices.append(chunks[0]["index"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda i: checkpoint(run, i), indices))
    leaves = [item for result in results for item in result["outputs"]]
    if not leaves:
        raise RuntimeError("No successful benchmark leaves")
    rss = max(item["max_rss_kb"] for item in leaves)
    # Leave 8 GB of the allocation for supervisors, filesystem buffers and variability.
    workers = min(8, int(24 * 1024**2 / max(rss * 1.25, 1)))
    if workers < 1:
        raise RuntimeError("A single worker exceeds the 24-GB memory target; reduce orbit checkpoint size")
    seconds = max(item["elapsed_seconds"] * 250 / item["orbits"] for item in leaves)
    group_size = max(1, min(20, int(24 * 3600 * workers / max(seconds * 1.5, 1))))
    if seconds * 1.5 > 24 * 3600:
        raise RuntimeError("A checkpoint exceeds the target runtime; reduce orbit checkpoint size")
    report = {
        "workers": workers,
        "group_size": group_size,
        "max_worker_rss_kb": rss,
        "estimated_checkpoint_seconds": seconds,
        "checkpoint_indices": indices,
        "slurm_job_id": os.environ["SLURM_JOB_ID"],
    }
    atomic_json(run / "benchmark.json", report)
    return report


def pipeline(run, catalog, auxiliary):
    """Gate production on real compute-node validation and queue audited finalization."""
    from .decam_pilot import pilot

    run = Path(run).resolve()
    manifest = prepare(run, catalog, auxiliary)
    geometry(run)
    pilot(run, manifest)
    sizing = benchmark(run)
    submission = submit(run, sizing["workers"], sizing["group_size"])
    if "job_id" not in submission:
        return finalize(run)
    return queue_finalizer(run, submission)


def queue_finalizer(run, submission):
    run = Path(run).resolve()
    job_id = submission["job_id"].split(";")[0]
    script = run / "finalize.sbatch"
    script.write_text(
        "\n".join(
            [
                "#!/bin/bash",
                "#SBATCH --account=trujillo",
                "#SBATCH --partition=core",
                "#SBATCH --cpus-per-task=1",
                "#SBATCH --mem=16G",
                "#SBATCH --time=48:00:00",
                "#SBATCH --job-name=ponder-decam-publish",
                f"#SBATCH --dependency=afterany:{job_id}",
                f"#SBATCH --output={run}/logs/finalize-%j.log",
                "set -euo pipefail",
                f"export PYTHONPATH={shlex.quote(str(run / 'code'))}",
                f"exec {shlex.quote(sys.executable)} -u -m ponder.decam_run complete --run-dir {shlex.quote(str(run))}",
                "",
            ]
        )
    )
    final_job = subprocess.check_output(["sbatch", "--parsable", str(script)], text=True).strip()
    submission["finalizer_job_id"] = final_job
    atomic_json(run / "submission.json", submission)
    return submission


def resubmit(run):
    """Retry incomplete tasks under the same immutable execution manifest."""
    run = Path(run).resolve()
    previous = load(run / "submission.json")
    job_id = previous["job_id"].split(";")[0]
    query = subprocess.run(["squeue", "-h", "-j", job_id, "-o", "%T"], text=True, capture_output=True)
    if query.returncode and "Invalid job id" not in query.stderr:
        raise RuntimeError("Cannot verify previous array state: " + query.stderr)
    if query.stdout.strip():
        raise RuntimeError("Previous array still has active tasks; wait before resubmitting")
    execution = load(run / "execution.json")
    manifest = load(run / "manifest.json")
    tasks = [
        i
        for i, indices in enumerate(execution["tasks"])
        if any(completed_checkpoint(run, n, manifest) is None for n in indices)
    ]
    if not tasks:
        return finalize(run)
    history = previous.get("previous_submissions", [])
    if len(history) >= 2:
        raise RuntimeError("Two infrastructure retries exhausted; inspect failed task logs")
    if previous.get("finalizer_job_id") and previous["finalizer_job_id"].split(";")[0] != os.environ.get(
        "SLURM_JOB_ID"
    ):
        subprocess.run(["scancel", previous["finalizer_job_id"].split(";")[0]], check=True)
    new_id = subprocess.check_output(
        ["sbatch", "--parsable", "--array=" + ",".join(map(str, tasks)) + "%4", str(run / "worker.sbatch")],
        text=True,
    ).strip()
    current = {
        "job_id": new_id,
        "tasks": len(tasks),
        "previous_submissions": history + [job_id],
        "submitted_utc": now(),
    }
    return queue_finalizer(run, current)


def complete(run):
    """Run after the array exits: publish or retry unfinished infrastructure work."""
    run = Path(run)
    manifest = load(run / "manifest.json")
    try:
        if all(completed_checkpoint(run, c["index"], manifest) is not None for c in manifest["chunks"]):
            return finalize(run)
        return resubmit(run)
    except Exception as exc:
        atomic_json(
            run / "needs_attention.json",
            {"time_utc": now(), "error": str(exc), "slurm_job_id": os.environ.get("SLURM_JOB_ID")},
        )
        raise


def finalize(run):
    """Use a disk-backed key ledger to reconcile every successful leaf with nightly files."""
    run = Path(run).resolve()
    manifest = load(run / "manifest.json")
    with writer_lock(run / "finalize"):
        if (run / "publication.json").exists():
            for row in pq.read_table(run / "nightly_coverage.parquet").to_pylist():
                if file_hash(run / "nightly" / f"{row['observing_date']}.parquet") != row["sha256"]:
                    raise RuntimeError("Published nightly file failed checksum verification")
            return load(run / "publication.json")
        results = []
        for chunk in manifest["chunks"]:
            result = completed_checkpoint(run, chunk["index"], manifest)
            if result is None:
                raise RuntimeError(f"Checkpoint {chunk['index']} is unfinished; publication refused")
            results.append(result)
        staging = run / "nightly.staging"
        staging.mkdir(exist_ok=True)
        ledger_path = run / "finalize.sqlite3"
        ledger_path.unlink(missing_ok=True)
        ledger = sqlite3.connect(ledger_path)
        ledger.execute("PRAGMA journal_mode=OFF")
        ledger.execute("PRAGMA temp_store=FILE")
        ledger.execute(
            """CREATE TABLE matches (night TEXT, exposure INTEGER, population TEXT,
                       solution TEXT, payload TEXT, PRIMARY KEY(exposure,population,solution)) WITHOUT ROWID"""
        )
        total = 0
        failures = []
        try:
            for result in results:
                failures.extend(result["excluded_orbits"])
                for output in result["outputs"]:
                    actual_rows = 0
                    for batch in pq.ParquetFile(output["path"]).iter_batches(batch_size=10000):
                        rows = batch.to_pylist()
                        ledger.executemany(
                            "INSERT INTO matches VALUES (?,?,?,?,?)",
                            [
                                (
                                    r["observing_date"],
                                    r["exposure_id"],
                                    r["object_type"],
                                    r["orbit_solution_id"],
                                    json.dumps(r, allow_nan=False),
                                )
                                for r in rows
                            ],
                        )
                        actual_rows += len(rows)
                        ledger.commit()
                    if actual_rows != output["rows"]:
                        raise RuntimeError("Checkpoint row count mismatch")
                    total += actual_rows
            ledger.execute("CREATE INDEX by_night ON matches(night,exposure,population,solution)")
            with read_db(manifest["db"]) as con:
                nights = con.execute("SELECT * FROM nights ORDER BY observing_date").fetchall()
                exclusions = pd.read_sql_query("SELECT * FROM excluded_exposures", con)
            exclusions.to_parquet(run / "excluded_exposures.parquet", index=False)
            atomic_json(run / "failed_orbits.json", failures)
            coverage = []
            written = 0
            for night, catalog_count, usable_count in nights:
                output = staging / f"{night}.parquet"
                populations = dict(
                    ledger.execute(
                        "SELECT population,count(*) FROM matches WHERE night=? GROUP BY population", (night,)
                    )
                )
                cursor = ledger.execute(
                    "SELECT payload FROM matches WHERE night=? " "ORDER BY exposure,population,solution",
                    (night,),
                )
                schema = OUTPUT_SCHEMA.with_metadata(
                    {
                        b"ponder.run_id": run.name.encode(),
                        b"ponder.manifest_sha256": file_hash(run / "manifest.json").encode(),
                    }
                )
                with pq.ParquetWriter(output, schema, compression="zstd") as writer:
                    while rows := cursor.fetchmany(10000):
                        writer.write_table(
                            pa.Table.from_pylist([json.loads(r[0]) for r in rows], schema=schema)
                        )
                        written += len(rows)
                count = sum(populations.values())
                if pq.read_metadata(output).num_rows != count:
                    raise RuntimeError("Nightly output count mismatch")
                coverage.append(
                    {
                        "observing_date": night,
                        "catalog_exposures": catalog_count,
                        "usable_exposures": usable_count,
                        "excluded_exposures": catalog_count - usable_count,
                        "comets": populations.get("comet", 0),
                        "non_comets": populations.get("non_comet", 0),
                        "rows": count,
                        "sha256": file_hash(output),
                        "status": (
                            "complete_with_exclusions"
                            if failures
                            or catalog_count != usable_count
                            or any(c["excluded"] for c in manifest["orbit_counts"].values())
                            else "complete"
                        ),
                    }
                )
            if written != total or ledger.execute("SELECT count(*) FROM matches").fetchone()[0] != total:
                raise RuntimeError("Unroutable or missing output rows")
            pd.DataFrame(coverage).to_parquet(run / "nightly_coverage.parquet", index=False)
            publication = {
                "published_utc": now(),
                "rows": total,
                "nights": len(coverage),
                "failed_orbits": len(failures),
                "exposure_counts": manifest["exposure_counts"],
                "orbit_counts": manifest["orbit_counts"],
                "manifest_sha256": file_hash(run / "manifest.json"),
            }
            # Recover a crash after directory rename but before the publication marker.
            target = run / "nightly"
            if target.exists():
                for item in coverage:
                    if file_hash(target / f"{item['observing_date']}.parquet") != item["sha256"]:
                        raise RuntimeError("Existing nightly directory disagrees with rebuilt outputs")
                shutil.rmtree(staging)
            else:
                staging.replace(target)
            atomic_json(run / "publication.json", publication)
            return publication
        finally:
            ledger.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage",
        choices=[
            "stage",
            "prepare",
            "geometry",
            "worker",
            "checkpoint",
            "leaf",
            "submit",
            "resubmit",
            "status",
            "finalize",
            "complete",
            "pilot",
            "benchmark",
            "pipeline",
        ],
    )
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--auxiliary-data", type=Path)
    parser.add_argument("--task-index", type=int)
    parser.add_argument("--chunk", type=int)
    parser.add_argument("--orbits", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--geometry", type=Path)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--group-size", type=int, default=20)
    args = parser.parse_args(argv)
    run = args.run_dir.resolve()
    if args.stage == "stage":
        stage(run, args.auxiliary_data or run.parent.parent / "auxiliary")
    elif args.stage == "prepare":
        if not args.catalog:
            parser.error("prepare requires --catalog")
        prepare(run, args.catalog, args.auxiliary_data or run.parent.parent / "auxiliary")
    elif args.stage == "geometry":
        geometry(run)
    elif args.stage == "pilot":
        from .decam_pilot import pilot

        print(json.dumps(pilot(run, load(run / "manifest.json")), indent=2))
    elif args.stage == "benchmark":
        print(json.dumps(benchmark(run), indent=2))
    elif args.stage == "pipeline":
        if not args.catalog:
            parser.error("pipeline requires --catalog")
        print(
            json.dumps(
                pipeline(run, args.catalog, args.auxiliary_data or run.parent.parent / "auxiliary"), indent=2
            )
        )
    elif args.stage == "worker":
        if args.task_index is None:
            parser.error("worker requires --task-index")
        worker(run, args.task_index)
    elif args.stage == "checkpoint":
        if args.chunk is None:
            parser.error("checkpoint requires --chunk")
        checkpoint(run, args.chunk)
    elif args.stage == "leaf":
        if not args.orbits or not args.output:
            parser.error("leaf requires --orbits and --output")
        return leaf(run, args.orbits, args.output, args.geometry)
    elif args.stage == "submit":
        print(json.dumps(submit(run, args.workers, args.group_size), indent=2))
    elif args.stage == "resubmit":
        print(json.dumps(resubmit(run), indent=2))
    elif args.stage == "status":
        print(json.dumps(status(run), indent=2))
    elif args.stage == "finalize":
        print(json.dumps(finalize(run), indent=2))
    elif args.stage == "complete":
        print(json.dumps(complete(run), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
