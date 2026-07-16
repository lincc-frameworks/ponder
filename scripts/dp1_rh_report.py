#!/usr/bin/env python
"""Summarize DP1 Ponder ephemerides by heliocentric distance."""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from ponder.utils import SORCHA_DEFAULT_COLOR_OFFSETS, format_rubin_filter_list, rubin_band

AU_KM = 149597870.700
DETECTION_COLUMNS = [
    "ObjID",
    "fieldMJD_TAI",
    "astrometricSigma_deg",
    "optFilter",
    "trailedSourceMag",
]


def read_ephemeris(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path)


def infer_detections_path(ephemeris_path: Path) -> Path:
    stem = ephemeris_path.name
    if stem.endswith("_ew.parquet"):
        return ephemeris_path.with_name(stem.removesuffix("_ew.parquet") + ".parquet")
    if stem.endswith("_ew.csv"):
        return ephemeris_path.with_name(stem.removesuffix("_ew.csv") + ".csv")
    return ephemeris_path.with_name(ephemeris_path.stem.removesuffix("_ew") + ephemeris_path.suffix)


def read_detections(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=DETECTION_COLUMNS)
    if path.suffix == ".parquet":
        return pd.read_parquet(path, columns=DETECTION_COLUMNS)
    return pd.read_csv(path, usecols=lambda column: column in DETECTION_COLUMNS)


def read_orbits(path: Path) -> pd.DataFrame:
    open_func = gzip.open if path.suffix == ".gz" else open
    with open_func(path, "rt") as handle:
        objects = json.load(handle)
    df = pd.DataFrame(objects)
    return df[["Principal_desig", "H"]].rename(columns={"Principal_desig": "ObjID", "H": "H_r"})


def read_pointing_metadata(db_path: Path) -> pd.DataFrame:
    with sqlite3.connect(db_path) as con:
        out = pd.read_sql_query("SELECT observationId AS FieldID, band FROM observations", con)
    out["local_obsnight"] = (out["FieldID"].astype("int64") // 100000).astype("int64")
    return out


def add_distances(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "Obj_Sun_LTC_km" not in out:
        out["Obj_Sun_LTC_km"] = np.sqrt(
            out["Obj_Sun_x_LTC_km"] ** 2
            + out["Obj_Sun_y_LTC_km"] ** 2
            + out["Obj_Sun_z_LTC_km"] ** 2
        )
    out["rH_au"] = out["Obj_Sun_LTC_km"] / AU_KM
    out["delta_au"] = out["Range_LTC_km"] / AU_KM
    return out


def add_observation_metadata(ephem: pd.DataFrame, db_path: Path, orbits_path: Path) -> pd.DataFrame:
    out = ephem.merge(read_pointing_metadata(db_path), on="FieldID", how="left")
    out = out.merge(read_orbits(orbits_path), on="ObjID", how="left")
    offsets = {"r-r": 0.0, **SORCHA_DEFAULT_COLOR_OFFSETS}
    out["filter_band"] = out["band"].map(rubin_band)
    out["color_offset"] = out["filter_band"].map(lambda band: offsets.get(f"{band}-r", np.nan))
    out["H_filter"] = out["H_r"] + out["color_offset"]
    out["apparent_mag"] = out["H_filter"] + 5.0 * np.log10(out["rH_au"]) + 5.0 * np.log10(
        out["delta_au"]
    )
    return out


def add_detection_metadata(ephem: pd.DataFrame, detections_path: Path) -> pd.DataFrame:
    detections = read_detections(detections_path)
    if detections.empty:
        out = ephem.copy()
        out["astrometricSigma_deg"] = np.nan
        out["detection_filter_band"] = pd.NA
        out["trailedSourceMag"] = np.nan
        out["positional_uncertainty_arcsec"] = np.nan
        return out

    detections = detections.copy()
    detections["detection_filter_band"] = detections["optFilter"].map(rubin_band)
    detections = detections.drop(columns=["optFilter"])
    detections = detections.drop_duplicates(["ObjID", "fieldMJD_TAI"])
    out = ephem.merge(detections, on=["ObjID", "fieldMJD_TAI"], how="left")
    out["positional_uncertainty_arcsec"] = out["astrometricSigma_deg"] * 3600.0
    return out


def summarize(df: pd.DataFrame, threshold: float, inclusive: bool) -> pd.DataFrame:
    mask = df["rH_au"] >= threshold if inclusive else df["rH_au"] > threshold
    qualifying = df.loc[mask].copy()
    if qualifying.empty:
        return pd.DataFrame(
            columns=[
                "ObjID",
                "possible_image_count",
                "mean_rH_au",
                "mean_apparent_mag",
                "mean_positional_uncertainty_arcsec",
                "first_local_obsnight",
                "last_local_obsnight",
                "min_rH_au",
                "max_rH_au",
                "filter_bands",
                "physical_filters",
            ]
        )

    summary = (
        qualifying.groupby("ObjID", dropna=False)
        .agg(
            possible_image_count=("FieldID", "count"),
            mean_rH_au=("rH_au", "mean"),
            mean_apparent_mag=("apparent_mag", "mean"),
            mean_positional_uncertainty_arcsec=("positional_uncertainty_arcsec", "mean"),
            first_local_obsnight=("local_obsnight", "min"),
            last_local_obsnight=("local_obsnight", "max"),
            min_rH_au=("rH_au", "min"),
            max_rH_au=("rH_au", "max"),
            filter_bands=(
                "filter_band",
                lambda values: format_rubin_filter_list(values),
            ),
            physical_filters=("band", lambda values: format_rubin_filter_list(values, normalize=False)),
        )
        .reset_index()
        .sort_values(["possible_image_count", "mean_rH_au", "ObjID"], ascending=[False, False, True])
    )
    return summary


def format_markdown_table(df: pd.DataFrame) -> str:
    out = df.copy()
    for col in [
        "mean_rH_au",
        "mean_apparent_mag",
        "mean_positional_uncertainty_arcsec",
        "min_rH_au",
        "max_rH_au",
    ]:
        out[col] = out[col].round(3)
    out = out.astype(object).where(pd.notna(out), "")
    return out.to_markdown(index=False)


def write_markdown(
    path: Path,
    *,
    ephemeris_path: Path,
    detections_path: Path,
    db_path: Path,
    orbits_path: Path,
    gt5: pd.DataFrame,
    ge5: pd.DataFrame,
) -> None:
    lines = [
        "# DP1 rH > 5 au Ponder Summary",
        "",
        f"- Ephemeris source: `{ephemeris_path}`",
        f"- Detection source: `{detections_path}`",
        f"- Pointing DB: `{db_path}`",
        f"- Orbit catalog: `{orbits_path}`",
        "- Apparent magnitude is derived with Sorcha's `phase_function = none` formula and Ponder's fixed color offsets.",
        "- Positional uncertainty is the mean matched Ponder `astrometricSigma_deg` value converted to arcseconds.",
        f"- Matched positional uncertainty is available for {gt5['mean_positional_uncertainty_arcsec'].notna().sum():,} of {len(gt5):,} `rH > 5 au` objects.",
        f"- Unique objects with at least one possible image at `rH > 5 au`: {len(gt5):,}",
        f"- Unique objects with at least one possible image at `rH >= 5 au`: {len(ge5):,}",
        "",
        "## rH > 5 au Objects By Possible Images",
        "",
    ]
    if gt5.empty:
        lines.append("No qualifying objects.")
    else:
        lines.append(format_markdown_table(gt5))
    lines.extend(
        [
            "",
            "## Reduced rH >= 5 au Object List",
            "",
        ]
    )
    if ge5.empty:
        lines.append("No qualifying objects.")
    else:
        lines.append(format_markdown_table(ge5))
    lines.append("")
    path.write_text("\n".join(lines))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ephemeris", required=True, type=Path)
    parser.add_argument(
        "--detections",
        type=Path,
        help="Combined Ponder detections parquet/CSV. Defaults to the ephemeris path without _ew.",
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--orbits", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--threshold", type=float, default=5.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    detections_path = args.detections or infer_detections_path(args.ephemeris)
    ephem = add_distances(read_ephemeris(args.ephemeris))
    ephem = add_observation_metadata(ephem, args.db, args.orbits)
    ephem = add_detection_metadata(ephem, detections_path)

    gt5 = summarize(ephem, args.threshold, inclusive=False)
    ge5 = summarize(ephem, args.threshold, inclusive=True)

    gt5_path = args.out_dir / "dp1_rh_gt5_by_object.csv"
    ge5_path = args.out_dir / "dp1_rh_ge5_reduced_by_object.csv"
    report_path = args.out_dir / "dp1_rh_report.md"
    gt5.to_csv(gt5_path, index=False)
    ge5.to_csv(ge5_path, index=False)
    write_markdown(
        report_path,
        ephemeris_path=args.ephemeris,
        detections_path=detections_path,
        db_path=args.db,
        orbits_path=args.orbits,
        gt5=gt5,
        ge5=ge5,
    )

    print(f"Wrote {gt5_path} ({len(gt5):,} objects)")
    print(f"Wrote {ge5_path} ({len(ge5):,} objects)")
    print(f"Wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
