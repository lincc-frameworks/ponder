#!/usr/bin/env python
"""Fail when RCC nightly unified products are stale or lose Ponder mode labels."""

from __future__ import annotations

import argparse
from pathlib import Path

import pyarrow.parquet as pq
from rubin_comet_catchers.unified_results import _candidate_nightly_dirs, unified_nightly_freshness


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instrument_root", type=Path)
    parser.add_argument(
        "--services",
        nargs="+",
        default=("skybot", "jpl", "ponder"),
    )
    args = parser.parse_args()

    services = tuple(args.services)
    nightly_dirs = _candidate_nightly_dirs(args.instrument_root, services)
    problems: list[str] = []
    ponder_nights = 0

    for nightly_dir in nightly_dirs:
        freshness = unified_nightly_freshness(nightly_dir, services=services)
        if not freshness.is_fresh:
            problems.append(f"{nightly_dir}: unified status={freshness.status}")

        ponder_path = nightly_dir / "ponder_results.parquet"
        if not ponder_path.exists():
            continue
        ponder_nights += 1
        unified_path = nightly_dir / "unified_results.parquet"
        for label, path in (("ponder", ponder_path), ("unified", unified_path)):
            if not path.exists():
                problems.append(f"{nightly_dir}: missing {label} parquet")
                continue
            if "object_mode" not in pq.ParquetFile(path).schema.names:
                problems.append(f"{path}: missing object_mode")

    print(
        f"nightly_dirs={len(nightly_dirs)} ponder_nights={ponder_nights} "
        f"problems={len(problems)}"
    )
    for problem in problems:
        print(f"ERROR: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
