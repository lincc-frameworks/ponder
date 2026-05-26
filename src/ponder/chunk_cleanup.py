"""Maintenance helpers for consolidating and filing Sorcha chunk artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
from pathlib import Path

from .runner import CHUNK_RUNS_DIRNAME, combine_csv_files

RUN_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_job_.+_[0-9a-f]{12}$")


def _read_json(path):
    try:
        return json.loads(Path(path).read_text() or "{}")
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _resolve_path(root, path_text):
    path = Path(path_text)
    return path if path.is_absolute() else Path(root) / path


def _ew_output_path(output_path):
    return output_path.with_name(f"{output_path.stem}_ew.csv")


def _chunk_is_complete(root, chunk):
    output_path = _resolve_path(root, chunk["output_path"])
    done_path = output_path.with_suffix(".done")
    if not done_path.exists():
        return False
    marker = _read_json(done_path)

    # Zero-output chunks are valid when the run marker explicitly says Sorcha
    # completed but had no detection or ephemeris CSV to write.
    output_expected = marker.get("output_exists", True)
    ew_expected = marker.get("ew_output_exists", True)
    return (not output_expected or output_path.exists()) and (
        not ew_expected or _ew_output_path(output_path).exists()
    )


def _manifest_is_complete(root, manifest):
    return all(_chunk_is_complete(root, chunk) for chunk in manifest["chunks"])


def _audit_ok(run_dir):
    audit_path = Path(run_dir) / "output_audit.csv"
    if not audit_path.exists():
        return False

    with audit_path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return False

    for row in rows:
        try:
            missing_rows = int(row.get("missing_rows") or 0)
        except ValueError:
            return False
        if row.get("status") != "ok" or missing_rows:
            return False
    return True


def _final_output_paths(root, manifest_path, manifest):
    run_dir = Path(manifest_path).parent
    date = run_dir.name.split("_job_", 1)[0]
    final_output = run_dir / f"{date}_job_{manifest['job_name']}.csv"
    visible_output = Path(root) / "results" / final_output.name
    return final_output, _ew_output_path(final_output), visible_output, _ew_output_path(visible_output)


def _link_or_copy(src, dst, apply):
    src = Path(src)
    dst = Path(dst)
    if not src.exists():
        return f"skip missing source {src}"
    if dst.exists():
        try:
            if src.samefile(dst):
                return f"already visible {dst}"
        except FileNotFoundError:
            pass
        return f"left existing visible file unchanged {dst}"
    if not apply:
        return f"would expose {src} at {dst}"

    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
        return f"hard-linked {src} to {dst}"
    except OSError:
        # Cloud-synced or cross-device directories can refuse hard links. Copying
        # is slower but keeps the maintenance command useful in those layouts.
        shutil.copy2(src, dst)
        return f"copied {src} to {dst}"


def _combine_or_link_back(source, output, apply):
    source = Path(source)
    output = Path(output)
    if output.exists():
        return f"kept existing combined file {output}"
    if source.exists():
        return _link_or_copy(source, output, apply)
    return None


def _combine_complete_manifest(
    root, manifest, final_output, final_ew_output, visible_output, visible_ew_output, apply
):
    messages = []
    backfill = _combine_or_link_back(visible_output, final_output, apply)
    if backfill:
        messages.append(backfill)
    if not final_output.exists() and not visible_output.exists():
        output_paths = [_resolve_path(root, chunk["output_path"]) for chunk in manifest["chunks"]]
        messages.append(f"{'would combine' if not apply else 'combined'} detections into {final_output}")
        if apply:
            combine_csv_files(output_paths, final_output)

    ew_backfill = _combine_or_link_back(visible_ew_output, final_ew_output, apply)
    if ew_backfill:
        messages.append(ew_backfill)
    if not final_ew_output.exists() and not visible_ew_output.exists():
        ew_paths = [
            _ew_output_path(_resolve_path(root, chunk["output_path"])) for chunk in manifest["chunks"]
        ]
        messages.append(f"{'would combine' if not apply else 'combined'} ephemerides into {final_ew_output}")
        if apply:
            combine_csv_files(ew_paths, final_ew_output)

    return messages


def consolidate_manifest(root, manifest_path, apply=False):
    """Consolidate one run manifest and expose its combined outputs if possible."""
    root = Path(root)
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    run_dir = manifest_path.parent
    final_output, final_ew_output, visible_output, visible_ew_output = _final_output_paths(
        root, manifest_path, manifest
    )
    messages = []

    complete = _manifest_is_complete(root, manifest)
    audited_combined = final_output.exists() and final_ew_output.exists() and _audit_ok(run_dir)
    if complete:
        messages.extend(
            _combine_complete_manifest(
                root,
                manifest,
                final_output,
                final_ew_output,
                visible_output,
                visible_ew_output,
                apply,
            )
        )
    elif not audited_combined:
        missing = sum(1 for chunk in manifest["chunks"] if not _chunk_is_complete(root, chunk))
        messages.append(f"left incomplete run unconsolidated: {run_dir.name} missing_chunks={missing}")
        return messages

    # Salvaged runs can have failed parent markers but still contain audited
    # combined files assembled from successful parent/debug leaves.
    messages.append(_link_or_copy(final_output, visible_output, apply))
    messages.append(_link_or_copy(final_ew_output, visible_ew_output, apply))
    return messages


def _move_if_needed(src, dst, apply):
    src = Path(src)
    dst = Path(dst)
    if not src.exists():
        return None
    if dst.exists():
        return f"left {src}; destination already exists {dst}"
    if not apply:
        return f"would move {src} to {dst}"

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"moved {src} to {dst}"


def _iter_run_manifests(results_dir):
    results_dir = Path(results_dir)
    direct = sorted(results_dir.glob("*/manifest.json"))
    archived = sorted((results_dir / CHUNK_RUNS_DIRNAME).glob("*/manifest.json"))
    return direct + [path for path in archived if path not in direct]


def consolidate_chunk_artifacts(root=".", apply=False, archive=True):
    """Promote combined outputs and move digest-scoped chunk files aside."""
    root = Path(root).expanduser().resolve()
    results_dir = root / "results"
    work_dir = root / "work"
    messages = []

    for manifest_path in _iter_run_manifests(results_dir):
        messages.extend(consolidate_manifest(root, manifest_path, apply=apply))

    if archive:
        for run_dir in sorted(results_dir.iterdir() if results_dir.exists() else []):
            if run_dir.is_dir() and RUN_DIR_RE.match(run_dir.name):
                moved = _move_if_needed(run_dir, results_dir / CHUNK_RUNS_DIRNAME / run_dir.name, apply)
                if moved:
                    messages.append(moved)
        for run_dir in sorted(work_dir.iterdir() if work_dir.exists() else []):
            if run_dir.is_dir() and RUN_DIR_RE.match(run_dir.name):
                moved = _move_if_needed(run_dir, work_dir / CHUNK_RUNS_DIRNAME / run_dir.name, apply)
                if moved:
                    messages.append(moved)

        log_dir = results_dir / "logs"
        for log_path in sorted(results_dir.glob("*-sorcha.log")) + sorted(results_dir.glob("*-sorcha.err")):
            moved = _move_if_needed(log_path, log_dir / log_path.name, apply)
            if moved:
                messages.append(moved)

    return messages


def main(argv=None):
    parser = argparse.ArgumentParser(description="Consolidate Ponder chunk artifacts")
    parser.add_argument("root", nargs="?", default=".", help="Directory containing Ponder work/ and results/")
    parser.add_argument(
        "--apply", action="store_true", help="Actually write links, combined files, and moves"
    )
    parser.add_argument(
        "--no-archive", action="store_true", help="Leave digest-scoped run directories in place"
    )
    args = parser.parse_args(argv)

    messages = consolidate_chunk_artifacts(args.root, apply=args.apply, archive=not args.no_archive)
    for message in messages:
        print(message)


if __name__ == "__main__":
    main()
