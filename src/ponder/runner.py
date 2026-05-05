import hashlib
import json
import os
import signal
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from tqdm.auto import tqdm

from .utils import *

# TODO: should be some sort of configurable
STATE_FILE = Path("state.json")
HASHES_FILE = Path("element_hashes.json")
WORK_DIR = Path("work")
RESULTS_DIR = Path("results")
DEFAULT_CHUNK_SIZE = 5000
DEFAULT_SORCHA_WORKERS = 1


def first_run_setup(objects, state, db_path):
    """If this is the first run, initialize state and hashes files."""
    print("First run — building baseline hashes.")
    prev_hashes = {obj_id(c): hash_orbit(c) for c in objects}
    state["last_mjd"] = 0.0
    HASHES_FILE.write_text(json.dumps(prev_hashes))
    STATE_FILE.write_text(json.dumps(state))
    print(f"  {len(prev_hashes)} comets hashed. Last MJD = {state['last_mjd']:.4f}")


@dataclass(frozen=True)
class SorchaChunk:
    index: int
    row_start: int
    row_end: int
    orbits_path: Path
    physparams_path: Path
    output_path: Path

    @property
    def ew_output_path(self):
        return self.output_path.with_name(f"{self.output_path.stem}_ew.csv")

    @property
    def done_path(self):
        return self.output_path.with_suffix(".done")

    @property
    def failed_path(self):
        return self.output_path.with_suffix(".failed")


def _terminate_process_group(proc):
    if proc.poll() is not None:
        return

    if hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
            return
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            return

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_sorcha(orbits, physparams, output, db, config, timeout=None):
    print(output.with_suffix(""))
    command = [
        "sorcha",
        "run",
        "-c",
        str(config),
        "--ob",
        str(orbits),
        "-p",
        str(physparams),
        "--pd",
        str(db),
        "-o",
        str(output.resolve().parent),
        "--ew",
        str(output.stem + "_ew"),
        "-t",
        str(output.stem),
        "-f",
    ]
    proc = subprocess.Popen(command, start_new_session=True)
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(proc)
        raise TimeoutError(f"Sorcha timed out after {timeout} seconds for {output.stem}") from exc

    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, command)


def build_id_set_inputs(objects, ids, job_name, comet):
    if len(ids) == 0:
        print(f"  [skipping {job_name} — no objects to process]")
        return

    if comet:
        orbs, phys = comets_to_sorcha_inputs(objects, ids)
    else:
        orbs, phys = mpcorb_to_sorcha_inputs(objects, ids)

    if len(orbs) == 0:
        print(f"  [skipping {job_name} — no valid Sorcha inputs]")
        return

    catalog_rows = catalog_rows_for_sorcha_inputs(objects, ids, comet)
    if len(catalog_rows) != len(orbs):
        raise ValueError(
            f"Catalog row count for {job_name} does not match Sorcha inputs: "
            f"{len(catalog_rows)} != {len(orbs)}"
        )

    return orbs.reset_index(drop=True), phys.reset_index(drop=True), catalog_rows.reset_index(drop=True)


def catalog_rows_for_sorcha_inputs(objects, ids, comet):
    df = pd.DataFrame(objects)
    if len(df) == 0:
        return df

    if comet:
        id_col = df["Provisional_packed_desig"].where(
            df["Provisional_packed_desig"].notna(), df["Designation_and_name"]
        )
        df = df[id_col.isin(ids)].copy()

        required_epoch_columns = [
            "Epoch_year",
            "Epoch_month",
            "Epoch_day",
            "Year_of_perihelion",
            "Month_of_perihelion",
            "Day_of_perihelion",
        ]
        bad = []
        for row_index, row in df.iterrows():
            if any(pd.isna(row.get(column)) for column in required_epoch_columns):
                bad.append(row_index)
        return df.drop(bad).reset_index(drop=True)

    return df[df["Principal_desig"].isin(ids)].copy().reset_index(drop=True)


def read_ignore_ids(ignore_objects_path=None, ignore_object_ids=None):
    ids = set(ignore_object_ids or [])
    if not ignore_objects_path:
        return ids

    path = Path(ignore_objects_path)
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path, dtype=str)
        for column in ["ObjID", "Principal_desig", "Provisional_packed_desig", "Designation_and_name"]:
            if column in df.columns:
                ids.update(value.strip() for value in df[column].dropna() if value.strip())
                return ids
        if len(df.columns) > 0:
            first_column = df.columns[0]
            ids.update(value.strip() for value in df[first_column].dropna() if value.strip())
        return ids

    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        ids.update(part.strip() for part in line.split(",") if part.strip())

    return ids


def filter_ignored_objects(objects, ignore_ids):
    if not ignore_ids:
        return objects

    kept = [obj for obj in objects if obj_id(obj) not in ignore_ids]
    print(f"  Ignore list — kept: {len(kept)}  removed: {len(objects) - len(kept)}")
    return kept


def dataframe_digest(*frames):
    digest = hashlib.sha256()
    for frame in frames:
        digest.update("|".join(frame.columns).encode())
        digest.update(b"\0")
        row_hashes = pd.util.hash_pandas_object(frame, index=False).values
        digest.update(row_hashes.tobytes())
        digest.update(b"\0")
    return digest.hexdigest()


def parse_chunk_indices(spec):
    if spec is None:
        return None

    indices = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if end < start:
                raise ValueError(f"Invalid chunk range: {part}")
            indices.update(range(start, end + 1))
        else:
            indices.add(int(part))

    return indices


def plan_sorcha_chunks(job_name, time, row_count, chunk_size, digest):
    base_stem = f"{time[:10]}_job_{job_name}"
    run_stem = f"{base_stem}_{digest[:12]}"
    work_dir = WORK_DIR / run_stem
    results_dir = RESULTS_DIR / run_stem
    chunks = []

    for index, row_start in enumerate(range(0, row_count, chunk_size)):
        row_end = min(row_start + chunk_size, row_count)
        chunk_stem = f"chunk_{index:05d}_rows_{row_start:07d}_{row_end - 1:07d}"
        chunks.append(
            SorchaChunk(
                index=index,
                row_start=row_start,
                row_end=row_end,
                orbits_path=work_dir / f"{chunk_stem}_orbits.csv",
                physparams_path=work_dir / f"{chunk_stem}_physparams.csv",
                output_path=results_dir / f"{chunk_stem}.csv",
            )
        )

    return chunks


def plan_debug_subchunks(parent_chunk, subchunk_size):
    chunks = []
    debug_work_dir = parent_chunk.orbits_path.parent / "debug"
    debug_results_dir = parent_chunk.output_path.parent / "debug"
    for index, row_start in enumerate(range(parent_chunk.row_start, parent_chunk.row_end, subchunk_size)):
        row_end = min(row_start + subchunk_size, parent_chunk.row_end)
        chunk_stem = (
            f"chunk_{parent_chunk.index:05d}_debug_{index:04d}_" f"rows_{row_start:07d}_{row_end - 1:07d}"
        )
        chunks.append(
            SorchaChunk(
                index=index,
                row_start=row_start,
                row_end=row_end,
                orbits_path=debug_work_dir / f"{chunk_stem}_orbits.csv",
                physparams_path=debug_work_dir / f"{chunk_stem}_physparams.csv",
                output_path=debug_results_dir / f"{chunk_stem}.csv",
            )
        )

    return chunks


def chunk_is_complete(chunk):
    return chunk.done_path.exists() and chunk.output_path.exists() and chunk.ew_output_path.exists()


def write_chunk_inputs(chunk, orbs, phys):
    chunk.orbits_path.parent.mkdir(parents=True, exist_ok=True)
    chunk.physparams_path.parent.mkdir(parents=True, exist_ok=True)
    orbs.iloc[chunk.row_start : chunk.row_end].to_csv(chunk.orbits_path, index=False)
    phys.iloc[chunk.row_start : chunk.row_end].to_csv(chunk.physparams_path, index=False)


def write_chunk_manifest(manifest_path, chunks, job_name, row_count, chunk_size, digest):
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "job_name": job_name,
        "row_count": row_count,
        "chunk_size": chunk_size,
        "digest": digest,
        "chunks": [
            {
                "index": chunk.index,
                "row_start": chunk.row_start,
                "row_end": chunk.row_end,
                "output_path": str(chunk.output_path),
            }
            for chunk in chunks
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))


def failure_report_paths(chunks):
    results_dir = chunks[0].output_path.parent
    return results_dir / "failures.csv", results_dir / "failed_catalog_rows.csv"


def write_failure_reports(chunks, failures, catalog_rows):
    if not failures:
        return None, None

    failure_path, catalog_path = failure_report_paths(chunks)
    failure_path.parent.mkdir(parents=True, exist_ok=True)

    failure_rows = []
    failed_catalog_rows = []
    for chunk, error in failures:
        failure_rows.append(
            {
                "chunk": chunk.index,
                "row_start": chunk.row_start,
                "row_end": chunk.row_end,
                "error": error,
                "failed_path": str(chunk.failed_path),
                "orbits_path": str(chunk.orbits_path),
                "physparams_path": str(chunk.physparams_path),
                "output_path": str(chunk.output_path),
            }
        )

        rows = catalog_rows.iloc[chunk.row_start : chunk.row_end].copy()
        rows.insert(0, "chunk", chunk.index)
        rows.insert(1, "chunk_row_start", chunk.row_start)
        rows.insert(2, "chunk_row_end", chunk.row_end)
        rows.insert(3, "chunk_error", error)
        failed_catalog_rows.append(rows)

    pd.DataFrame(failure_rows).to_csv(failure_path, index=False)
    if failed_catalog_rows:
        pd.concat(failed_catalog_rows, ignore_index=True).to_csv(catalog_path, index=False)
    else:
        pd.DataFrame(columns=["chunk", "chunk_row_start", "chunk_row_end", "chunk_error"]).to_csv(
            catalog_path, index=False
        )

    return failure_path, catalog_path


def debug_report_paths(chunks):
    results_dir = chunks[0].output_path.parent
    return results_dir / "subchunk_debug_report.csv", results_dir / "failed_subchunk_catalog_rows.csv"


def write_debug_subchunk_reports(chunks, results, parent_by_subchunk, catalog_rows):
    if not chunks:
        return None, None

    report_path, catalog_path = debug_report_paths(chunks)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    report_rows = []
    failed_catalog_rows = []
    for subchunk, ok, error in results:
        parent_chunk = parent_by_subchunk[subchunk]
        report_rows.append(
            {
                "parent_chunk": parent_chunk.index,
                "subchunk": subchunk.index,
                "row_start": subchunk.row_start,
                "row_end": subchunk.row_end,
                "status": "done" if ok else "failed",
                "error": error or "",
                "failed_path": "" if ok else str(subchunk.failed_path),
                "orbits_path": str(subchunk.orbits_path),
                "physparams_path": str(subchunk.physparams_path),
                "output_path": str(subchunk.output_path),
            }
        )

        if not ok:
            rows = catalog_rows.iloc[subchunk.row_start : subchunk.row_end].copy()
            rows.insert(0, "parent_chunk", parent_chunk.index)
            rows.insert(1, "subchunk", subchunk.index)
            rows.insert(2, "subchunk_row_start", subchunk.row_start)
            rows.insert(3, "subchunk_row_end", subchunk.row_end)
            rows.insert(4, "subchunk_error", error)
            failed_catalog_rows.append(rows)

    pd.DataFrame(report_rows).to_csv(report_path, index=False)
    if failed_catalog_rows:
        pd.concat(failed_catalog_rows, ignore_index=True).to_csv(catalog_path, index=False)
    else:
        pd.DataFrame(
            columns=[
                "parent_chunk",
                "subchunk",
                "subchunk_row_start",
                "subchunk_row_end",
                "subchunk_error",
            ]
        ).to_csv(catalog_path, index=False)

    return report_path, catalog_path


def run_sorcha_chunk(chunk, db, config, timeout):
    chunk.output_path.parent.mkdir(parents=True, exist_ok=True)
    if chunk.done_path.exists():
        chunk.done_path.unlink()
    if chunk.failed_path.exists():
        chunk.failed_path.unlink()

    try:
        run_sorcha(chunk.orbits_path, chunk.physparams_path, chunk.output_path, db, config, timeout=timeout)
    except Exception as exc:
        failure = {
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "chunk": chunk.index,
            "row_start": chunk.row_start,
            "row_end": chunk.row_end,
            "error": str(exc),
        }
        chunk.failed_path.write_text(json.dumps(failure, indent=2))
        return chunk, False, str(exc)

    success = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "chunk": chunk.index,
        "row_start": chunk.row_start,
        "row_end": chunk.row_end,
    }
    chunk.done_path.write_text(json.dumps(success, indent=2))
    return chunk, True, None


def run_debug_subchunks(
    orbs,
    phys,
    catalog_rows,
    parent_failures,
    db,
    config,
    subchunk_size,
    workers,
    timeout=None,
    resume=True,
):
    if subchunk_size <= 0 or not parent_failures:
        return []

    parent_by_subchunk = {}
    debug_chunks = []
    for parent_chunk, _ in parent_failures:
        for subchunk in plan_debug_subchunks(parent_chunk, subchunk_size):
            debug_chunks.append(subchunk)
            parent_by_subchunk[subchunk] = parent_chunk

    completed = [chunk for chunk in debug_chunks if resume and chunk_is_complete(chunk)]
    pending = [chunk for chunk in debug_chunks if chunk not in completed]
    print(
        f"  Debug subchunks — {len(debug_chunks)} total, "
        f"{len(completed)} complete, {len(pending)} pending, size={subchunk_size}, workers={workers}"
    )

    for chunk in pending:
        write_chunk_inputs(chunk, orbs, phys)

    results = [(chunk, True, None) for chunk in completed]
    with tqdm(
        total=len(debug_chunks),
        initial=len(completed),
        desc=f"Debug subchunks ({workers} workers, size={subchunk_size})",
        unit="subchunk",
        dynamic_ncols=True,
    ) as progress:
        if pending:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(run_sorcha_chunk, chunk, db, config, timeout) for chunk in pending]
                for future in as_completed(futures):
                    chunk, ok, error = future.result()
                    parent_chunk = parent_by_subchunk[chunk]
                    label = (
                        f"chunk {parent_chunk.index:05d} debug {chunk.index:04d} "
                        f"rows {chunk.row_start}-{chunk.row_end - 1}"
                    )
                    if ok:
                        progress.write(f"  [debug done] {label}")
                    else:
                        progress.write(f"  [debug failed] {label}: {error}")
                    results.append((chunk, ok, error))
                    progress.update(1)

    report_path, catalog_path = write_debug_subchunk_reports(
        debug_chunks,
        sorted(results, key=lambda result: (parent_by_subchunk[result[0]].index, result[0].index)),
        parent_by_subchunk,
        catalog_rows,
    )
    print(f"  Wrote debug subchunk report to {report_path}")
    print(f"  Wrote failed debug catalog rows to {catalog_path}")

    return results


def combine_csv_files(input_paths, output_path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as out:
        wrote_header = False
        for input_path in input_paths:
            with open(input_path) as src:
                header = src.readline()
                if not header:
                    continue
                if not wrote_header:
                    out.write(header)
                    wrote_header = True
                for line in src:
                    out.write(line)


def combine_chunk_outputs(chunks, final_output):
    combine_csv_files([chunk.output_path for chunk in chunks], final_output)
    combine_csv_files(
        [chunk.ew_output_path for chunk in chunks],
        final_output.with_name(f"{final_output.stem}_ew.csv"),
    )


def run_sorcha_chunks(
    orbs,
    phys,
    job_name,
    time,
    db,
    config,
    chunk_size,
    workers,
    timeout=None,
    resume=True,
    only_chunks=None,
    catalog_rows=None,
    debug_failed_chunk_size=0,
):
    workers = max(1, workers)
    if len(orbs) != len(phys):
        raise ValueError("Orbit and physical parameter input lengths do not match")
    if catalog_rows is None:
        catalog_rows = pd.DataFrame(index=range(len(orbs)))
    catalog_rows = catalog_rows.reset_index(drop=True)
    if len(catalog_rows) != len(orbs):
        raise ValueError("Catalog row count does not match Sorcha input length")

    if chunk_size <= 0:
        orbits_path = WORK_DIR / f"job_{job_name}_orbits.csv"
        physpar_path = WORK_DIR / f"job_{job_name}_physparams.csv"
        out_path = RESULTS_DIR / f"{time[:10]}_job_{job_name}.csv"
        orbs.to_csv(orbits_path, index=False)
        phys.to_csv(physpar_path, index=False)
        run_sorcha(orbits_path, physpar_path, out_path, db, config, timeout=timeout)
        return

    digest = dataframe_digest(orbs, phys)
    chunks = plan_sorcha_chunks(job_name, time, len(orbs), chunk_size, digest)
    if only_chunks is not None:
        known_indices = {chunk.index for chunk in chunks}
        unknown_indices = sorted(only_chunks - known_indices)
        if unknown_indices:
            raise ValueError(f"Unknown chunk index for {job_name}: {unknown_indices[0]}")
        chunks_to_run = [chunk for chunk in chunks if chunk.index in only_chunks]
    else:
        chunks_to_run = chunks

    manifest_path = chunks[0].orbits_path.parent / "manifest.json"
    write_chunk_manifest(manifest_path, chunks, job_name, len(orbs), chunk_size, digest)

    completed = [chunk for chunk in chunks_to_run if resume and chunk_is_complete(chunk)]
    pending = [chunk for chunk in chunks_to_run if chunk not in completed]
    final_output = RESULTS_DIR / f"{time[:10]}_job_{job_name}.csv"
    selected_text = ""
    if only_chunks is not None:
        selected_text = f", selected={len(chunks_to_run)}"

    print(
        f"  Sorcha chunks — {job_name}: {len(chunks)} total, "
        f"{len(completed)} complete, {len(pending)} pending{selected_text}, workers={workers}"
    )

    failures = []
    with tqdm(
        total=len(chunks_to_run),
        initial=len(completed),
        desc=f"{job_name} chunks ({workers} workers)",
        unit="chunk",
        dynamic_ncols=True,
    ) as progress:
        if pending:
            for chunk in pending:
                write_chunk_inputs(chunk, orbs, phys)

            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(run_sorcha_chunk, chunk, db, config, timeout) for chunk in pending]
                for future in as_completed(futures):
                    chunk, ok, error = future.result()
                    label = f"chunk {chunk.index:05d} rows {chunk.row_start}-{chunk.row_end - 1}"
                    if ok:
                        progress.write(f"  [done] {label}")
                    else:
                        progress.write(f"  [failed] {label}: {error}")
                        failures.append((chunk, error))
                    progress.update(1)

    if failures:
        failure_path, catalog_path = write_failure_reports(chunks, failures, catalog_rows)
        print(f"  Wrote failure report to {failure_path}")
        print(f"  Wrote failed catalog rows to {catalog_path}")
        run_debug_subchunks(
            orbs,
            phys,
            catalog_rows,
            failures,
            db,
            config,
            debug_failed_chunk_size,
            workers,
            timeout=timeout,
            resume=resume,
        )
        failure_text = ", ".join(f"chunk {chunk.index:05d}: {error}" for chunk, error in failures)
        raise RuntimeError(f"Sorcha chunk failures for {job_name}: {failure_text}")

    if only_chunks is not None:
        print(f"  Completed selected chunks for {job_name}; skipping final combine and state update")
        return False

    if not all(chunk_is_complete(chunk) for chunk in chunks):
        missing = [f"{chunk.index:05d}" for chunk in chunks if not chunk_is_complete(chunk)]
        raise RuntimeError(f"Cannot combine {job_name}; incomplete chunks: {', '.join(missing)}")

    combine_chunk_outputs(chunks, final_output)
    print(f"  Combined {len(chunks)} chunks into {final_output}")
    return True


def run_id_set(
    objects,
    ids,
    job_name,
    time,
    comet,
    db,
    config,
    chunk_size,
    workers,
    timeout=None,
    resume=True,
    only_chunks=None,
    debug_failed_chunk_size=0,
):
    inputs = build_id_set_inputs(objects, ids, job_name, comet)
    if not inputs:
        return True

    orbs, phys, catalog_rows = inputs
    return run_sorcha_chunks(
        orbs,
        phys,
        job_name,
        time,
        db,
        config,
        chunk_size,
        workers,
        timeout=timeout,
        resume=resume,
        only_chunks=only_chunks,
        catalog_rows=catalog_rows,
        debug_failed_chunk_size=debug_failed_chunk_size,
    )


def run_ponder(
    db_path,
    object_path,
    config_path,
    comet,
    filter_orbits=True,
    chunk_size=DEFAULT_CHUNK_SIZE,
    sorcha_workers=DEFAULT_SORCHA_WORKERS,
    sorcha_timeout=None,
    resume_chunks=True,
    only_chunks=None,
    ignore_objects_path=None,
    ignore_object_ids=None,
    debug_failed_chunk_size=0,
):
    """Run Ponder on the given configs."""
    db_path = Path(db_path)
    object_path = Path(object_path)
    config_path = Path(config_path)
    sorcha_workers = max(1, sorcha_workers)
    selected_chunks = parse_chunk_indices(only_chunks)

    WORK_DIR.mkdir(exist_ok=True)
    RESULTS_DIR.mkdir(exist_ok=True)

    # load persisted state
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"last_mjd": 0.0}
    prev_hashes = json.loads(HASHES_FILE.read_text()) if HASHES_FILE.exists() else {}

    objects = json.loads(object_path.read_text())
    ignore_ids = read_ignore_ids(ignore_objects_path, ignore_object_ids)
    objects = filter_ignored_objects(objects, ignore_ids)
    if filter_orbits:
        unfiltered_count = len(objects)
        objects = filter_orbit_objects(objects, comet=comet)
        print(f"  Orbit filter — kept: {len(objects)}  removed: {unfiltered_count - len(objects)}")

    if not prev_hashes:
        first_run_setup(objects, state, db_path)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n[{ts}] Starting cycle")

    # -- load and diff inputs --
    new_ids, updated_ids, unchanged_ids = diff_objects(objects, prev_hashes)
    print(
        f"  Objects — new: {len(new_ids)}  updated: {len(updated_ids)}  " f"unchanged: {len(unchanged_ids)}"
    )

    new_pts_db = WORK_DIR / "new_pointings.db"
    n_new_pts = extract_new_pointings(db_path, state["last_mjd"], new_pts_db)
    total_pts = db_count(db_path)
    print(f"  Pointings — new: {n_new_pts}  total: {total_pts}")

    #  -- job unchanged_and_new: new pointings * unchanged + new objects --
    job_uan_ids = set(unchanged_ids) | set(new_ids)
    unchanged_and_new_done = run_id_set(
        objects,
        job_uan_ids,
        "unchanged_and_new",
        ts,
        comet,
        new_pts_db,
        config_path,
        chunk_size,
        sorcha_workers,
        timeout=sorcha_timeout,
        resume=resume_chunks,
        only_chunks=selected_chunks,
        debug_failed_chunk_size=debug_failed_chunk_size,
    )

    updated_done = run_id_set(
        objects,
        updated_ids,
        "updated",
        ts,
        comet,
        db_path,
        config_path,
        chunk_size,
        sorcha_workers,
        timeout=sorcha_timeout,
        resume=resume_chunks,
        only_chunks=selected_chunks,
        debug_failed_chunk_size=debug_failed_chunk_size,
    )

    if not unchanged_and_new_done or not updated_done:
        print("  Partial chunk run requested; state not updated")
        return

    # -- persist state for next run --
    last_mjd = db_max_mjd(db_path)
    state["last_mjd"] = last_mjd
    STATE_FILE.write_text(json.dumps(state))


def main():
    from .__main__ import main as cli_main

    return cli_main()
