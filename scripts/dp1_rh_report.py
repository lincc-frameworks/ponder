#!/usr/bin/env python
"""Summarize DP1 Ponder ephemerides by heliocentric distance."""

from __future__ import annotations

import argparse
import configparser
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
        # A successful zero-detection Ponder run produces a valid Parquet file
        # with no columns. Reindexing a full read handles that case while still
        # selecting the report columns from normal outputs.
        return pd.read_parquet(path).reindex(columns=DETECTION_COLUMNS)
    return pd.read_csv(path, usecols=lambda column: column in DETECTION_COLUMNS)


def read_orbits(path: Path, object_mode: str) -> pd.DataFrame:
    open_func = gzip.open if path.suffix == ".gz" else open
    with open_func(path, "rt") as handle:
        objects = json.load(handle)
    df = pd.DataFrame(objects)
    id_column = "Principal_desig" if object_mode == "asteroid" else "Designation_and_name"
    missing = [column for column in [id_column, "H"] if column not in df]
    if missing:
        raise KeyError(f"Orbit catalog is missing columns required for {object_mode} mode: {missing}")
    return (
        df[[id_column, "H"]]
        .rename(columns={id_column: "ObjID", "H": "H_r"})
        .dropna(subset=["ObjID"])
        .drop_duplicates("ObjID")
    )


def read_pointing_metadata(db_path: Path) -> pd.DataFrame:
    with sqlite3.connect(db_path) as con:
        out = pd.read_sql_query(
            "SELECT observationId AS FieldID, band, fieldRA, fieldDec FROM observations", con
        )
    out["local_obsnight"] = (out["FieldID"].astype("int64") // 100000).astype("int64")
    return out


def read_circle_radius(config_path: Path) -> float:
    parser = configparser.ConfigParser()
    if not parser.read(config_path):
        raise FileNotFoundError(f"Could not read Sorcha config: {config_path}")
    camera_model = parser.get("FOV", "camera_model", fallback="").strip().lower()
    if camera_model != "circle":
        raise ValueError(f"DP1 report requires camera_model=circle; found {camera_model!r}")
    radius = parser.getfloat("FOV", "circle_radius")
    if radius <= 0.0:
        raise ValueError(f"circle_radius must be positive; found {radius}")
    return radius


def filter_to_circle(ephem: pd.DataFrame, radius_deg: float) -> pd.DataFrame:
    """Keep ephemerides strictly inside the configured pointing-centered cone."""
    required = ["RA_deg", "Dec_deg", "fieldRA", "fieldDec"]
    missing = [column for column in required if column not in ephem]
    if missing:
        raise KeyError(f"Cannot apply DP1 FOV filter; missing columns: {missing}")

    object_ra = np.radians(pd.to_numeric(ephem["RA_deg"], errors="coerce"))
    object_dec = np.radians(pd.to_numeric(ephem["Dec_deg"], errors="coerce"))
    field_ra = np.radians(pd.to_numeric(ephem["fieldRA"], errors="coerce"))
    field_dec = np.radians(pd.to_numeric(ephem["fieldDec"], errors="coerce"))
    cosine = (
        np.sin(object_dec) * np.sin(field_dec)
        + np.cos(object_dec) * np.cos(field_dec) * np.cos(object_ra - field_ra)
    )
    out = ephem.copy()
    out["fov_separation_deg"] = np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0)))
    return out.loc[out["fov_separation_deg"] < radius_deg].reset_index(drop=True)


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


def add_observation_metadata(
    ephem: pd.DataFrame, db_path: Path, orbits_path: Path, object_mode: str
) -> pd.DataFrame:
    out = ephem.merge(read_pointing_metadata(db_path), on="FieldID", how="left")
    out = out.merge(read_orbits(orbits_path, object_mode), on="ObjID", how="left")
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


SUMMARY_COLUMNS = [
    "ObjID",
    "object_mode",
    "fov_radius_deg",
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


def summarize_rows(df: pd.DataFrame) -> pd.DataFrame:
    qualifying = df.copy()
    if qualifying.empty:
        return pd.DataFrame(columns=SUMMARY_COLUMNS)

    summary = (
        qualifying.groupby("ObjID", dropna=False)
        .agg(
            object_mode=("object_mode", "first"),
            fov_radius_deg=("fov_radius_deg", "first"),
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
    return summary[SUMMARY_COLUMNS]


def summarize(df: pd.DataFrame, threshold: float, inclusive: bool) -> pd.DataFrame:
    mask = df["rH_au"] >= threshold if inclusive else df["rH_au"] > threshold
    return summarize_rows(df.loc[mask])


def summarize_below(df: pd.DataFrame, threshold: float) -> pd.DataFrame:
    return summarize_rows(df.loc[df["rH_au"] < threshold])


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
    config_path: Path,
    object_mode: str,
    fov_radius_deg: float,
    pre_fov_rows: int,
    post_fov_rows: int,
    all_objects: pd.DataFrame,
    lt5: pd.DataFrame,
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
        f"- Sorcha config: `{config_path}`",
        f"- Object mode: `{object_mode}`",
        f"- Circular FOV radius: `{fov_radius_deg:.6f} deg` (strict `<` cut)",
        f"- Buffered ephemeris rows before FOV cut: {pre_fov_rows:,}",
        f"- In-FOV ephemeris rows after FOV cut: {post_fov_rows:,}",
        "- Apparent magnitude is derived with Sorcha's `phase_function = none` formula and Ponder's fixed color offsets.",
        "- Positional uncertainty is the mean matched Ponder `astrometricSigma_deg` value converted to arcseconds.",
        f"- Matched positional uncertainty is available for {gt5['mean_positional_uncertainty_arcsec'].notna().sum():,} of {len(gt5):,} `rH > 5 au` objects.",
        f"- Unique objects before the `5 au` cutoff: {len(all_objects):,}",
        f"- Unique objects with at least one possible image at `rH < 5 au`: {len(lt5):,}",
        f"- Unique objects with at least one possible image at `rH > 5 au`: {len(gt5):,}",
        f"- Unique objects with at least one possible image at `rH >= 5 au`: {len(ge5):,}",
        "",
        "## CSV Outputs",
        "",
        "- `dp1_rh_all_by_object.csv`: all objects before applying the `5 au` cutoff.",
        "- `dp1_rh_lt5_by_object.csv`: objects with at least one possible image below `5 au`.",
        "- `dp1_rh_gt5_by_object.csv`: objects with at least one possible image above `5 au`.",
        "- `dp1_rh_ge5_reduced_by_object.csv`: reduced list using the inclusive `5 au` cutoff.",
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
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--object-mode", choices=["asteroid", "comet"], default="asteroid")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--threshold", type=float, default=5.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    detections_path = args.detections or infer_detections_path(args.ephemeris)
    fov_radius_deg = read_circle_radius(args.config)
    ephem = add_distances(read_ephemeris(args.ephemeris))
    ephem = add_observation_metadata(ephem, args.db, args.orbits, args.object_mode)
    pre_fov_rows = len(ephem)
    ephem = filter_to_circle(ephem, fov_radius_deg)
    ephem["object_mode"] = args.object_mode
    ephem["fov_radius_deg"] = fov_radius_deg
    ephem = add_detection_metadata(ephem, detections_path)

    all_objects = summarize_rows(ephem)
    lt5 = summarize_below(ephem, args.threshold)
    gt5 = summarize(ephem, args.threshold, inclusive=False)
    ge5 = summarize(ephem, args.threshold, inclusive=True)

    all_path = args.out_dir / "dp1_rh_all_by_object.csv"
    lt5_path = args.out_dir / "dp1_rh_lt5_by_object.csv"
    gt5_path = args.out_dir / "dp1_rh_gt5_by_object.csv"
    ge5_path = args.out_dir / "dp1_rh_ge5_reduced_by_object.csv"
    report_path = args.out_dir / "dp1_rh_report.md"
    all_objects.to_csv(all_path, index=False)
    lt5.to_csv(lt5_path, index=False)
    gt5.to_csv(gt5_path, index=False)
    ge5.to_csv(ge5_path, index=False)
    write_markdown(
        report_path,
        ephemeris_path=args.ephemeris,
        detections_path=detections_path,
        db_path=args.db,
        orbits_path=args.orbits,
        config_path=args.config,
        object_mode=args.object_mode,
        fov_radius_deg=fov_radius_deg,
        pre_fov_rows=pre_fov_rows,
        post_fov_rows=len(ephem),
        all_objects=all_objects,
        lt5=lt5,
        gt5=gt5,
        ge5=ge5,
    )

    print(f"Wrote {all_path} ({len(all_objects):,} objects)")
    print(f"Wrote {lt5_path} ({len(lt5):,} objects)")
    print(f"Wrote {gt5_path} ({len(gt5):,} objects)")
    print(f"Wrote {ge5_path} ({len(ge5):,} objects)")
    print(f"Wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
