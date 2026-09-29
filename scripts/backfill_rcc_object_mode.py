#!/usr/bin/env python
"""Atomically add a default object_mode column to RCC nightly Ponder parquets."""

from __future__ import annotations

import argparse
import os
import uuid
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def backfill_file(
    path: Path, object_mode: str, *, apply: bool, batch_size: int = 250_000
) -> str:
    parquet = pq.ParquetFile(path)
    if "object_mode" in parquet.schema_arrow.names:
        return "already_labelled"
    if not apply:
        return "would_label"

    schema = pa.schema(
        [
            *parquet.schema_arrow.remove_metadata(),
            pa.field("object_mode", pa.string(), nullable=False),
        ]
    )
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with pq.ParquetWriter(tmp_path, schema, compression="snappy") as writer:
            for batch in parquet.iter_batches(batch_size=batch_size):
                table = pa.Table.from_batches([batch])
                mode = pa.array([object_mode] * len(table), type=pa.string())
                writer.write_table(
                    table.append_column("object_mode", mode).cast(schema)
                )
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return "labelled"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="RCC data root or nightly subtree")
    parser.add_argument(
        "--object-mode", choices=("asteroid", "comet"), default="asteroid"
    )
    parser.add_argument("--filename", default="ponder_results.parquet")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    paths = sorted(args.root.expanduser().resolve().rglob(args.filename))
    counts: dict[str, int] = {}
    for path in paths:
        status = backfill_file(path, args.object_mode, apply=args.apply)
        counts[status] = counts.get(status, 0) + 1
        print(f"{status}: {path}")
    print(f"files={len(paths)} statuses={counts}")


if __name__ == "__main__":
    main()
