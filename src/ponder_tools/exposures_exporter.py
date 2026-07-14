"""Export Rubin pointing records into Ponder/Sorcha input products.

The Butler-facing parts are intentionally kept in ``ponder_tools`` rather than
the core ``ponder`` package. Butler is imported lazily so lightweight uses, such
as converting an existing Sorcha CSV into a SQLite pointing database, work in a
normal Python environment.
"""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
from datetime import timezone
from itertools import repeat
from pathlib import Path
from typing import Any

import astropy.units as u
import pandas as pd
from astropy.coordinates import SkyCoord
from tqdm.auto import tqdm
from tqdm.contrib.concurrent import process_map

OBSERVATIONS_TABLE = "observations"
OBSERVATION_COLUMNS = [
    "observationId",
    "observationStartMJD",
    "visitTime",
    "visitExposureTime",
    "band",
    "seeingFwhmGeom",
    "seeingFwhmEff",
    "fiveSigmaDepth",
    "fieldRA",
    "fieldDec",
    "rotSkyPos",
]


def _exp_to_dict(exp) -> dict:
    if hasattr(exp, "toDict"):
        return exp.toDict()
    if hasattr(exp, "to_dict"):
        return exp.to_dict()
    raise AttributeError("Exposure record has neither toDict() nor to_dict().")


def _timespan_to_utc_ns(timespan):
    if timespan is None:
        return None, None, None, None

    b_utc = timespan.begin.utc
    e_utc = timespan.end.utc
    b_dt = b_utc.to_datetime(timezone=timezone.utc)
    e_dt = e_utc.to_datetime(timezone=timezone.utc)
    b_ns = int(round(b_utc.unix * 1e9))
    e_ns = int(round(e_utc.unix * 1e9))
    return b_dt, e_dt, b_ns, e_ns


def _make_butler(repo, collections=None):
    from lsst.daf.butler import Butler

    return Butler(repo, collections=collections)


def _count_exposures(butler: Any, where: str) -> int:
    return butler.registry.queryDataIds("exposure", where=where).count()


def _write_csv(df: pd.DataFrame, path: str | Path) -> None:
    csv_path = Path(path).expanduser()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path, index=False)


def _write_sqlite(df: pd.DataFrame, path: str | Path, table_name: str = OBSERVATIONS_TABLE) -> Path:
    sqlite_path = Path(path).expanduser()
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(sqlite_path) as con:
        df.to_sql(table_name, con, index=False, if_exists="replace")
    return sqlite_path


def _first_available(df: pd.DataFrame, candidates: list[str]) -> pd.Series | None:
    for column in candidates:
        if column in df.columns:
            return df[column]
    return None


def _required_source(df: pd.DataFrame, candidates: list[str], output_column: str) -> pd.Series:
    series = _first_available(df, candidates)
    if series is None:
        raise KeyError(f"Cannot build {output_column}; missing source columns {candidates}")
    return series


def _optional_source(df: pd.DataFrame, candidates: list[str], default) -> pd.Series:
    series = _first_available(df, candidates)
    if series is not None:
        return series
    return pd.Series([default] * len(df), index=df.index)


def observations_dataframe_from_sorcha(df: pd.DataFrame) -> pd.DataFrame:
    """Convert a Sorcha-formatted pointing dataframe into Ponder's SQLite schema."""
    observation_start = _required_source(
        df, ["observationStartMJD", "observationStartMJD_TAI"], "observationStartMJD"
    )
    visit_exposure_time = _optional_source(df, ["visitExposureTime", "visitTime"], 30.0)
    visit_time = _optional_source(df, ["visitTime"], None)
    if visit_time.isna().all():
        visit_time = visit_exposure_time

    out = pd.DataFrame(
        {
            "observationId": _required_source(df, ["observationId", "FieldID"], "observationId"),
            "observationStartMJD": observation_start,
            "visitTime": visit_time,
            "visitExposureTime": visit_exposure_time,
            "band": _optional_source(df, ["band", "filter", "optFilter"], ""),
            "seeingFwhmGeom": _optional_source(df, ["seeingFwhmGeom", "seeingFwhmGeom_arcsec"], 1.0),
            "seeingFwhmEff": _optional_source(df, ["seeingFwhmEff", "seeingFwhmEff_arcsec"], 1.0),
            "fiveSigmaDepth": _optional_source(
                df, ["fiveSigmaDepth", "fieldFiveSigmaDepth_mag"], 25.0
            ),
            "fieldRA": _optional_source(df, ["fieldRA", "fieldRA_deg"], None),
            "fieldDec": _optional_source(df, ["fieldDec", "fieldDec_deg"], None),
            "rotSkyPos": _optional_source(df, ["rotSkyPos", "fieldRotSkyPos_deg"], None),
        }
    )
    return out[OBSERVATION_COLUMNS]


def write_observations_sqlite_from_sorcha_csv(
    sorcha_csv_path: str | Path, sqlite_path: str | Path
) -> Path:
    """Create a SQLite observations table from an existing Sorcha pointing CSV."""
    df = pd.read_csv(sorcha_csv_path)
    observations = observations_dataframe_from_sorcha(df)
    return _write_sqlite(observations, sqlite_path)


def _band_from_physical_filter(physical_filter: str | None) -> str:
    if physical_filter is None:
        return ""
    pf = str(physical_filter).strip().lower()
    if pf in {"u", "g", "r", "i", "z", "y"}:
        return pf
    match = re.search(r"\b([ugrizy])\b", pf)
    if match:
        return match.group(1)
    if pf and pf[0] in "ugrizy":
        return pf[0]
    return ""


def _looks_like_radians_scalar(x: float | None) -> bool:
    if x is None:
        return False
    try:
        xf = float(x)
    except Exception:
        return False
    return abs(xf) <= 2.0 * math.pi + 0.2


def _angle_to_deg(x: float | None) -> float | None:
    if x is None:
        return None
    try:
        xf = float(x)
    except Exception:
        return None
    if _looks_like_radians_scalar(xf):
        return math.degrees(xf)
    return xf


def _looks_like_radians(ra: float, dec: float) -> bool:
    if ra is None or dec is None:
        return False
    try:
        ra = float(ra)
        dec = float(dec)
    except Exception:
        return False
    return (0.0 <= ra <= 2.0 * math.pi + 0.2) and (-math.pi / 2 - 0.2 <= dec <= math.pi / 2 + 0.2)


def _ra_dec_to_deg(ra: float, dec: float) -> tuple[float, float]:
    ra_f = float(ra)
    dec_f = float(dec)
    if _looks_like_radians(ra_f, dec_f):
        return math.degrees(ra_f) % 360.0, math.degrees(dec_f)
    return ra_f % 360.0, dec_f


def _passes_cone(d: dict, cone_center: SkyCoord, cone_radius_deg: float) -> bool:
    ra = d.get("tracking_ra")
    dec = d.get("tracking_dec")
    if ra is None or dec is None:
        return False
    try:
        ra_deg, dec_deg = _ra_dec_to_deg(ra, dec)
    except Exception:
        return False
    coord = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs")
    return coord.separation(cone_center).deg <= float(cone_radius_deg)


def make_sorcha_dataframe(df_full: pd.DataFrame) -> pd.DataFrame:
    """Create a Sorcha-formatted observations dataframe from Butler exposure records."""
    df = df_full.copy()
    if "timespan_begin_tai_jd" in df.columns:
        mjd_tai = df["timespan_begin_tai_jd"].astype(float) - 2400000.5
    else:
        mjd_tai = pd.to_datetime(df["timespan_begin_utc"], utc=True).astype("int64") / 1e9
        mjd_tai = mjd_tai / 86400.0 + 40587.0

    band = (
        df.get("physical_filter").map(_band_from_physical_filter)
        if "physical_filter" in df.columns
        else ""
    )
    rot_deg = df.get("sky_angle").map(_angle_to_deg) if "sky_angle" in df.columns else None

    return pd.DataFrame(
        {
            "observationId": df.get("exposure_id"),
            "observationStartMJD_TAI": mjd_tai,
            "visitExposureTime": df.get("exposure_time"),
            "filter": band,
            "seeingFwhmGeom_arcsec": 1.0,
            "seeingFwhmEff_arcsec": 1.0,
            "fieldFiveSigmaDepth_mag": 25.0,
            "fieldRA_deg": df.get("tracking_ra"),
            "fieldDec_deg": df.get("tracking_dec"),
            "fieldRotSkyPos_deg": rot_deg,
        }
    )


def make_observations_dataframe(df_full: pd.DataFrame) -> pd.DataFrame:
    """Create the SQLite observations dataframe expected by the Ponder Sorcha config."""
    return observations_dataframe_from_sorcha(make_sorcha_dataframe(df_full))


def _row_from_exposure_record(d: dict) -> dict | None:
    b_dt, e_dt, b_ns, e_ns = _timespan_to_utc_ns(d.get("timespan"))
    if b_ns is None:
        return None

    mid_tai_isot = None
    mid_utc_dt = None
    mid_tai_jd = None
    mid_utc_ns = None
    try:
        ts = d["timespan"]
        begin_tai = ts.begin.tai
        end_tai = ts.end.tai
        mid_tai = begin_tai + (end_tai - begin_tai) / 2
        mid_utc = mid_tai.utc
        mid_utc_dt = mid_utc.to_datetime(timezone=timezone.utc)
        mid_tai_isot = mid_tai.tai.isot
        mid_tai_jd = float(mid_tai.tai.jd)
        mid_utc_ns = int(round(mid_utc.unix * 1e9))
    except Exception:
        pass

    return dict(
        repo=d.get("repo"),
        instrument=d.get("instrument"),
        exposure_id=d.get("id"),
        day_obs=d.get("day_obs"),
        group=d.get("group"),
        obs_id=d.get("obs_id"),
        physical_filter=d.get("physical_filter"),
        exposure_time=d.get("exposure_time"),
        dark_time=d.get("dark_time"),
        observation_type=d.get("observation_type"),
        observation_reason=d.get("observation_reason"),
        seq_num=d.get("seq_num"),
        seq_start=d.get("seq_start"),
        seq_end=d.get("seq_end"),
        target_name=d.get("target_name"),
        science_program=d.get("science_program"),
        tracking_ra=d.get("tracking_ra"),
        tracking_dec=d.get("tracking_dec"),
        sky_angle=d.get("sky_angle"),
        azimuth=d.get("azimuth"),
        zenith_angle=d.get("zenith_angle"),
        has_simulated=d.get("has_simulated"),
        can_see_sky=d.get("can_see_sky"),
        timespan_begin_utc=b_dt,
        timespan_end_utc=e_dt,
        midpoint_tai=mid_tai_isot,
        midpoint_utc=mid_utc_dt,
        timespan_begin_utc_ns=b_ns,
        timespan_end_utc_ns=e_ns,
        midpoint_tai_jd=mid_tai_jd,
        midpoint_utc_ns=mid_utc_ns,
        timespan_begin_tai_jd=float(d["timespan"].begin.tai.jd),
        timespan_end_tai_jd=float(d["timespan"].end.tai.jd),
    )


def _scan_repo_for_pointings(
    repo: str,
    collections,
    where: str,
    prefer_longest_exptime: bool,
    cone_ra,
    cone_dec,
    cone_radius_deg,
) -> dict[int, dict]:
    butler = _make_butler(repo, collections=collections)
    records = butler.registry.queryDimensionRecords("exposure", where=where)

    cone_center = None
    if cone_radius_deg is not None or cone_ra is not None or cone_dec is not None:
        if cone_ra is None or cone_dec is None or cone_radius_deg is None:
            raise ValueError("Cone search requires --cone-ra, --cone-dec, and --cone-radius-deg.")
        cone_center = SkyCoord(ra=float(cone_ra) * u.deg, dec=float(cone_dec) * u.deg, frame="icrs")

    best: dict[int, dict] = {}
    for exp in records:
        d = _exp_to_dict(exp)
        d["repo"] = str(repo)
        if cone_center is not None and not _passes_cone(d, cone_center, float(cone_radius_deg)):
            continue

        row = _row_from_exposure_record(d)
        if row is None:
            continue
        b_ns = row["timespan_begin_utc_ns"]
        if b_ns not in best:
            best[b_ns] = row
        elif prefer_longest_exptime:
            old_t = best[b_ns].get("exposure_time") or 0.0
            new_t = row.get("exposure_time") or 0.0
            if new_t > old_t:
                best[b_ns] = row

    return best


def pointings_from_repos(
    repos,
    *,
    n_workers: int = 4,
    collections=None,
    instruments=("LSSTCam", "LSSTComCam"),
    ignore_observation_types=("dark", "bias", "flat", "indome", "cwfs", "stuttered"),
    day_obs_min=None,
    day_obs_max=None,
    prefer_longest_exptime=False,
    export_fields=None,
    export_max_age_days: int = 4,
    full_csv_path: str = "",
    export_csv_path: str = "",
    only_science: bool = False,
    cone_ra: float | None = None,
    cone_dec: float | None = None,
    cone_radius_deg: float | None = None,
):
    """Query Butler exposure records and return full/reduced pointing dataframes."""
    if isinstance(repos, str):
        repos = [repos]

    n_workers_eff = max(1, min(int(n_workers), len(repos)))
    where_parts = ["instrument IN (" + ",".join(f"'{i}'" for i in instruments) + ")"]
    if only_science:
        where_parts.append("observation_type = 'science'")
    if ignore_observation_types:
        where_parts.append(
            "observation_type NOT IN (" + ",".join(f"'{t}'" for t in ignore_observation_types) + ")"
        )
    if day_obs_min is not None:
        where_parts.append(f"day_obs >= {int(day_obs_min)}")
    if day_obs_max is not None:
        where_parts.append(f"day_obs <= {int(day_obs_max)}")
    where = " AND ".join(where_parts)

    best = {}
    if n_workers_eff <= 1:
        for repo in repos:
            butler = _make_butler(repo, collections=collections)
            total = _count_exposures(butler, where)
            records = butler.registry.queryDimensionRecords("exposure", where=where)

            cone_center = None
            if cone_radius_deg is not None or cone_ra is not None or cone_dec is not None:
                if cone_ra is None or cone_dec is None or cone_radius_deg is None:
                    raise ValueError("Cone search requires --cone-ra, --cone-dec, and --cone-radius-deg.")
                cone_center = SkyCoord(
                    ra=float(cone_ra) * u.deg, dec=float(cone_dec) * u.deg, frame="icrs"
                )

            for exp in tqdm(records, desc=f"Reading exposures ({repo})", total=total):
                d = _exp_to_dict(exp)
                d["repo"] = str(repo)
                if cone_center is not None and not _passes_cone(
                    d, cone_center, float(cone_radius_deg)
                ):
                    continue

                row = _row_from_exposure_record(d)
                if row is None:
                    continue
                b_ns = row["timespan_begin_utc_ns"]
                if b_ns not in best:
                    best[b_ns] = row
                elif prefer_longest_exptime:
                    old_t = best[b_ns].get("exposure_time") or 0.0
                    new_t = row.get("exposure_time") or 0.0
                    if new_t > old_t:
                        best[b_ns] = row
    else:
        results = process_map(
            _scan_repo_for_pointings,
            repos,
            repeat(collections),
            repeat(where),
            repeat(prefer_longest_exptime),
            repeat(cone_ra),
            repeat(cone_dec),
            repeat(cone_radius_deg),
            max_workers=int(n_workers_eff),
            chunksize=1,
            desc="Scanning repos (parallel)",
        )
        for repo_best in results:
            for b_ns, row in repo_best.items():
                if b_ns not in best:
                    best[b_ns] = row
                elif prefer_longest_exptime:
                    old_t = best[b_ns].get("exposure_time") or 0.0
                    new_t = row.get("exposure_time") or 0.0
                    if new_t > old_t:
                        best[b_ns] = row

    df_full = pd.DataFrame(best.values()).sort_values("timespan_begin_utc_ns").reset_index(drop=True)
    df_full["timespan_begin_utc"] = pd.to_datetime(df_full["timespan_begin_utc"], utc=True)
    df_full["timespan_end_utc"] = pd.to_datetime(df_full["timespan_end_utc"], utc=True)
    df_full["midpoint_utc"] = pd.to_datetime(df_full["midpoint_utc"], utc=True, errors="coerce")

    if full_csv_path:
        _write_csv(df_full, full_csv_path)

    df_export = None
    if export_fields is not None:
        missing = [column for column in export_fields if column not in df_full.columns]
        if missing:
            raise KeyError(f"export_fields contains missing columns: {missing}")

        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=int(export_max_age_days))
        df_export = df_full.loc[df_full["timespan_begin_utc"] <= cutoff, export_fields].copy()
        if export_csv_path:
            _write_csv(df_export, export_csv_path)

    return df_full, df_export


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Query Rubin Butler exposure records or convert an existing Sorcha pointing CSV "
            "into Ponder/Sorcha pointing products."
        )
    )
    parser.add_argument(
        "--repos",
        nargs="+",
        default=["/repo/dp1", "dp2_prep", "embargo", "/repo/main"],
        help="One or more Butler repo roots to query.",
    )
    parser.add_argument("--collections", nargs="+", default=None)
    parser.add_argument("--n-workers", type=int, default=4)
    parser.add_argument("--instruments", nargs="+", default=["LSSTCam", "LSSTComCam"])
    parser.add_argument("--only-science", action="store_true")
    parser.add_argument(
        "--ignore-observation-types",
        nargs="+",
        default=["dark", "bias", "flat", "indome", "cwfs", "stuttered"],
    )
    parser.add_argument("--day-obs-min", type=int, default=None)
    parser.add_argument("--day-obs-max", type=int, default=None)
    parser.add_argument("--cone-ra", type=float, default=None)
    parser.add_argument("--cone-dec", type=float, default=None)
    parser.add_argument("--cone-radius-deg", type=float, default=None)
    parser.add_argument("--prefer-longest-exptime", action="store_true")
    parser.add_argument("--export-max-age-days", type=int, default=4)
    parser.add_argument("--full-csv", default="pointings_full.csv")
    parser.add_argument("--export-csv", default="pointings_export.csv")
    parser.add_argument("--sorcha-csv", default="")
    parser.add_argument(
        "--sqlite-db",
        default="",
        help="Optional path to write a SQLite DB with an observations table.",
    )
    parser.add_argument(
        "--sqlite-source-csv",
        default="",
        help="Convert an existing Sorcha pointing CSV to --sqlite-db and skip Butler queries.",
    )
    parser.add_argument(
        "--export-fields",
        nargs="+",
        default=[
            "timespan_begin_utc",
            "midpoint_utc",
            "midpoint_tai",
            "timespan_end_utc",
            "repo",
            "instrument",
            "exposure_id",
            "day_obs",
            "physical_filter",
            "exposure_time",
            "observation_type",
            "tracking_ra",
            "tracking_dec",
            "sky_angle",
        ],
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    sqlite_db = str(Path(args.sqlite_db).expanduser()) if args.sqlite_db else ""
    if args.sqlite_source_csv:
        if not sqlite_db:
            raise ValueError("--sqlite-source-csv requires --sqlite-db")
        sqlite_path = write_observations_sqlite_from_sorcha_csv(args.sqlite_source_csv, sqlite_db)
        with sqlite3.connect(sqlite_path) as con:
            count = con.execute(f"SELECT COUNT(*) FROM {OBSERVATIONS_TABLE}").fetchone()[0]
        print(f"Wrote SQLite observations DB: {sqlite_path} ({count:,} rows)")
        return 0

    full_csv = str(Path(args.full_csv).expanduser()) if args.full_csv else ""
    export_csv = str(Path(args.export_csv).expanduser()) if args.export_csv else ""
    sorcha_csv = str(Path(args.sorcha_csv).expanduser()) if args.sorcha_csv else ""

    n_workers_eff = max(1, min(int(args.n_workers), len(args.repos)))
    if n_workers_eff != int(args.n_workers):
        print(
            f"Adjusting --n-workers from {int(args.n_workers)} to {n_workers_eff} "
            f"(number of repos = {len(args.repos)})."
        )

    df_full, df_export = pointings_from_repos(
        repos=args.repos,
        n_workers=n_workers_eff,
        collections=args.collections,
        instruments=tuple(args.instruments),
        ignore_observation_types=tuple(args.ignore_observation_types),
        day_obs_min=args.day_obs_min,
        day_obs_max=args.day_obs_max,
        prefer_longest_exptime=args.prefer_longest_exptime,
        export_fields=list(args.export_fields) if args.export_fields else None,
        export_max_age_days=int(args.export_max_age_days),
        full_csv_path=full_csv,
        export_csv_path=export_csv,
        only_science=args.only_science,
        cone_ra=args.cone_ra,
        cone_dec=args.cone_dec,
        cone_radius_deg=args.cone_radius_deg,
    )

    print(f"Wrote full CSV: {full_csv} ({len(df_full):,} rows)")
    if df_export is not None:
        print(f"Wrote export CSV: {export_csv} ({len(df_export):,} rows)")
    else:
        print("No export CSV written (export_fields was None).")

    if sorcha_csv or sqlite_db:
        df_sorcha = make_sorcha_dataframe(df_full)
        if sorcha_csv:
            _write_csv(df_sorcha, sorcha_csv)
            print(f"Wrote Sorcha CSV: {sorcha_csv} ({len(df_sorcha):,} rows)")
        if sqlite_db:
            observations = observations_dataframe_from_sorcha(df_sorcha)
            sqlite_path = _write_sqlite(observations, sqlite_db)
            print(f"Wrote SQLite observations DB: {sqlite_path} ({len(observations):,} rows)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
