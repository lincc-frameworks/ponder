"""Combine audited asteroid and comet Ponder runs without losing provenance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .runner import OBJECT_MODE_ASTEROID, OBJECT_MODE_COMET, OBJECT_MODES


@dataclass(frozen=True)
class AuditedRun:
    object_mode: str
    audit_path: Path
    detections_path: Path
    ephemeris_path: Path
    completed_mtime_ns: int


def _resolve_audit_path(results_dir: Path, audit_path: Path, value: str) -> Path:
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path
    candidates = (
        results_dir.parent / path,
        audit_path.parent / path.name,
        results_dir / path,
    )
    return next(
        (candidate for candidate in candidates if candidate.exists()), candidates[0]
    )


def discover_audited_runs(
    results_dir: str | Path, object_mode: str
) -> list[AuditedRun]:
    """Return complete ``job_new`` run pairs from one mode-specific result root."""
    if object_mode not in OBJECT_MODES:
        raise ValueError(f"Unknown object mode: {object_mode}")

    root = Path(results_dir).expanduser().resolve()
    runs: list[AuditedRun] = []
    for audit_path in sorted(root.glob("chunk_runs/*_job_new_*/output_audit.csv")):
        try:
            audit = pd.read_csv(audit_path)
        except (OSError, pd.errors.ParserError):
            continue
        if "output_name" not in audit.columns:
            continue
        rows = {str(row.output_name): row for row in audit.itertuples(index=False)}
        if not {"detections", "ephemeris"}.issubset(rows):
            continue

        paths: dict[str, Path] = {}
        valid = True
        for label in ("detections", "ephemeris"):
            row = rows[label]
            status = str(getattr(row, "status", "")).strip().lower()
            source_rows = int(getattr(row, "source_rows", 0) or 0)
            combined_rows = int(getattr(row, "combined_rows", 0) or 0)
            zero_output = (
                status == "skipped_no_key_columns" and source_rows == combined_rows == 0
            )
            if status != "ok" and not zero_output:
                valid = False
                break
            if int(getattr(row, "missing_rows", 0) or 0) != 0:
                valid = False
                break
            output_path = _resolve_audit_path(
                root,
                audit_path,
                getattr(row, "combined_path", ""),
            )
            try:
                pq.ParquetFile(output_path)
            except (FileNotFoundError, OSError, pa.ArrowInvalid):
                valid = False
                break
            paths[label] = output_path.resolve()
        if not valid:
            continue

        manifest_path = audit_path.parent / "manifest.json"
        if manifest_path.exists():
            try:
                manifest_mode = str(
                    json.loads(manifest_path.read_text()).get("object_mode") or ""
                )
            except (OSError, json.JSONDecodeError):
                continue
            if manifest_mode and manifest_mode != object_mode:
                continue

        runs.append(
            AuditedRun(
                object_mode=object_mode,
                audit_path=audit_path.resolve(),
                detections_path=paths["detections"],
                ephemeris_path=paths["ephemeris"],
                completed_mtime_ns=audit_path.stat().st_mtime_ns,
            )
        )
    return sorted(runs, key=lambda run: (run.completed_mtime_ns, str(run.audit_path)))


def latest_audited_run(results_dir: str | Path, object_mode: str) -> AuditedRun:
    runs = discover_audited_runs(results_dir, object_mode)
    if not runs:
        raise FileNotFoundError(
            f"No complete audited {object_mode} runs below {results_dir}"
        )
    return runs[-1]


def _input_schema(path: Path) -> pa.Schema:
    return pq.ParquetFile(path).schema_arrow


def _unified_schema(mode_paths: dict[str, Path]) -> pa.Schema:
    schemas = [
        _input_schema(path).remove_metadata()
        for path in mode_paths.values()
        if len(_input_schema(path)) > 0
    ]
    if schemas:
        schema = pa.unify_schemas(schemas)
        fields = [field for field in schema if field.name != "object_mode"]
    else:
        fields = []
    fields.append(pa.field("object_mode", pa.string(), nullable=False))
    return pa.schema(fields)


def _table_with_mode(table: pa.Table, schema: pa.Schema, object_mode: str) -> pa.Table:
    arrays = []
    for field in schema:
        if field.name == "object_mode":
            arrays.append(pa.array([object_mode] * len(table), type=pa.string()))
        elif field.name in table.column_names:
            column = table[field.name]
            arrays.append(
                column if column.type == field.type else column.cast(field.type)
            )
        else:
            arrays.append(pa.nulls(len(table), type=field.type))
    return pa.Table.from_arrays(arrays, schema=schema)


def combine_mode_parquets(
    mode_paths: dict[str, str | Path],
    output_path: str | Path,
    *,
    batch_size: int = 250_000,
) -> dict[str, int]:
    """Stream mode-labelled input parquets into one union-schema parquet."""
    normalized = {
        mode: Path(path).expanduser().resolve() for mode, path in mode_paths.items()
    }
    unknown = set(normalized) - set(OBJECT_MODES)
    if unknown:
        raise ValueError(f"Unknown object modes: {sorted(unknown)}")
    if not normalized:
        raise ValueError("At least one mode parquet is required")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    schema = _unified_schema(normalized)
    counts: dict[str, int] = {}
    with pq.ParquetWriter(output, schema, compression="snappy") as writer:
        for mode in OBJECT_MODES:
            path = normalized.get(mode)
            if path is None:
                continue
            parquet = pq.ParquetFile(path)
            counts[mode] = parquet.metadata.num_rows
            for batch in parquet.iter_batches(batch_size=batch_size):
                writer.write_table(
                    _table_with_mode(pa.Table.from_batches([batch]), schema, mode)
                )
    return counts


def _fingerprint_run(run: AuditedRun) -> str:
    values = [run.object_mode, str(run.audit_path)]
    for path in (run.detections_path, run.ephemeris_path):
        stat = path.stat()
        values.extend((str(path), str(stat.st_size), str(stat.st_mtime_ns)))
    return "\0".join(values)


def _expose(src: Path, dst: Path) -> str:
    if dst.exists():
        try:
            if src.samefile(dst):
                return "already_visible"
        except FileNotFoundError:
            pass
        return "conflict"
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
        return "linked"
    except OSError:
        shutil.copy2(src, dst)
        return "copied"


def combine_audited_runs(
    asteroid_run: AuditedRun,
    comet_run: AuditedRun,
    output_results_dir: str | Path,
    *,
    run_date: str | None = None,
) -> tuple[Path, Path, Path]:
    """Create an RCC-discoverable audited pair from one run of each mode."""
    if asteroid_run.object_mode != OBJECT_MODE_ASTEROID:
        raise ValueError("asteroid_run is not labelled asteroid")
    if comet_run.object_mode != OBJECT_MODE_COMET:
        raise ValueError("comet_run is not labelled comet")

    output_root = Path(output_results_dir).expanduser().resolve()
    date = run_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    digest = hashlib.sha256(
        f"{_fingerprint_run(asteroid_run)}\0{_fingerprint_run(comet_run)}".encode()
    ).hexdigest()
    run_dir = output_root / "chunk_runs" / f"{date}_job_new_{digest[:12]}"
    detections_path = run_dir / f"{date}_job_new.parquet"
    ephemeris_path = run_dir / f"{date}_job_new_ew.parquet"
    run_dir.mkdir(parents=True, exist_ok=True)

    detection_counts = combine_mode_parquets(
        {
            OBJECT_MODE_ASTEROID: asteroid_run.detections_path,
            OBJECT_MODE_COMET: comet_run.detections_path,
        },
        detections_path,
    )
    ephemeris_counts = combine_mode_parquets(
        {
            OBJECT_MODE_ASTEROID: asteroid_run.ephemeris_path,
            OBJECT_MODE_COMET: comet_run.ephemeris_path,
        },
        ephemeris_path,
    )

    audit_rows = []
    for label, path, counts in (
        ("detections", detections_path, detection_counts),
        ("ephemeris", ephemeris_path, ephemeris_counts),
    ):
        combined_rows = pq.ParquetFile(path).metadata.num_rows
        source_rows = sum(counts.values())
        audit_rows.append(
            {
                "output_name": label,
                "combined_path": str(path),
                "source_file_count": len(counts),
                "id_column": "ObjID" if "ObjID" in _input_schema(path).names else "",
                "timestamp_column": "fieldMJD_TAI"
                if "fieldMJD_TAI" in _input_schema(path).names
                else "",
                "key_columns": "ObjID,fieldMJD_TAI",
                "source_rows": source_rows,
                "combined_rows": combined_rows,
                "source_pairs": source_rows,
                "combined_pairs": combined_rows,
                "missing_pairs": 0,
                "missing_rows": max(0, source_rows - combined_rows),
                "status": "ok" if source_rows == combined_rows else "missing",
                "asteroid_rows": counts.get(OBJECT_MODE_ASTEROID, 0),
                "comet_rows": counts.get(OBJECT_MODE_COMET, 0),
                "object_mode": "asteroid,comet",
            }
        )
    audit_path = run_dir / "output_audit.csv"
    pd.DataFrame(audit_rows).to_csv(audit_path, index=False)

    manifest = {
        "job_name": "new",
        "object_modes": list(OBJECT_MODES),
        "digest": digest,
        "asteroid_audit_path": str(asteroid_run.audit_path),
        "comet_audit_path": str(comet_run.audit_path),
        "detections_path": str(detections_path),
        "ephemeris_path": str(ephemeris_path),
        "detection_rows_by_mode": detection_counts,
        "ephemeris_rows_by_mode": ephemeris_counts,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    visible = output_root / detections_path.name
    visible_ew = output_root / ephemeris_path.name
    statuses = {
        "detections": _expose(detections_path, visible),
        "ephemeris": _expose(ephemeris_path, visible_ew),
    }
    (run_dir / "promotion_status.json").write_text(json.dumps(statuses, indent=2))
    return detections_path, ephemeris_path, audit_path


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Combine latest audited asteroid and comet Ponder runs"
    )
    parser.add_argument("--asteroid-results-dir", type=Path, required=True)
    parser.add_argument("--comet-results-dir", type=Path, required=True)
    parser.add_argument("--output-results-dir", type=Path, required=True)
    parser.add_argument(
        "--run-date", help="Output date prefix (default: current UTC date)"
    )
    args = parser.parse_args(argv)

    asteroid_run = latest_audited_run(args.asteroid_results_dir, OBJECT_MODE_ASTEROID)
    comet_run = latest_audited_run(args.comet_results_dir, OBJECT_MODE_COMET)
    detections, ephemeris, audit = combine_audited_runs(
        asteroid_run,
        comet_run,
        args.output_results_dir,
        run_date=args.run_date,
    )
    print(f"Combined detections: {detections}")
    print(f"Combined ephemeris: {ephemeris}")
    print(f"Audit: {audit}")


if __name__ == "__main__":
    main()
