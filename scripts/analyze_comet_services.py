#!/usr/bin/env python
"""Compare LSSTCam comet coverage across SkyBot, JPL, and Ponder."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

import pandas as pd
from astropy.time import Time
from pyarrow import parquet as pq
from rubin_comet_catchers.ponder_grabber import obsnight_from_time_series
from rubin_comet_catchers.services import normalize_object_name_value

SERVICES = ("skybot", "jpl", "ponder")
SERVICE_FILES = {
    "skybot": "skybot_results.parquet",
    "jpl": "jpl_results.parquet",
}
NAME_COLUMNS = {
    "skybot": ("object_name", "Name", "Clean Name"),
    "jpl": ("object_name", "Quaero Name", "Clean Name", "Object name", "Name"),
}
COMET_NAME_PATTERN = r"^(?:[CPDXAI]/|[0-9]+[PI](?:/|$))"
NUMBERED_INTERSTELLAR_PATTERN = re.compile(r"^\s*([0-9]+I)(?:/|$)", re.IGNORECASE)


def _first_column(columns, candidates):
    return next((column for column in candidates if column in columns), None)


def _visit_series(frame: pd.DataFrame) -> pd.Series:
    output = pd.Series(pd.NA, index=frame.index, dtype="string")
    for column in (
        "visit",
        "visit_id",
        "visit_id_consdb",
        "visit_id_lookup",
        "visit_id_fallback",
    ):
        if column not in frame:
            continue
        values = frame[column].astype("string")
        output = output.where(output.notna(), values)
    return output.str.replace(r"\.0$", "", regex=True)


def _is_comet(frame: pd.DataFrame, name_column: str) -> pd.Series:
    mask = (
        frame[name_column]
        .astype("string")
        .str.match(COMET_NAME_PATTERN, case=False, na=False)
    )
    if "Class" in frame:
        mask |= (
            frame["Class"].astype("string").str.contains("comet", case=False, na=False)
        )
    return mask


def _canonical_identity(display_name: object, normalized_name: object) -> object:
    """Collapse numbered interstellar names such as 3I and 3I/ATLAS."""
    match = NUMBERED_INTERSTELLAR_PATTERN.match(str(display_name))
    if match:
        return match.group(1).lower()
    return normalized_name


def read_service_records(nightly_root: Path) -> tuple[pd.DataFrame, dict[str, int]]:
    frames = []
    row_counts = {service: 0 for service in SERVICES}
    for service, filename in SERVICE_FILES.items():
        for path in sorted(nightly_root.rglob(filename)):
            available = pq.ParquetFile(path).schema.names
            name_column = _first_column(available, NAME_COLUMNS[service])
            if name_column is None:
                continue
            wanted = {
                name_column,
                "Class",
                "visit",
                "visit_id",
                "visit_id_consdb",
                "visit_id_lookup",
                "visit_id_fallback",
            }
            frame = pd.read_parquet(
                path, columns=[column for column in wanted if column in available]
            )
            frame = frame.loc[_is_comet(frame, name_column)].copy()
            if frame.empty:
                continue
            row_counts[service] += len(frame)
            display_names = frame[name_column].astype("string")
            normalized_names = display_names.map(normalize_object_name_value)
            normalized_names = pd.Series(
                (
                    _canonical_identity(display, normalized)
                    for display, normalized in zip(
                        display_names, normalized_names, strict=True
                    )
                ),
                index=frame.index,
                dtype="string",
            )
            frames.append(
                pd.DataFrame(
                    {
                        "service": service,
                        "name_norm": normalized_names,
                        "display_name": display_names,
                        "obsnight": path.parent.name,
                        "visit": _visit_series(frame),
                    }
                )
            )
    return pd.concat(frames, ignore_index=True), row_counts


def _pointing_visits(db_path: Path) -> pd.DataFrame:
    with sqlite3.connect(db_path) as connection:
        frame = pd.read_sql_query(
            "SELECT observationId AS visit, "
            "observationStartMJD + visitExposureTime / 2.0 / 86400.0 AS fieldMJD_TAI "
            "FROM observations",
            connection,
        )
    frame["visit"] = frame["visit"].astype("Int64").astype("string")
    return frame.sort_values("fieldMJD_TAI")


def read_ponder_records(
    detections_path: Path,
    pointing_db: Path,
    selected_nights: set[str],
) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    detections = pd.read_parquet(detections_path)
    if "object_mode" in detections:
        detections = detections[
            detections["object_mode"].astype("string").eq("comet")
        ].copy()
    detections["obsnight"] = obsnight_from_time_series(
        detections["fieldMJD_TAI"], column="fieldMJD_TAI"
    ).astype("string")
    detections = detections[detections["obsnight"].isin(selected_nights)].copy()

    visits = _pointing_visits(pointing_db)
    detections = pd.merge_asof(
        detections.sort_values("fieldMJD_TAI"),
        visits,
        on="fieldMJD_TAI",
        direction="nearest",
        tolerance=1e-7,
    )
    missing_visits = int(detections["visit"].isna().sum())
    normalized_names = detections["ObjID"].map(normalize_object_name_value)
    detections["name_norm"] = [
        _canonical_identity(display, normalized)
        for display, normalized in zip(
            detections["ObjID"], normalized_names, strict=True
        )
    ]
    records = pd.DataFrame(
        {
            "service": "ponder",
            "name_norm": detections["name_norm"],
            "display_name": detections["ObjID"].astype("string"),
            "obsnight": detections["obsnight"],
            "visit": detections["visit"],
        }
    )
    return records, detections, missing_visits


def membership_table(records: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    membership = (
        records.assign(present=True)
        .drop_duplicates([*keys, "service"])
        .pivot_table(
            index=keys,
            columns="service",
            values="present",
            aggfunc="any",
            fill_value=False,
        )
        .reset_index()
    )
    for service in SERVICES:
        if service not in membership:
            membership[service] = False
        membership[service] = membership[service].astype(bool)
    membership["combination"] = membership.apply(
        lambda row: "+".join(service for service in SERVICES if row[service]), axis=1
    )
    return membership


def _display_names(records: pd.DataFrame) -> dict[str, str]:
    priority = {
        service: index for index, service in enumerate(("ponder", "skybot", "jpl"))
    }
    values = records.dropna(subset=["name_norm", "display_name"]).copy()
    values["priority"] = values["service"].map(priority)
    values.sort_values(["name_norm", "priority"], inplace=True)
    return (
        values.drop_duplicates("name_norm")
        .set_index("name_norm")["display_name"]
        .to_dict()
    )


def _membership_counts(grain: str, frame: pd.DataFrame) -> pd.DataFrame:
    return (
        frame.groupby("combination", dropna=False)
        .size()
        .rename("count")
        .reset_index()
        .assign(grain=grain)
        .loc[:, ["grain", "combination", "count"]]
    )


def representative_cutouts(
    ponder: pd.DataFrame, ponder_only_names: set[str]
) -> pd.DataFrame:
    candidates = ponder[ponder["name_norm"].isin(ponder_only_names)].copy()
    candidates["margin_mag"] = pd.to_numeric(
        candidates.get("fiveSigmaDepth_mag"), errors="coerce"
    ) - pd.to_numeric(candidates.get("trailedSourceMag"), errors="coerce")
    candidates.sort_values(
        ["name_norm", "margin_mag", "fieldMJD_TAI"],
        ascending=[True, False, True],
        inplace=True,
    )
    selected = candidates.drop_duplicates("name_norm").copy()
    tai = Time(
        selected["fieldMJD_TAI"].to_numpy(dtype=float), format="mjd", scale="tai"
    )
    tai.precision = 6
    selected["Name"] = selected["ObjID"]
    selected["Class"] = "Comet"
    selected["RA"] = selected["RA_deg"]
    selected["Dec"] = selected["Dec_deg"]
    selected["Query_Date_TAI_ISOT"] = tai.isot
    selected["Date midpoint"] = selected["Query_Date_TAI_ISOT"]
    selected["VMag (mag)"] = selected.get("trailedSourceMag")
    selected["V_depth"] = selected.get("fiveSigmaDepth_mag")
    return selected.sort_values("Name")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nightly-root", type=Path, required=True)
    parser.add_argument("--ponder-detections", type=Path, required=True)
    parser.add_argument("--pointing-db", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    nightly_root = args.nightly_root.expanduser().resolve()
    selected_nights = {
        path.name for path in nightly_root.glob("*/*/????-??-??") if path.is_dir()
    }

    service_records, row_counts = read_service_records(nightly_root)
    ponder_records, ponder_rows, missing_ponder_visits = read_ponder_records(
        args.ponder_detections.expanduser().resolve(),
        args.pointing_db.expanduser().resolve(),
        selected_nights,
    )
    row_counts["ponder"] = len(ponder_records)
    records = pd.concat([service_records, ponder_records], ignore_index=True)
    records = records[records["name_norm"].notna()].copy()
    displays = _display_names(records)

    objects = membership_table(records, ["name_norm"])
    object_nights = membership_table(records, ["name_norm", "obsnight"])
    visits = membership_table(records[records["visit"].notna()], ["name_norm", "visit"])
    for frame in (objects, object_nights, visits):
        frame.insert(1, "display_name", frame["name_norm"].map(displays))

    counts = pd.concat(
        [
            _membership_counts("object", objects),
            _membership_counts("object-night", object_nights),
            _membership_counts("object-visit", visits),
        ],
        ignore_index=True,
    )
    totals = []
    for grain, frame in (
        ("object", objects),
        ("object-night", object_nights),
        ("object-visit", visits),
    ):
        for service in SERVICES:
            totals.append(
                {"grain": grain, "service": service, "count": int(frame[service].sum())}
            )
    totals_df = pd.DataFrame(totals)

    ponder_only_names = set(
        objects.loc[
            objects["ponder"] & ~objects["skybot"] & ~objects["jpl"], "name_norm"
        ]
    )
    ponder_rows["margin_mag"] = pd.to_numeric(
        ponder_rows["fiveSigmaDepth_mag"], errors="coerce"
    ) - pd.to_numeric(ponder_rows["trailedSourceMag"], errors="coerce")
    cutouts = representative_cutouts(ponder_rows, ponder_only_names)
    per_object = (
        ponder_rows[ponder_rows["name_norm"].isin(ponder_only_names)]
        .groupby(["name_norm", "ObjID"], dropna=False)
        .agg(
            detection_rows=("ObjID", "size"),
            nights=("obsnight", "nunique"),
            visits=("visit", "nunique"),
            first_mjd=("fieldMJD_TAI", "min"),
            last_mjd=("fieldMJD_TAI", "max"),
            brightest_mag=("trailedSourceMag", "min"),
            deepest_margin_mag=("margin_mag", "max"),
        )
        .reset_index()
    )

    examples = objects[objects["name_norm"].isin({"99p", "c2020u4"})].copy()

    objects.to_csv(output_dir / "object_membership.csv", index=False)
    object_nights.to_csv(output_dir / "object_night_membership.csv", index=False)
    visits.to_csv(output_dir / "object_visit_membership.csv", index=False)
    counts.to_csv(output_dir / "membership_counts.csv", index=False)
    totals_df.to_csv(output_dir / "service_totals.csv", index=False)
    per_object.sort_values(["detection_rows", "ObjID"], ascending=[False, True]).to_csv(
        output_dir / "ponder_only_comets.csv", index=False
    )
    cutouts.to_parquet(output_dir / "ponder_only_cutout_input.parquet", index=False)
    examples.to_csv(output_dir / "example_membership.csv", index=False)

    summary = {
        "night_count": len(selected_nights),
        "first_obsnight": min(selected_nights),
        "last_obsnight": max(selected_nights),
        "source_comet_rows": row_counts,
        "missing_ponder_visit_matches": missing_ponder_visits,
        "object_counts": {service: int(objects[service].sum()) for service in SERVICES},
        "object_night_counts": {
            service: int(object_nights[service].sum()) for service in SERVICES
        },
        "object_visit_counts": {
            service: int(visits[service].sum()) for service in SERVICES
        },
        "object_membership_counts": counts[counts["grain"].eq("object")]
        .set_index("combination")["count"]
        .astype(int)
        .to_dict(),
        "ponder_only_object_count": len(ponder_only_names),
        "ponder_only_cutout_rows": len(cutouts),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
