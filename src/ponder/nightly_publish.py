"""Resumable columnar publication of completed DECam prediction checkpoints.

This file can also run as a standalone script with the original run/code on
PYTHONPATH. The frozen prediction package is never modified by publication.
"""

import argparse
import hashlib
import json
import multiprocessing
import resource
import shutil
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ponder.decam_pointings import writer_lock
from ponder.predictions import OUTPUT_SCHEMA, atomic_json, file_hash

KEYS = ["exposure_id", "object_type", "orbit_solution_id"]
BATCH = 65536


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def load(path):
    return json.loads(Path(path).read_text())


def stamp():
    return datetime.now(timezone.utc).isoformat()


@lru_cache(maxsize=4)
def run_manifest(run):
    return load(Path(run) / "manifest.json")


def log(message):
    print(f"{stamp()} {message}", flush=True)


def code_hash(directory):
    digest = hashlib.sha256()
    for path in sorted(Path(directory).glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


@lru_cache(maxsize=2)
def routing(run):
    manifest = load(Path(run) / "manifest.json")
    with sqlite3.connect(Path(manifest["db"]).as_uri() + "?mode=ro", uri=True) as con:
        nights = con.execute("SELECT * FROM nights ORDER BY observing_date").fetchall()
        exposures = con.execute(
            "SELECT observationId,observing_date FROM observations ORDER BY observationId"
        ).fetchall()
    names = [row[0] for row in nights]
    codes = {name: i for i, name in enumerate(names)}
    ids = np.array([row[0] for row in exposures], dtype=np.int64)
    dates = np.array([codes[row[1]] for row in exposures], dtype=np.int32)
    return nights, pa.array(names), ids, dates


def validate_rows(table, run):
    """Vectorized validation of routing, populations, keys, and field radius."""
    _, nights, exposure_ids, exposure_dates = routing(str(run))
    for name in KEYS + ["observing_date", "field_separation_deg"]:
        require(table[name].null_count == 0, f"Null {name}")
    date_codes = pc.index_in(table["observing_date"], value_set=nights)
    require(date_codes.null_count == 0, "Unknown observing night")
    dates = date_codes.to_numpy(zero_copy_only=False)
    ids = table["exposure_id"].to_numpy(zero_copy_only=False)
    positions = np.searchsorted(exposure_ids, ids)
    require(np.all(positions < len(exposure_ids)), "Unknown exposure")
    require(np.array_equal(exposure_ids[positions], ids), "Unknown exposure")
    require(np.array_equal(exposure_dates[positions], dates), "Incorrect exposure/night routing")
    populations = pc.index_in(table["object_type"], value_set=pa.array(["comet", "non_comet"]))
    require(populations.null_count == 0, "Unknown population")
    sep = table["field_separation_deg"].to_numpy(zero_copy_only=False)
    radius = run_manifest(str(run))["radius_deg"]
    require(np.all(np.isfinite(sep) & (sep >= 0) & (sep <= radius)), "Invalid field separation")
    return dates, populations.to_numpy(zero_copy_only=False)


def inventory(run, work, buckets=64, group_size=128, selected=None):
    run, work = Path(run).resolve(), Path(work).resolve()
    manifest = load(run / "manifest.json")
    manifest_hash = file_hash(run / "manifest.json")
    publisher_hash = file_hash(__file__)
    require(file_hash(manifest["db"]) == manifest["db_sha256"], "Pointing database changed")
    target = work / "inventory.json"
    if target.exists():
        saved = load(target)
        require(
            saved["manifest_sha256"] == manifest_hash and saved["publisher_sha256"] == publisher_hash,
            "Publication inputs or code changed; use a new work directory",
        )
        require(
            saved["buckets"] == buckets
            and saved["group_size"] == group_size
            and saved["selected"] == selected,
            "Publication configuration changed",
        )
        return saved
    expected_source = load(run / "execution.json")["source_hash"]
    require(code_hash(run / "code/ponder") == expected_source, "Frozen prediction source changed")
    chunks, failures, seen = [], [], set()
    total = 0
    for chunk in manifest["chunks"]:
        if selected is not None and chunk["index"] not in selected:
            continue
        marker = run / "checkpoints" / f"{chunk['index']:06d}" / "done.json"
        require(marker.exists(), f"Unfinished checkpoint {chunk['index']}")
        done = load(marker)
        require(done["input_sha256"] == chunk["sha256"], "Checkpoint input fingerprint mismatch")
        require(done["input_rows"] == chunk["rows"], "Checkpoint input count mismatch")
        require(done.get("source_hash", expected_source) == expected_source, "Checkpoint source mismatch")
        require(
            sum(item["orbits"] for item in done["outputs"]) + len(done["excluded_orbits"]) == chunk["rows"],
            "Checkpoint orbit accounting mismatch",
        )
        outputs = []
        for item in done["outputs"]:
            path = str(Path(item["path"]).resolve())
            require(path not in seen, "Checkpoint output listed more than once")
            seen.add(path)
            require(item["source_hash"] == expected_source, "Leaf source fingerprint mismatch")
            source = pq.ParquetFile(path)
            require(
                source.schema_arrow.equals(OUTPUT_SCHEMA, check_metadata=False), "Checkpoint schema mismatch"
            )
            require(source.metadata.num_rows == item["rows"], "Checkpoint footer count mismatch")
            outputs.append({**item, "path": path})
            total += item["rows"]
        chunks.append(
            {
                "index": chunk["index"],
                "outputs": outputs,
                "marker": str(marker),
                "marker_sha256": file_hash(marker),
            }
        )
        failures.extend(done["excluded_orbits"])
    require(chunks, "No checkpoints selected")
    result = {
        "run": str(run),
        "manifest_sha256": manifest_hash,
        "publisher_sha256": publisher_hash,
        "source_hash": expected_source,
        "selected": selected,
        "buckets": buckets,
        "group_size": group_size,
        "groups": [chunks[i : i + group_size] for i in range(0, len(chunks), group_size)],
        "rows": total,
        "checkpoints": len(chunks),
        "excluded_orbits": failures,
    }
    atomic_json(target, result)
    return result


def verify_artifacts(directory, marker, inventory_hash):
    require(marker["inventory_sha256"] == inventory_hash, "Stale publication checkpoint")
    for item in marker["files"]:
        path = directory / item["name"]
        require(path.exists() and file_hash(path) == item["sha256"], f"Corrupt partition: {path}")


def map_group(work, index):
    pa.set_cpu_count(2)
    work = Path(work)
    inv = load(work / "inventory.json")
    fingerprint = file_hash(work / "inventory.json")
    target = work / "map" / f"{index:06d}"
    if (target / "done.json").exists():
        saved = load(target / "done.json")
        verify_artifacts(target, saved, fingerprint)
        return {"stage": "map", "index": index, "resumed": True}
    temporary = target.with_name(target.name + ".tmp")
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    nights = routing(inv["run"])[0]
    counts = np.zeros((len(nights), 2), dtype=np.int64)
    buffers = [[] for _ in range(inv["buckets"])]
    buffered = np.zeros(inv["buckets"], dtype=np.int64)
    byte_counts = np.zeros(inv["buckets"], dtype=np.int64)
    rows = np.zeros(inv["buckets"], dtype=np.int64)
    writers = {}

    def flush(bucket):
        if not buffers[bucket]:
            return
        table = pa.concat_tables(buffers[bucket])
        if bucket not in writers:
            writers[bucket] = pq.ParquetWriter(
                temporary / f"bucket-{bucket:03d}.parquet", OUTPUT_SCHEMA, compression="zstd"
            )
        writers[bucket].write_table(table, row_group_size=BATCH)
        buffers[bucket].clear()
        buffered[bucket] = 0

    try:
        for chunk in inv["groups"][index]:
            require(file_hash(chunk["marker"]) == chunk["marker_sha256"], "Checkpoint marker changed")
            for output in chunk["outputs"]:
                require(file_hash(output["path"]) == output["sha256"], "Corrupt checkpoint output")
                actual = 0
                for batch in pq.ParquetFile(output["path"]).iter_batches(batch_size=BATCH):
                    table = pa.Table.from_batches([batch], schema=OUTPUT_SCHEMA)
                    dates, populations = validate_rows(table, inv["run"])
                    counts += np.bincount(dates * 2 + populations, minlength=len(nights) * 2).reshape(-1, 2)
                    bucket_ids = dates % inv["buckets"]
                    order = np.argsort(bucket_ids, kind="stable")
                    ordered = table.take(pa.array(order))
                    lengths = np.bincount(bucket_ids, minlength=inv["buckets"])
                    offset = 0
                    for bucket, length in enumerate(lengths):
                        if length:
                            part = ordered.slice(offset, int(length))
                            # Copy each partition out of its parent batch so buffers
                            # cannot retain whole unrelated batches through slices.
                            part = part.take(pa.array(np.arange(length, dtype=np.int64)))
                            buffers[bucket].append(part)
                            buffered[bucket] += length
                            rows[bucket] += length
                            byte_counts[bucket] += part.nbytes
                            offset += length
                            if buffered[bucket] >= BATCH:
                                flush(bucket)
                    while buffered.sum() > 500000:
                        flush(int(buffered.argmax()))
                    actual += len(table)
                require(actual == output["rows"], "Checkpoint row count changed")
        for bucket in range(inv["buckets"]):
            flush(bucket)
    finally:
        for writer in writers.values():
            writer.close()
    files = [
        {
            "name": f"bucket-{bucket:03d}.parquet",
            "bucket": bucket,
            "sha256": file_hash(temporary / f"bucket-{bucket:03d}.parquet"),
            "rows": int(rows[bucket]),
            "arrow_bytes": int(byte_counts[bucket]),
        }
        for bucket in sorted(writers)
    ]
    report = {
        "inventory_sha256": fingerprint,
        "files": files,
        "counts": counts.tolist(),
        "rows": int(rows.sum()),
        "max_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }
    require(report["rows"] == int(counts.sum()), "Partition row accounting mismatch")
    atomic_json(temporary / "done.json", report)
    require(not target.exists(), "Uncommitted map directory exists")
    temporary.replace(target)
    return {"stage": "map", "index": index, "rows": report["rows"], "max_rss_kb": report["max_rss_kb"]}


def audit_night(path, night, run, expected_counts, manifest_hash):
    source = pq.ParquetFile(path)
    require(source.schema_arrow.equals(OUTPUT_SCHEMA, check_metadata=False), "Nightly schema mismatch")
    metadata = source.schema_arrow.metadata or {}
    require(metadata.get(b"ponder.manifest_sha256") == manifest_hash.encode(), "Nightly manifest mismatch")
    counts = np.zeros(2, dtype=np.int64)
    previous = None
    for batch in source.iter_batches(
        batch_size=BATCH, columns=KEYS + ["observing_date", "field_separation_deg"]
    ):
        table = pa.Table.from_batches([batch])
        _, populations = validate_rows(table, run)
        require(pc.all(pc.equal(table["observing_date"], night)).as_py(), "Wrong nightly file")
        counts += np.bincount(populations, minlength=2)
        if len(table):
            first = tuple(table[key][0].as_py() for key in KEYS)
            require(previous is None or first > previous, "Duplicate or unordered key across batches")
            # Check both order and uniqueness using Arrow kernels, without row dictionaries.
            order = pc.sort_indices(table.select(KEYS), sort_keys=[(k, "ascending") for k in KEYS])
            require(np.array_equal(order.to_numpy(), np.arange(len(table))), "Unordered nightly keys")
            equal = None
            for key in KEYS:
                same = pc.equal(table[key].slice(1), table[key].slice(0, len(table) - 1))
                equal = same if equal is None else pc.and_(equal, same)
            require(not pc.any(equal).as_py(), "Duplicate nightly key")
            previous = tuple(table[key][-1].as_py() for key in KEYS)
    require(counts.tolist() == list(expected_counts), "Nightly population counts disagree")
    require(source.metadata.num_rows == int(counts.sum()), "Nightly footer count mismatch")
    return file_hash(path)


class PartitionTooLarge(Exception):
    pass


def read_partition(paths, names, limit):
    tables, size = [], 0
    values = pa.array(names)
    for path in paths:
        for batch in pq.ParquetFile(path).iter_batches(batch_size=BATCH):
            table = pa.Table.from_batches([batch])
            table = table.filter(pc.is_in(table["observing_date"], value_set=values))
            if len(table):
                size += table.nbytes
                if size > limit:
                    raise PartitionTooLarge()
                tables.append(table)
    return pa.concat_tables(tables) if tables else pa.Table.from_batches([], schema=OUTPUT_SCHEMA)


def reduce_bucket(work, bucket, memory_limit=4 * 1024**3):
    pa.set_cpu_count(2)
    work = Path(work)
    inv = load(work / "inventory.json")
    fingerprint = file_hash(work / "inventory.json")
    nights = routing(inv["run"])[0]
    counts = np.zeros((len(nights), 2), dtype=np.int64)
    paths = []
    for index in range(len(inv["groups"])):
        directory = work / "map" / f"{index:06d}"
        saved = load(directory / "done.json")
        require(saved["inventory_sha256"] == fingerprint, "Stale map checkpoint")
        counts += np.array(saved["counts"], dtype=np.int64)
        for item in saved["files"]:
            if item["bucket"] == bucket:
                path = directory / item["name"]
                require(file_hash(path) == item["sha256"], "Corrupt map partition")
                paths.append(path)
    directory = work / "nightly"
    directory.mkdir(exist_ok=True)
    pending = []
    for i in range(bucket, len(nights), inv["buckets"]):
        night = nights[i][0]
        marker = work / "nights" / f"{night}.json"
        if marker.exists():
            saved = load(marker)
            require(saved["inventory_sha256"] == fingerprint, "Stale nightly marker")
            require(file_hash(directory / f"{night}.parquet") == saved["sha256"], "Corrupt completed night")
        else:
            pending.append(i)

    def publish(indices):
        if not indices:
            return
        try:
            table = read_partition(paths, [nights[i][0] for i in indices], memory_limit)
        except PartitionTooLarge:
            require(len(indices) > 1, "Single night exceeds publication memory limit")
            mid = len(indices) // 2
            publish(indices[:mid])
            publish(indices[mid:])
            return
        order = pc.sort_indices(
            table, sort_keys=[("observing_date", "ascending")] + [(k, "ascending") for k in KEYS]
        )
        offset = 0
        for i in indices:
            night, catalog_count, usable_count = nights[i]
            count = int(counts[i].sum())
            # Indices must be in observing-date order, matching the sort above.
            selected = order.slice(offset, count)
            temporary = directory / f"{night}.tmp"
            schema = OUTPUT_SCHEMA.with_metadata(
                {
                    b"ponder.run_id": Path(inv["run"]).name.encode(),
                    b"ponder.manifest_sha256": inv["manifest_sha256"].encode(),
                }
            )
            with pq.ParquetWriter(temporary, schema, compression="zstd") as writer:
                for start in range(0, count, BATCH):
                    writer.write_table(
                        table.take(selected.slice(start, BATCH)).replace_schema_metadata(schema.metadata)
                    )
            checksum = audit_night(temporary, night, inv["run"], counts[i], inv["manifest_sha256"])
            temporary.replace(directory / f"{night}.parquet")
            atomic_json(
                work / "nights" / f"{night}.json",
                {
                    "inventory_sha256": fingerprint,
                    "observing_date": night,
                    "catalog_exposures": catalog_count,
                    "usable_exposures": usable_count,
                    "excluded_exposures": catalog_count - usable_count,
                    "comets": int(counts[i, 0]),
                    "non_comets": int(counts[i, 1]),
                    "rows": count,
                    "sha256": checksum,
                    "status": (
                        "complete_with_exclusions"
                        if inv["excluded_orbits"]
                        or catalog_count != usable_count
                        or any(c["excluded"] for c in run_manifest(inv["run"])["orbit_counts"].values())
                        else "complete"
                    ),
                },
            )
            offset += count
        require(offset == len(table), "Partition rows do not reconcile to nights")

    publish(pending)
    result = {
        "stage": "reduce",
        "bucket": bucket,
        "new_nights": len(pending),
        "max_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }
    atomic_json(work / "reduce" / f"{bucket:03d}.json", result)
    return result


def seal(run, work, pilot=False):
    run, work = Path(run), Path(work)
    inv = load(work / "inventory.json")
    require(file_hash(run / "manifest.json") == inv["manifest_sha256"], "Run manifest changed")
    manifest = load(run / "manifest.json")
    fingerprint = file_hash(work / "inventory.json")
    records = [load(work / "nights" / f"{night[0]}.json") for night in routing(str(run))[0]]
    nightly_root = work / "nightly"
    if not nightly_root.exists() and not pilot:
        nightly_root = run / "nightly"
    require(all(row["inventory_sha256"] == fingerprint for row in records), "Nightly provenance mismatch")
    require(sum(row["rows"] for row in records) == inv["rows"], "Publication row accounting mismatch")
    for row in records:
        require(
            file_hash(nightly_root / f"{row['observing_date']}.parquet") == row["sha256"],
            "Nightly checksum mismatch",
        )
    report = {
        "published_utc": stamp(),
        "rows": inv["rows"],
        "nights": len(records),
        "failed_orbits": len(inv["excluded_orbits"]),
        "exposure_counts": manifest["exposure_counts"],
        "orbit_counts": manifest["orbit_counts"],
        "manifest_sha256": inv["manifest_sha256"],
        "publisher_sha256": inv["publisher_sha256"],
        "pilot": pilot,
    }
    if pilot:
        atomic_json(work / "publication.json", report)
        return report
    require(inv["selected"] is None, "Cannot publish a checkpoint subset")
    require(len(manifest["chunks"]) == inv["checkpoints"], "Missing checkpoints")
    with sqlite3.connect(Path(manifest["db"]).as_uri() + "?mode=ro", uri=True) as con:
        columns = [row[1] for row in con.execute("PRAGMA table_info(excluded_exposures)")]
        rejected = [dict(zip(columns, row)) for row in con.execute("SELECT * FROM excluded_exposures")]
    import pandas as pd

    pd.DataFrame(rejected, columns=columns).to_parquet(run / "excluded_exposures.parquet", index=False)
    for population, counts in manifest["orbit_counts"].items():
        with (run / "inputs" / population / "excluded_orbits.jsonl").open() as source:
            require(
                sum(bool(line.strip()) for line in source) == counts["excluded"],
                "Catalog exclusion count mismatch",
            )
    require(len(rejected) == manifest["exposure_counts"]["excluded"], "Exposure exclusion count mismatch")
    atomic_json(run / "failed_orbits.json", inv["excluded_orbits"])
    coverage = [{k: v for k, v in row.items() if k != "inventory_sha256"} for row in records]
    pq.write_table(pa.Table.from_pylist(coverage), run / "nightly_coverage.parquet", compression="zstd")
    checksums = "".join(f"{row['sha256']}  {row['observing_date']}.parquet\n" for row in records)
    (nightly_root / "SHA256SUMS").write_text(checksums)
    target = run / "nightly"
    if nightly_root != target:
        require(not target.exists(), "Existing nightly directory requires explicit reconciliation")
        nightly_root.replace(target)
    atomic_json(run / "publication.json", report)
    verified = {
        **report,
        "passed": True,
        "verified_utc": stamp(),
        "checkpoints": inv["checkpoints"],
        "unique_keys": True,
        "populations": {
            "comet": sum(r["comets"] for r in records),
            "non_comet": sum(r["non_comets"] for r in records),
        },
        "empty_nights": sum(r["rows"] == 0 for r in records),
        "propagation_exclusions": inv["excluded_orbits"],
    }
    atomic_json(run / "verification.json", verified)
    return verified


def execute(run, work, workers=4, pilot=False, buckets=64, group_size=128):
    run, work = Path(run).resolve(), Path(work).resolve()
    manifest = load(run / "manifest.json")
    selected = None
    if pilot:
        special = [c["index"] for c in manifest["chunks"] if c["object_type"] == "comet"]
        if len(manifest["chunks"]) > 5159:
            special.append(5159)
        regular = [c["index"] for c in manifest["chunks"] if c["index"] not in special]
        needed = min(len(regular), max(0, 128 - len(special)))
        selected = sorted(
            set(special + [regular[i] for i in np.linspace(0, len(regular) - 1, needed, dtype=int)])
        )
    start = time.monotonic()
    with writer_lock(run / "finalize"):
        if (run / "verification.json").exists() and not pilot:
            saved = load(run / "verification.json")
            require(saved.get("passed"), "Existing verification did not pass")
            require(
                saved["manifest_sha256"] == file_hash(run / "manifest.json"), "Published manifest changed"
            )
            for row in pq.read_table(run / "nightly_coverage.parquet").to_pylist():
                require(
                    file_hash(run / "nightly" / f"{row['observing_date']}.parquet") == row["sha256"],
                    "Published checksum mismatch",
                )
            return saved
        inv = inventory(run, work, buckets, group_size, selected)
        if (run / "nightly").exists() and not pilot:
            return seal(run, work)
        log(
            f"Inventory: {inv['checkpoints']} checkpoints, {inv['rows']} rows, {len(inv['groups'])} map groups"
        )
        stats = []
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
            for function, items in [(map_group, range(len(inv["groups"]))), (reduce_bucket, range(buckets))]:
                futures = [pool.submit(function, str(work), index) for index in items]
                for future in as_completed(futures):
                    result = future.result()
                    stats.append(result)
                    log(json.dumps(result))
        report = seal(run, work, pilot)
        elapsed = time.monotonic() - start
        timing = {
            "elapsed_seconds": elapsed,
            "rows": inv["rows"],
            "checkpoints": inv["checkpoints"],
            "max_worker_rss_kb": max((s.get("max_rss_kb", 0) for s in stats), default=0),
            "workers": workers,
            "tasks": stats,
        }
        if pilot:
            total_rows = sum(
                sum(
                    o["rows"]
                    for o in load(run / "checkpoints" / f"{c['index']:06d}" / "done.json")["outputs"]
                )
                for c in manifest["chunks"]
            )
            timing["projected_seconds_with_2x_margin"] = elapsed * total_rows / max(inv["rows"], 1) * 2
            timing["passed"] = (
                timing["projected_seconds_with_2x_margin"] < 86400
                and timing["max_worker_rss_kb"] * workers < 24 * 1024**2
            )
        atomic_json(work / "timing.json", timing)
        log(json.dumps({**report, "elapsed_seconds": elapsed}))
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    require(1 <= args.workers <= 4, "Workers must be between one and four")
    work = args.work_dir or args.run_dir / "publication_v2" / ("pilot" if args.pilot else "full")
    try:
        execute(args.run_dir, work, args.workers, args.pilot)
    except Exception as exc:
        atomic_json(work / "failure.json", {"time_utc": stamp(), "error": str(exc)})
        raise


if __name__ == "__main__":
    main()
