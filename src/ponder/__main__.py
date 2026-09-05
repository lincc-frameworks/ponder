import argparse
from datetime import datetime, timezone
from pathlib import Path

from .runner import (
    DEFAULT_DEBUG_FAILED_CHUNK_SIZE,
    DEFAULT_ISOLATE_FAILING_ROWS,
    UPDATE_MODE_AUTO,
    UPDATE_MODES,
    run_ponder,
)
from .utils import get_current_orbits


def main():
    parser = argparse.ArgumentParser(description="Run Ponder small-body analysis")
    parser.add_argument("--db", type=Path, help="Path to the database file")
    parser.add_argument("--orbits", type=Path, help="Path to the orbits JSON file")
    parser.add_argument("--config", type=Path, help="Path to the configuration file")
    population = parser.add_mutually_exclusive_group()
    population.add_argument(
        "--comet", action="store_true", help="Run the MPC comet population"
    )
    population.add_argument(
        "--neo",
        action="store_true",
        help="Run only MPCORB asteroids with q = a(1-e) < 1.3 au",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path.cwd() / "work",
        help="Directory to store intermediate files",
    )
    parser.add_argument(
        "--download_orbits",
        action="store_true",
        help="Whether to download the latest orbits from MPC",
    )
    parser.add_argument(
        "--no-filter-orbits",
        action="store_true",
        help="Disable the default orbit-catalog filter",
    )
    parser.add_argument(
        "--update-mode",
        choices=UPDATE_MODES,
        default=UPDATE_MODE_AUTO,
        help="Select incremental behavior. auto preserves the default full incremental cycle.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=5000,
        help="Rows per resumable Sorcha chunk. Use 0 to run one legacy Sorcha job.",
    )
    parser.add_argument(
        "--sorcha-workers",
        type=int,
        default=1,
        help="Number of Sorcha chunks to run in parallel",
    )
    parser.add_argument(
        "--sorcha-timeout",
        type=float,
        help="Optional per-chunk Sorcha timeout in seconds",
    )
    parser.add_argument(
        "--debug-failed-chunk-size",
        type=int,
        default=DEFAULT_DEBUG_FAILED_CHUNK_SIZE,
        help=(
            "If a Sorcha chunk fails, split it into this many rows per debug subchunk "
            f"(default: {DEFAULT_DEBUG_FAILED_CHUNK_SIZE})"
        ),
    )
    parser.add_argument(
        "--force-debug-chunking",
        action="store_true",
        help="With --only-chunks, skip parent chunks and directly run debug subchunks",
    )
    parser.add_argument(
        "--isolate-failing-rows",
        dest="isolate_failing_rows",
        action="store_true",
        default=DEFAULT_ISOLATE_FAILING_ROWS,
        help="Recursively split failed debug subchunks until failing rows are isolated (default)",
    )
    parser.add_argument(
        "--no-isolate-failing-rows",
        dest="isolate_failing_rows",
        action="store_false",
        help="Do not recursively split failed debug subchunks after the first debug pass",
    )
    parser.add_argument(
        "--no-resume-chunks",
        action="store_true",
        help="Rerun chunks even if matching completed chunk markers already exist",
    )
    parser.add_argument(
        "--only-chunks",
        help="Comma-separated chunk indices or ranges to run for debugging, e.g. 12,18-20",
    )
    parser.add_argument(
        "--ignore-objects",
        type=Path,
        help="Path to a text or CSV file of object IDs to skip",
    )
    parser.add_argument(
        "--ignore-object",
        action="append",
        default=[],
        help="Object ID to skip. May be supplied multiple times",
    )

    args = parser.parse_args()
    orbit_path = args.orbits
    if args.download_orbits:
        date_str = datetime.now(timezone.utc).strftime("%d-%m-%Y")
        orbit_path = get_current_orbits(date_str, args.work_dir, comet=args.comet)
    if not orbit_path:
        print("Error: Must provide --orbits or --download_orbits")
        return
    if args.db is None:
        if args.download_orbits:
            print(
                f"No database supplied; downloaded orbits to {orbit_path}. Skipping Ponder analysis."
            )
        else:
            print("Error: Must provide --db to run Ponder analysis")
        return
    if args.comet:
        print("Running comet analysis")
    elif args.neo:
        print("Running NEO analysis")
    else:
        print("Running asteroid analysis")
    run_ponder(
        args.db,
        orbit_path,
        args.config,
        comet=args.comet,
        filter_orbits=not args.no_filter_orbits,
        chunk_size=args.chunk_size,
        sorcha_workers=args.sorcha_workers,
        sorcha_timeout=args.sorcha_timeout,
        resume_chunks=not args.no_resume_chunks,
        only_chunks=args.only_chunks,
        ignore_objects_path=args.ignore_objects,
        ignore_object_ids=args.ignore_object,
        debug_failed_chunk_size=args.debug_failed_chunk_size,
        force_debug_chunking=args.force_debug_chunking,
        isolate_failing_rows=args.isolate_failing_rows,
        update_mode=args.update_mode,
        neo=args.neo,
    )


if __name__ == "__main__":
    main()
