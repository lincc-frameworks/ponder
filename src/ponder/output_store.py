"""Disk-backed, atomic updates of a run's consolidated ephemeris observations."""

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pyarrow.parquet as pq


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def identifier(value):
    return '"' + str(value).replace('"', '""') + '"'


@contextmanager
def connection(output):
    """Bound DuckDB memory and clean up per-operation spill files."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    spill_root = Path(os.environ.get("PONDER_SPILL_DIR", output.parent))
    spill_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ponder-output-", dir=spill_root) as scratch:
        con = duckdb.connect(str(Path(scratch) / "output.duckdb"))
        try:
            con.execute("SET memory_limit = " + literal(os.environ.get("PONDER_OUTPUT_MEMORY_LIMIT", "8GB")))
            con.execute("SET threads = " + str(int(os.environ.get("PONDER_OUTPUT_THREADS", "2"))))
            con.execute("SET preserve_insertion_order = false")
            con.execute("SET temp_directory = " + literal(Path(scratch) / "spill"))
            yield con
        finally:
            con.close()


def validate_observation_context(output, context):
    """Do not mix databases, object populations, or scientific configurations."""
    output = Path(output)
    if output.exists():
        metadata = pq.ParquetFile(output).schema_arrow.metadata or {}
        saved = metadata.get(b"ponder_context")
        if saved is None or json.loads(saved) != context:
            raise ValueError("Master observations have a missing or different run context; choose a new path")


def upsert_observations(paths, output, context, id_columns, time_columns):
    """Replace matching key groups, preserving all other observations.

    Keys are object/time plus exposure ID when available. Incoming multiplicity
    is preserved, so repeated execution is idempotent without collapsing visits.
    This is an upsert, not deletion of an object's entire prediction history.
    """
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.with_name(output.name + ".lock")
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise RuntimeError(
            f"Master observations are locked; check for an active/interrupted writer: {lock}"
        ) from exc
    try:
        validate_observation_context(output, context)
        sources = []
        for path in paths:
            path = Path(path)
            if path.resolve() == output.resolve():
                raise ValueError("Master observations cannot also be a job output")
            source = pq.ParquetFile(path)  # Missing/corrupt products must not be silently skipped.
            if source.metadata.num_rows:
                sources.append(path)
        if not sources:
            return

        names = pq.ParquetFile(sources[0]).schema_arrow.names
        object_id = next((n for n in id_columns if n in names), None)
        timestamp = next((n for n in time_columns if n in names), None)
        if object_id is None or timestamp is None:
            raise ValueError("Master observations require object and timestamp columns")
        exposure_columns = ("FieldID", "observationId", "exposure_id")
        exposure = next((n for n in exposure_columns if n in names), None)
        keys = [object_id, timestamp] + ([exposure] if exposure else [])
        for path in sources + ([output] if output.exists() else []):
            columns = pq.ParquetFile(path).schema_arrow.names
            other_exposure = next((n for n in exposure_columns if n in columns), None)
            if not set(keys).issubset(columns) or other_exposure != exposure:
                raise ValueError(f"Incompatible observation key columns in {path}")

        with connection(output) as con:
            filenames = "[" + ",".join(literal(p) for p in sources) + "]"
            con.execute(
                "CREATE VIEW incoming AS SELECT * FROM read_parquet(" + filenames + ", union_by_name=true)"
            )
            # Fail before promotion on invalid keys rather than accumulating
            # rows that cannot be matched reliably on a retry.
            invalid = " OR ".join(f"{identifier(k)} IS NULL" for k in keys)
            invalid += f" OR NOT isfinite(CAST({identifier(timestamp)} AS DOUBLE))"
            if con.execute("SELECT count(*) FROM incoming WHERE " + invalid).fetchone()[0]:
                raise ValueError("Incoming observations contain null or nonfinite keys")
            result = "SELECT * FROM incoming"
            if output.exists():
                con.execute("CREATE VIEW previous AS SELECT * FROM read_parquet(" + literal(output) + ")")
                join = " AND ".join(
                    f"CAST(old.{identifier(k)} AS {'DOUBLE' if k == timestamp else 'VARCHAR'}) = "
                    f"CAST(new.{identifier(k)} AS {'DOUBLE' if k == timestamp else 'VARCHAR'})"
                    for k in keys
                )
                result += (
                    " UNION ALL BY NAME SELECT old.* FROM previous old ANTI JOIN incoming new ON " + join
                )
            temporary = output.with_name(output.name + ".tmp")
            try:
                metadata = "{ponder_context: " + literal(json.dumps(context, sort_keys=True)) + "}"
                con.execute(
                    f"COPY (SELECT * FROM ({result}) ORDER BY CAST({identifier(timestamp)} AS DOUBLE)) "
                    f"TO {literal(temporary)} (FORMAT PARQUET, COMPRESSION SNAPPY, KV_METADATA {metadata})"
                )
                os.replace(temporary, output)
            finally:
                temporary.unlink(missing_ok=True)
    finally:
        lock.rmdir()
