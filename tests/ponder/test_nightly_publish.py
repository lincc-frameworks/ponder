import json
import sqlite3
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ponder import nightly_publish as pub
from ponder.predictions import OUTPUT_SCHEMA, atomic_json, file_hash


@pytest.fixture
def run(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    frozen = run / "code/ponder"
    frozen.mkdir(parents=True)
    (frozen / "science.py").write_text("# frozen prediction source\n")
    source_hash = pub.code_hash(frozen)
    atomic_json(run / "execution.json", {"source_hash": source_hash})
    db = run / "pointings.db"
    with sqlite3.connect(db) as con:
        con.executescript(
            """
            CREATE TABLE observations (observationId INTEGER PRIMARY KEY, observing_date TEXT);
            INSERT INTO observations VALUES (10,'2024-01-01'),(20,'2024-01-02'),(30,'2024-01-03');
            CREATE TABLE nights (observing_date TEXT PRIMARY KEY, catalog_exposures INTEGER, usable_exposures INTEGER);
            INSERT INTO nights VALUES ('2024-01-01',1,1),('2024-01-02',1,1),('2024-01-03',1,1);
            CREATE TABLE excluded_exposures (exposure_id INTEGER, observing_date TEXT, reasons TEXT, source_json TEXT);
        """
        )
    chunks = []
    for index, population in enumerate(["comet", "non_comet"]):
        rows = []
        for exposure_id, night in [(20, "2024-01-02"), (10, "2024-01-01")]:
            row = {field.name: ("" if pa.types.is_string(field.type) else 0) for field in OUTPUT_SCHEMA}
            row.update(
                ObjID="shared display designation",
                FieldID=str(exposure_id),
                exposure_id=exposure_id,
                observing_date=night,
                object_type=population,
                orbit_solution_id=population + "-1",
                object_designation="shared display designation",
                ponder_catalog_row=index,
                filter="original filter",
                archive_filename="image.fits",
                field_separation_deg=1.3,
            )
            rows.append(row)
        path = run / f"leaf-{index}.parquet"
        pq.write_table(pa.Table.from_pylist(rows, schema=OUTPUT_SCHEMA), path)
        chunk = {"index": index, "sha256": f"input-{index}", "rows": 1, "object_type": population}
        chunks.append(chunk)
        atomic_json(
            run / "checkpoints" / f"{index:06d}" / "done.json",
            {
                "input_sha256": chunk["sha256"],
                "input_rows": 1,
                "source_hash": source_hash,
                "excluded_orbits": [],
                "outputs": [
                    {
                        "path": str(path),
                        "sha256": file_hash(path),
                        "rows": 2,
                        "orbits": 1,
                        "source_hash": source_hash,
                    }
                ],
            },
        )
        directory = run / "inputs" / population
        directory.mkdir(parents=True)
        (directory / "excluded_orbits.jsonl").write_text("")
    atomic_json(
        run / "manifest.json",
        {
            "db": str(db),
            "db_sha256": file_hash(db),
            "chunks": chunks,
            "radius_deg": 1.3,
            "exposure_counts": {"catalog": 3, "usable": 3, "excluded": 0},
            "orbit_counts": {
                mode: {"input": 1, "accepted": 1, "excluded": 0} for mode in ["comet", "non_comet"]
            },
        },
    )
    return run


def prepare(run):
    work = run / "publication_v2/full"
    pub.inventory(run, work, buckets=2, group_size=1)
    for index in range(2):
        pub.map_group(work, index)
    return work


def rewrite_leaf(run, transform, index=0):
    path = run / f"leaf-{index}.parquet"
    table = transform(pq.read_table(path))
    pq.write_table(table, path)
    marker = run / "checkpoints" / f"{index:06d}" / "done.json"
    done = json.loads(marker.read_text())
    done["outputs"][0].update(sha256=file_hash(path), rows=len(table))
    atomic_json(marker, done)


def test_nightly_mixed_populations_empty_nights_resume_and_values(run):
    work = prepare(run)
    assert pub.map_group(work, 0)["resumed"]
    for bucket in range(2):
        pub.reduce_bucket(work, bucket)
    first = work / "nightly/2024-01-01.parquet"
    stamp = first.stat().st_mtime_ns
    pub.reduce_bucket(work, 0)
    assert first.stat().st_mtime_ns == stamp
    report = pub.seal(run, work)
    assert report["passed"] and report["rows"] == 4 and report["nights"] == 3
    assert report["empty_nights"] == 1
    assert report["populations"] == {"comet": 2, "non_comet": 2}
    assert pq.read_schema(run / "nightly/2024-01-03.parquet").equals(OUTPUT_SCHEMA, check_metadata=False)
    assert pq.read_metadata(run / "nightly/2024-01-03.parquet").num_rows == 0
    expected = pa.concat_tables([pq.read_table(run / f"leaf-{i}.parquet") for i in range(2)])
    actual = pa.concat_tables([pq.read_table(p) for p in sorted((run / "nightly").glob("*.parquet"))])
    sort_keys = [("observing_date", "ascending")] + [(key, "ascending") for key in pub.KEYS]
    assert expected.sort_by(sort_keys).equals(actual.sort_by(sort_keys), check_metadata=False)
    assert pub.execute(run, work)["passed"]


def test_duplicate_across_checkpoint_leaves_is_rejected(run):
    # A duplicate is not silently deduplicated, even across different leaves.
    path = run / "leaf-0.parquet"
    table = pq.read_table(path)
    rewrite_leaf(run, lambda other: pa.concat_tables([other, table.slice(0, 1)]), index=1)
    work = prepare(run)
    with pytest.raises(RuntimeError, match="Duplicate"):
        pub.reduce_bucket(work, 1)
    assert not (run / "publication.json").exists()


def test_wrong_exposure_night_is_rejected(run):
    def wrong(table):
        rows = table.to_pylist()
        rows[0]["observing_date"] = "2024-01-01"
        return pa.Table.from_pylist(rows, schema=OUTPUT_SCHEMA)

    rewrite_leaf(run, wrong)
    work = run / "work"
    pub.inventory(run, work, buckets=2, group_size=1)
    with pytest.raises(RuntimeError, match="routing"):
        pub.map_group(work, 0)


def test_missing_checkpoint_and_corrupt_leaf_fail_closed(run):
    marker = run / "checkpoints/000001/done.json"
    payload = marker.read_bytes()
    marker.unlink()
    with pytest.raises(RuntimeError, match="Unfinished"):
        pub.inventory(run, run / "work")
    marker.write_bytes(payload)
    pub.inventory(run, run / "work", group_size=1)
    path = run / "leaf-0.parquet"
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(RuntimeError, match="Corrupt checkpoint"):
        pub.map_group(run / "work", 0)


def test_partial_map_and_night_recover_and_completed_corruption_blocks(run):
    work = run / "work"
    pub.inventory(run, work, buckets=2, group_size=1)
    partial = work / "map/000000.tmp"
    partial.mkdir(parents=True)
    (partial / "partial.parquet").write_bytes(b"incomplete")
    pub.map_group(work, 0)
    pub.map_group(work, 1)
    (work / "nightly").mkdir()
    (work / "nightly/2024-01-01.tmp").write_bytes(b"incomplete")
    pub.reduce_bucket(work, 0)
    (work / "nightly/2024-01-01.parquet").write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="Corrupt completed night"):
        pub.reduce_bucket(work, 0)


def test_bucket_splits_when_memory_limit_requires_it(run):
    work = run / "work"
    pub.inventory(run, work, buckets=1, group_size=1)
    pub.map_group(work, 0)
    pub.map_group(work, 1)
    table = pq.read_table(run / "leaf-0.parquet")
    pub.reduce_bucket(work, 0, memory_limit=table.nbytes + 100)
    assert len(list((work / "nights").glob("*.json"))) == 3


def test_crash_after_directory_rename_can_seal(run):
    work = prepare(run)
    for bucket in range(2):
        pub.reduce_bucket(work, bucket)
    (work / "nightly").replace(run / "nightly")
    assert pub.seal(run, work)["passed"]


def test_recovered_leaf_and_explicit_exclusion_accounting(run):
    marker = run / "checkpoints/000000/done.json"
    done = json.loads(marker.read_text())
    done.pop("source_hash")  # The recovered checkpoint records hashes per leaf.
    done["input_rows"] = 2
    done["excluded_orbits"] = [{"object_designation": "2020 CD3", "reason": "repeated_single_orbit_timeout"}]
    atomic_json(marker, done)
    manifest = json.loads((run / "manifest.json").read_text())
    manifest["chunks"][0]["rows"] = 2
    atomic_json(run / "manifest.json", manifest)
    inv = pub.inventory(run, run / "work")
    assert len(inv["excluded_orbits"]) == 1


def test_corrupt_intermediate_partition_blocks_reduction(run):
    work = prepare(run)
    path = next((work / "map/000000").glob("*.parquet"))
    bucket = int(path.stem.split("-")[-1])
    path.write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="Corrupt map partition"):
        pub.reduce_bucket(work, bucket)


def test_process_pool_coordinator(run):
    result = pub.execute(run, run / "parallel", workers=2, buckets=2, group_size=1)
    assert result["passed"] and result["rows"] == 4


def test_duplicate_key_across_audit_batches(run, monkeypatch):
    table = pq.read_table(run / "leaf-0.parquet").slice(0, 1)
    path = run / "duplicate.parquet"
    schema = OUTPUT_SCHEMA.with_metadata({b"ponder.manifest_sha256": b"expected"})
    pq.write_table(pa.concat_tables([table, table]).replace_schema_metadata(schema.metadata), path)
    monkeypatch.setattr(pub, "BATCH", 1)
    with pytest.raises(RuntimeError, match="across batches"):
        pub.audit_night(path, "2024-01-02", run, [2, 0], "expected")
