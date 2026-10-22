"""Export Rubin Butler exposure records into Ponder/Sorcha pointing CSVs.

This utility is intentionally kept in ``ponder_tools`` instead of the core
``ponder`` package: it helps build pointing inputs, but it is not part of the
Sorcha orchestration path. Butler is imported lazily so the package remains
importable in normal Python environments that do not have the LSST stack set up.
"""

from __future__ import annotations

import argparse
import math
import re
from datetime import timezone
from itertools import repeat
from pathlib import Path
from typing import Any

import astropy.units as u
import pandas as pd
from astropy.coordinates import SkyCoord
from tqdm.auto import tqdm
from tqdm.contrib.concurrent import process_map


def _exp_to_dict(exp) -> dict:
    if hasattr(exp, "toDict"):
        return exp.toDict()
    if hasattr(exp, "to_dict"):
        return exp.to_dict()
    raise AttributeError("Exposure record has neither toDict() nor to_dict().")


def _timespan_to_utc_ns(timespan):
    """Return UTC datetimes and nanosecond timestamps for a Butler timespan."""
    if timespan is None:
        return None, None, None, None

    b = timespan.begin
    e = timespan.end

    b_utc = b.utc
    e_utc = e.utc

    b_dt = b_utc.to_datetime(timezone=timezone.utc)
    e_dt = e_utc.to_datetime(timezone=timezone.utc)

    b_ns = int(round(b_utc.unix * 1e9))
    e_ns = int(round(e_utc.unix * 1e9))

    return b_dt, e_dt, b_ns, e_ns


def _make_butler(repo, collections=None):
    """Build a Butler only when an export actually runs.

    Importing Butler at module import time makes lightweight commands such as
    ``--help`` fail outside an activated LSST stack, so keep it behind this
    runtime helper.
    """
    from lsst.daf.butler import Butler

    return Butler(repo, collections=collections)


def _count_exposures(butler: Any, where: str) -> int:
    return butler.registry.queryDataIds("exposure", where=where).count()


def _write_csv(df: pd.DataFrame, path: str | Path) -> None:
    """Write a CSV, creating explicit output directories on demand."""
    csv_path = Path(path).expanduser()
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path, index=False)


# --- Cone search helpers ---


def _looks_like_radians(ra: float, dec: float) -> bool:
    """Heuristic: Rubin metadata is sometimes in radians; treat small values as rad."""
    if ra is None or dec is None:
        return False
    try:
        ra = float(ra)
        dec = float(dec)
    except Exception:
        return False
    return (0.0 <= ra <= 2.0 * math.pi + 0.2) and (-math.pi / 2 - 0.2 <= dec <= math.pi / 2 + 0.2)


def _ra_dec_to_deg(ra: float, dec: float) -> tuple[float, float]:
    """Return (ra_deg, dec_deg), handling possible radian inputs."""
    ra_f = float(ra)
    dec_f = float(dec)
    if _looks_like_radians(ra_f, dec_f):
        return math.degrees(ra_f) % 360.0, math.degrees(dec_f)
    return ra_f % 360.0, dec_f


def _passes_cone(d: dict, cone_center: SkyCoord, cone_radius_deg: float) -> bool:
    """Return True if record dict d is within cone; False if missing coords."""
    ra = d.get("tracking_ra")
    dec = d.get("tracking_dec")
    if ra is None or dec is None:
        return False
    try:
        ra_deg, dec_deg = _ra_dec_to_deg(ra, dec)
    except Exception:
        return False
    c = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs")
    sep = c.separation(cone_center)
    return sep.deg <= float(cone_radius_deg)


#
# --- Sorcha export helpers ---


def _band_from_physical_filter(physical_filter: str | None) -> str:
    """Best-effort conversion from Rubin physical_filter to Sorcha band (u,g,r,i,z,y)."""
    if physical_filter is None:
        return ""
    pf = str(physical_filter).strip().lower()
    if pf in {"u", "g", "r", "i", "z", "y"}:
        return pf
    # Common patterns like "g_01" or "HSC-G" or similar: grab first standalone ugrizy
    m = re.search(r"\b([ugrizy])\b", pf)
    if m:
        return m.group(1)
    # Fallback: if it starts with a band letter
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


def make_sorcha_dataframe(df_full: pd.DataFrame) -> pd.DataFrame:
    """Create a Sorcha-formatted observations CSV dataframe from df_full.

    The Butler exposure records provide timing, filter, pointing, and rotation.
    Seeing/depth values are fixed placeholders here because this exporter is
    meant to create a usable pointing table, not derive observing-condition
    estimates.
    """
    df = df_full.copy()

    # observationStartMJD_TAI from JD(TAI)
    mjd_tai = None
    if "timespan_begin_tai_jd" in df.columns:
        mjd_tai = df["timespan_begin_tai_jd"].astype(float) - 2400000.5
    else:
        # Fallback from UTC datetime (less ideal; Sorcha wants TAI)
        mjd_tai = pd.to_datetime(df["timespan_begin_utc"], utc=True).astype("int64") / 1e9
        # unix seconds -> MJD(UTC) approx
        mjd_tai = mjd_tai / 86400.0 + 40587.0

    # Band from physical_filter
    band = (
        df.get("physical_filter").map(_band_from_physical_filter) if "physical_filter" in df.columns else ""
    )

    # Angles
    ra_deg = df.get("tracking_ra")
    dec_deg = df.get("tracking_dec")
    rot_deg = df.get("sky_angle").map(_angle_to_deg) if "sky_angle" in df.columns else None

    out = pd.DataFrame(
        {
            "observationId": df.get("exposure_id"),
            "observationStartMJD_TAI": mjd_tai,
            "visitExposureTime": df.get("exposure_time"),
            "filter": band,
            "seeingFwhmGeom_arcsec": 1.0,
            "seeingFwhmEff_arcsec": 1.0,
            "fieldFiveSigmaDepth_mag": 25.0,
            "fieldRA_deg": ra_deg,
            "fieldDec_deg": dec_deg,
            "fieldRotSkyPos_deg": rot_deg,
        }
    )

    return out


def _scan_repo_for_pointings(
    repo: str, collections, where: str, prefer_longest_exptime: bool, cone_ra, cone_dec, cone_radius_deg
) -> dict[int, dict]:
    """Scan one Butler repo and return pointings keyed by exposure start time.

    The key matches the top-level merge key used across repos, so worker results
    can be combined without carrying duplicate rows from overlapping Butler
    repositories.
    """
    butler = _make_butler(repo, collections=collections)
    records = butler.registry.queryDimensionRecords("exposure", where=where)

    # Cone search setup (constructed inside worker for pickling safety)
    cone_center = None
    if cone_radius_deg is not None or cone_ra is not None or cone_dec is not None:
        if cone_ra is None or cone_dec is None or cone_radius_deg is None:
            raise ValueError("Cone search requires --cone-ra, --cone-dec, and --cone-radius-deg.")
        cone_center = SkyCoord(ra=float(cone_ra) * u.deg, dec=float(cone_dec) * u.deg, frame="icrs")

    # Deduplicate within the repo first; cross-repo duplicates are resolved
    # after process_map returns using the same key and tie-break rule.
    best: dict[int, dict] = {}

    for exp in records:
        d = _exp_to_dict(exp)

        # Cone filtering (if enabled)
        if cone_center is not None:
            if not _passes_cone(d, cone_center, float(cone_radius_deg)):
                continue

        b_dt, e_dt, b_ns, e_ns = _timespan_to_utc_ns(d.get("timespan"))
        if b_ns is None:
            continue

        # Exposure midpoints: use the midpoint of the timespan.
        # Timespan bounds are astropy Time objects (often TAI). We compute the midpoint in TAI,
        # convert to UTC for a timezone-aware datetime, and store a TAI ISO string + TAI JD.
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

        row = dict(
            repo=str(repo),
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

        if b_ns not in best:
            best[b_ns] = row
        else:
            if prefer_longest_exptime:
                old = best[b_ns]
                old_t = old.get("exposure_time") or 0.0
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
    """
    Returns (df_full, df_export).

    df_full: all columns, all deduped pointings
    df_export: only export_fields (in order), and filtered to exclude rows newer than
               export_max_age_days (default 4). If export_fields is None, df_export is None.

    CSVs:
      - full_csv_path -> writes df_full
      - export_csv_path -> writes df_export (if export_fields provided)
    """
    if isinstance(repos, str):
        repos = [repos]

    # Effective worker count: don't spawn more workers than repos.
    n_workers_eff = max(1, min(int(n_workers), len(repos)))

    # Build one Butler WHERE clause and share it across all repos/workers so
    # sequential and parallel scans apply identical selection criteria.
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

    # Merged pointings are keyed by exposure start time. The default keeps the
    # first row seen in repo order; --prefer-longest-exptime changes only ties.
    best = {}

    # Scan repos either sequentially (n_workers=1) with per-repo tqdm, or in parallel.
    if n_workers_eff <= 1:
        for repo in repos:
            butler = _make_butler(repo, collections=collections)
            total = _count_exposures(butler, where)
            records = butler.registry.queryDimensionRecords("exposure", where=where)

            # Cone search setup (sequential path)
            cone_center = None
            if cone_radius_deg is not None or cone_ra is not None or cone_dec is not None:
                if cone_ra is None or cone_dec is None or cone_radius_deg is None:
                    raise ValueError("Cone search requires --cone-ra, --cone-dec, and --cone-radius-deg.")
                cone_center = SkyCoord(ra=float(cone_ra) * u.deg, dec=float(cone_dec) * u.deg, frame="icrs")

            for exp in tqdm(records, desc=f"Reading exposures ({repo})", total=total):
                d = _exp_to_dict(exp)

                # Cone filtering (if enabled)
                if cone_center is not None:
                    if not _passes_cone(d, cone_center, float(cone_radius_deg)):
                        continue

                b_dt, e_dt, b_ns, e_ns = _timespan_to_utc_ns(d.get("timespan"))
                if b_ns is None:
                    continue

                # Exposure midpoints: use the midpoint of the timespan.
                # Timespan bounds are astropy Time objects (often TAI). We compute the midpoint in TAI,
                # convert to UTC for a timezone-aware datetime, and store a TAI ISO string + TAI JD.
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

                row = dict(
                    repo=str(repo),
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

                if b_ns not in best:
                    best[b_ns] = row
                else:
                    if prefer_longest_exptime:
                        old = best[b_ns]
                        old_t = old.get("exposure_time") or 0.0
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

        # Merge per-repo dicts; resolve collisions the same way as the sequential path.
        for repo_best in results:
            for b_ns, row in repo_best.items():
                if b_ns not in best:
                    best[b_ns] = row
                else:
                    if prefer_longest_exptime:
                        old = best[b_ns]
                        old_t = old.get("exposure_time") or 0.0
                        new_t = row.get("exposure_time") or 0.0
                        if new_t > old_t:
                            best[b_ns] = row

    df_full = pd.DataFrame(best.values()).sort_values("timespan_begin_utc_ns").reset_index(drop=True)

    df_full["timespan_begin_utc"] = pd.to_datetime(df_full["timespan_begin_utc"], utc=True)
    df_full["timespan_end_utc"] = pd.to_datetime(df_full["timespan_end_utc"], utc=True)
    df_full["midpoint_utc"] = pd.to_datetime(df_full["midpoint_utc"], utc=True, errors="coerce")

    # (1) export the full df
    if full_csv_path:
        _write_csv(df_full, full_csv_path)

    df_export = None

    # (2) special export: select fields + drop rows newer than N days ago
    if export_fields is not None:
        missing = [c for c in export_fields if c not in df_full.columns]
        if missing:
            raise KeyError(f"export_fields contains missing columns: {missing}")

        # cutoff = now - N days. Drop anything with start time > cutoff (i.e., too new)
        now_utc = pd.Timestamp.now(tz="UTC")
        cutoff = now_utc - pd.Timedelta(days=int(export_max_age_days))

        df_export = df_full.loc[df_full["timespan_begin_utc"] <= cutoff, export_fields].copy()

        if export_csv_path:
            _write_csv(df_export, export_csv_path)

    return df_full, df_export


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Query Butler exposure dimension records across one or more repos, "
            "deduplicate by timespan begin, and write full + reduced CSV exports."
        )
    )

    p.add_argument(
        "--repos",
        nargs="+",
        default=["/repo/dp1", "dp2_prep", "embargo", "/repo/main"],
        help="One or more Butler repo roots to query.",
    )
    p.add_argument(
        "--collections",
        nargs="+",
        default=None,
        help="Optional collections to use when constructing the Butler.",
    )
    p.add_argument(
        "--n-workers",
        type=int,
        default=4,
        help="Number of worker processes to use when scanning repos in parallel (default: 4).",
    )
    p.add_argument(
        "--instruments",
        nargs="+",
        default=["LSSTCam", "LSSTComCam"],
        help="Instruments to include (default: LSSTCam LSSTComCam).",
    )
    p.add_argument(
        "--only-science",
        action="store_true",
        help="If set, include only observation_type='science' (default: false).",
    )
    p.add_argument(
        "--ignore-observation-types",
        nargs="+",
        default=["dark", "bias", "flat", "indome", "cwfs", "stuttered"],
        help="Observation types to exclude.",
    )
    p.add_argument(
        "--day-obs-min",
        type=int,
        default=None,
        help="Optional minimum day_obs (YYYYMMDD).",
    )
    p.add_argument(
        "--day-obs-max",
        type=int,
        default=None,
        help="Optional maximum day_obs (YYYYMMDD).",
    )
    p.add_argument(
        "--cone-ra",
        type=float,
        default=None,
        help="Cone search center RA in degrees (ICRS). Requires --cone-dec and --cone-radius-deg.",
    )
    p.add_argument(
        "--cone-dec",
        type=float,
        default=None,
        help="Cone search center Dec in degrees (ICRS). Requires --cone-ra and --cone-radius-deg.",
    )
    p.add_argument(
        "--cone-radius-deg",
        type=float,
        default=None,
        help="Cone search radius in degrees. Requires --cone-ra and --cone-dec.",
    )
    p.add_argument(
        "--prefer-longest-exptime",
        action="store_true",
        help="When multiple records share the same timespan begin, keep the one with longest exposure_time.",
    )
    p.add_argument(
        "--export-max-age-days",
        type=int,
        default=4,
        help=(
            "For the reduced export CSV, drop rows with timespan_begin_utc newer than now - N days. "
            "Default: 4."
        ),
    )
    p.add_argument(
        "--full-csv",
        default="pointings_full.csv",
        help="Path to write the full CSV (all columns).",
    )
    p.add_argument(
        "--export-csv",
        default="pointings_export.csv",
        help="Path to write the reduced export CSV (export-fields only).",
    )
    p.add_argument(
        "--sorcha-csv",
        default="",
        help="Optional path to write a Sorcha-formatted observations CSV.",
    )
    p.add_argument(
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
        help="Column names (in order) for the reduced export CSV.",
    )

    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Normalize output paths for readable logging; _write_csv creates parents.
    full_csv = str(Path(args.full_csv).expanduser()) if args.full_csv else ""
    export_csv = str(Path(args.export_csv).expanduser()) if args.export_csv else ""
    sorcha_csv = str(Path(args.sorcha_csv).expanduser()) if args.sorcha_csv else ""

    n_workers_eff = max(1, min(int(args.n_workers), len(args.repos)))
    if n_workers_eff != int(args.n_workers):
        print(
            f"Adjusting --n-workers from {int(args.n_workers)} to {n_workers_eff} (number of repos = {len(args.repos)})."
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

    if sorcha_csv:
        df_sorcha = make_sorcha_dataframe(df_full)
        _write_csv(df_sorcha, sorcha_csv)
        print(f"Wrote Sorcha CSV: {sorcha_csv} ({len(df_sorcha):,} rows)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
