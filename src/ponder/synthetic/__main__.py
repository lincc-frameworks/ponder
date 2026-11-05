"""Reproducible catalog preparation and bounded injection-truth generation."""

import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path

import numpy as np
import pandas as pd
from astropy.time import Time

from .model import physical_parameters, sample_model, sha256, to_bcart
from .truth import geometry_candidates, propagate_truth


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n")


def write_bundle(output, orbits, physical, draws, metadata):
    orbits.to_csv(output / "orbits.csv", index=False)
    physical.to_csv(output / "physical.csv", index=False)
    draws.to_csv(output / "model_draws.csv", index=False)
    metadata["files"] = {
        name: sha256(output / name) for name in ["orbits.csv", "physical.csv", "model_draws.csv"]
    }
    write_json(output / "bundle.json", metadata)


def read_bundle(path):
    path = Path(path)
    metadata = json.loads((path / "bundle.json").read_text())
    for name in ["orbits.csv", "physical.csv", "model_draws.csv"]:
        if sha256(path / name) != metadata["files"][name]:
            raise ValueError(f"Bundle checksum mismatch: {name}")
    frames = [
        pd.read_csv(path / name, dtype={"ObjID": str, "source_id": str})
        for name in ["orbits.csv", "physical.csv", "model_draws.csv"]
    ]
    return (*frames, metadata)


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="Sample the external OO model and write Sorcha BCART inputs")
    prepare.add_argument("--model-repo", type=Path, required=True)
    prepare.add_argument("--count", type=int, required=True)
    prepare.add_argument("--radius-au", type=float, required=True)
    prepare.add_argument("--start-utc", required=True)
    prepare.add_argument("--end-utc", required=True)
    prepare.add_argument("--seed", type=int, required=True)
    prepare.add_argument(
        "--h-r", type=float, required=True, help="Explicit fixed-H scenario, not a luminosity prior"
    )
    prepare.add_argument("--gs", type=float, default=0.15)
    prepare.add_argument("--weight-column", choices=["ISO_weights", "oneOverESF"], default="ISO_weights")
    prepare.add_argument("--epoch-mjd-tdb", type=float, help="Defaults to survey start converted to TDB")
    prepare.add_argument("--output", type=Path, required=True)
    select = sub.add_parser("select", help="Create a provenance-linked subset for a bounded experiment")
    select.add_argument("--bundle", type=Path, required=True)
    select.add_argument("--ids", type=Path, required=True, help="CSV with unique ObjID column")
    select.add_argument("--reason", required=True, help="Explain selection; no implicit completeness claim")
    select.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("propagate", help="Compute every supplied object/visit pair before detection cuts")
    run.add_argument("--bundle", type=Path, required=True)
    run.add_argument("--pointings", type=Path, required=True, help="CSV with exact IDs and exp_midpt_mjd TAI")
    run.add_argument("--planets", type=Path, required=True)
    run.add_argument("--asteroids", type=Path)
    run.add_argument("--allow-planetary-only", action="store_true")
    run.add_argument("--field-radius-deg", type=float, default=1.75)
    run.add_argument("--max-pairs", type=int, default=1_000_000)
    run.add_argument("--output", type=Path, required=True)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=False)
    receipt = {
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "command": args.command,
        "arguments": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        "code_sha256": {f.name: sha256(f) for f in Path(__file__).parent.glob("*.py")},
        "versions": {name: version(name) for name in ["sorcha", "assist", "rebound", "astropy", "numpy"]},
        "status": "running",
    }
    write_json(args.output / "run.json", receipt)
    try:
        if args.command == "prepare":
            start, end = Time(args.start_utc, scale="utc").tdb.mjd, Time(args.end_utc, scale="utc").tdb.mjd
            epoch = start if args.epoch_mjd_tdb is None else args.epoch_mjd_tdb
            draws, metadata = sample_model(
                args.model_repo, args.count, args.radius_au, start, end, args.seed, args.weight_column
            )
            orbits = to_bcart(draws, epoch, metadata["model_mu_au3_day2"])
            physical = physical_parameters(orbits.ObjID, args.h_r, args.gs)
            metadata.update(
                {
                    "epoch_mjd_tdb": float(epoch),
                    "photometry": {
                        "H_r": args.h_r,
                        "GS": args.gs,
                        "scenario": "fixed-H inactive nucleus; not a sampled luminosity distribution",
                    },
                    "frames": "OO Astropy mean-ecliptic J2000 -> ICRS -> Sorcha ecliptic J2000",
                    "population_draw_count": len(draws),
                }
            )
            write_bundle(args.output, orbits, physical, draws, metadata)
            receipt["objects"] = len(orbits)
        elif args.command == "select":
            orbits, physical, draws, metadata = read_bundle(args.bundle)
            ids = pd.read_csv(args.ids, dtype={"ObjID": str}).ObjID
            if (
                len(ids) == 0
                or ids.isna().any()
                or ids.duplicated().any()
                or not set(ids) <= set(orbits.ObjID)
            ):
                raise ValueError("selection IDs must be nonempty, unique, and present in parent bundle")
            metadata = dict(metadata)
            metadata.update(
                {
                    "mode": "explicit_subset",
                    "parent_bundle_sha256": sha256(args.bundle / "bundle.json"),
                    "parent_bundle_path": str(args.bundle.resolve()),
                    "selection_reason": args.reason,
                    "selection_ids_sha256": sha256(args.ids),
                    "selected_objects": len(ids),
                    "selection_completeness": "not established; no field-yield claim",
                }
            )
            frames = [f.set_index("ObjID").loc[ids].reset_index() for f in [orbits, physical, draws]]
            write_bundle(args.output, *frames, metadata)
            receipt["objects"] = len(ids)
        else:
            orbits, physical, _, metadata = read_bundle(args.bundle)
            pointings = pd.read_csv(args.pointings, dtype={"observationId": str})
            truth, summary = propagate_truth(
                orbits,
                physical,
                pointings,
                args.planets,
                args.asteroids,
                args.allow_planetary_only,
                args.field_radius_deg,
                args.max_pairs,
            )
            truth.to_parquet(args.output / "truth.parquet", index=False)
            candidates = geometry_candidates(truth)
            candidates.to_parquet(args.output / "injection_candidates.parquet", index=False)
            summary.update(
                {
                    "bundle_sha256": sha256(args.bundle / "bundle.json"),
                    "bundle_mode": metadata["mode"],
                    "input_sha256": {
                        str(p.resolve()): sha256(p)
                        for p in [args.pointings, args.planets] + ([args.asteroids] if args.asteroids else [])
                    },
                    "files": {
                        n: sha256(args.output / n) for n in ["truth.parquet", "injection_candidates.parquet"]
                    },
                }
            )
            write_json(args.output / "summary.json", summary)
            receipt["summary"] = summary
        receipt["status"] = "complete"
    except BaseException as exc:
        receipt.update({"status": "failed", "error_type": type(exc).__name__, "error": str(exc)})
        raise
    finally:
        receipt["finished_utc"] = datetime.now(timezone.utc).isoformat()
        write_json(args.output / "run.json", receipt)
    print(json.dumps({"status": receipt["status"], "output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
