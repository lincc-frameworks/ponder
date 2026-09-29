#!/usr/bin/env python
"""Recover files written by a cancelled RCC Ponder ingestion transaction."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--writer-pid", required=True)
    parser.add_argument("--instrument", choices=("comcam", "lsstcam"), required=True)
    parser.add_argument(
        "--extra-night-dir",
        action="append",
        default=[],
        help="Additional nightly directory, relative to data-root, containing a new partial write.",
    )
    parser.add_argument("--quarantine", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def move(source: Path, target: Path, *, apply: bool) -> None:
    print(f"MOVE {source} -> {target}")
    if apply:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(source, target)


def main() -> None:
    args = parse_args()
    nightly = args.data_root / "nightly"
    backups = sorted(nightly.glob(f"**/*.old*.{args.writer_pid}.*.parquet"))
    if not backups:
        raise RuntimeError(f"No backups found for writer PID {args.writer_pid}")

    affected_dirs = {backup.parent for backup in backups}
    for backup in backups:
        instrument_root = nightly / args.instrument
        if not backup.is_relative_to(instrument_root):
            raise RuntimeError(
                f"Refusing recovery target outside {args.instrument}: {backup}"
            )
        canonical_name = backup.name.split(".old", 1)[0] + ".parquet"
        canonical = backup.with_name(canonical_name)
        if not canonical.exists():
            raise RuntimeError(f"Missing current file for backup {backup}")
        relative = canonical.relative_to(args.data_root)
        move(canonical, args.quarantine / relative, apply=args.apply)
        move(backup, canonical, apply=args.apply)

    for directory in sorted(affected_dirs):
        for name in ("ponder_results.parquet", "ponder_nightly_update.lock"):
            path = directory / name
            if path.exists():
                relative = path.relative_to(args.data_root)
                move(path, args.quarantine / relative, apply=args.apply)

    for relative_dir in args.extra_night_dir:
        directory = (args.data_root / relative_dir).resolve()
        if not directory.is_relative_to((nightly / args.instrument).resolve()):
            raise RuntimeError(f"Refusing extra directory outside instrument root: {directory}")
        for name in ("ponder_results.parquet", "ponder_nightly_update.lock"):
            path = directory / name
            if path.exists():
                relative = path.relative_to(args.data_root)
                move(path, args.quarantine / relative, apply=args.apply)

    lock = args.data_root / "ponder_updates/ponder_nightly_updates.lock"
    if lock.exists():
        move(lock, args.quarantine / lock.relative_to(args.data_root), apply=args.apply)

    print(f"RECOVERED_BACKUPS {len(backups) if args.apply else 0}")
    print(f"PLANNED_BACKUPS {len(backups)}")


if __name__ == "__main__":
    main()
