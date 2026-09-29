#!/usr/bin/env python
"""Generate RCC cutouts from a pre-normalized Ponder position parquet."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
from rubin_comet_catchers.file_cutouts import _summarize_results, generate_cutouts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_file", type=Path)
    parser.add_argument("--thumbnail-outdir", required=True)
    parser.add_argument("--instrument", default="LSSTCam")
    parser.add_argument("--lookup-workers", type=int, default=8)
    parser.add_argument("--dims", nargs=2, type=int, default=[600, 600])
    parser.add_argument("--to-write", nargs="+", default=["png", "fits"])
    args = parser.parse_args()

    table = pd.read_parquet(args.input_file.expanduser())
    required = {"Name", "Class", "RA", "Dec", "Query_Date_TAI_ISOT"}
    missing = sorted(required - set(table.columns))
    if missing:
        raise ValueError(f"Ponder cutout input is missing columns: {', '.join(missing)}")
    results = generate_cutouts(
        table,
        input_format="ponder",
        instrument=args.instrument,
        thumbnail_outdir=args.thumbnail_outdir,
        lookup_workers=args.lookup_workers,
        dims=args.dims,
        to_write=args.to_write,
    )
    summary = _summarize_results(results)
    summary["failure_reasons"] = dict(summary["failure_reasons"])
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
