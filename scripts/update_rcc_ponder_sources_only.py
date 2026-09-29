#!/usr/bin/env python
"""Publish Ponder nightly sources while deferring unified rebuilds."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from rubin_comet_catchers import ponder_nightly_updates as updates


def _defer_unified(nightly_dir: str | Path, **_kwargs) -> tuple[Path, int]:
    return Path(nightly_dir) / "unified_results.parquet", 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--ponder-results-dir", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--summary-json", type=Path, required=True)
    parser.add_argument("--start-obsnight", required=True)
    parser.add_argument("--end-obsnight", required=True)
    args = parser.parse_args()

    with patch.object(
        updates, "append_missing_ponder_objects_to_unified", _defer_unified
    ):
        summary = updates.update_ponder_nightlies(
            data_root=args.data_root,
            ponder_results_dir=args.ponder_results_dir,
            start_obsnight=args.start_obsnight,
            end_obsnight=args.end_obsnight,
            ledger_path=args.ledger,
            summary_json=args.summary_json,
            update_master=False,
            refresh_service_comparison=False,
        )
    updates.print_summary(summary)


if __name__ == "__main__":
    main()
