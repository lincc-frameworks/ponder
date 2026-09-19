"""Export packed DEEP ECSV metadata without a Butler registry.

This explicit legacy timing mode is for the audited dinob collections: their
``mjd_start`` is FITS DATE-AVG (a TAI midpoint), not a UTC exposure start.
The resulting database is intended for geometry validation, not completeness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

CORNERS = [f"{axis}_{corner}" for corner in ("bl", "tl", "tr", "br") for axis in ("ra", "dec")]
COLUMNS = [
    "dataId",
    "visit",
    "detector",
    "mjd_start",
    "mjd_mid",
    "pointing_ra",
    "pointing_dec",
    "exposureTime",
    "psfSigma",
    "psfArea",
    "zeroPoint",
    "skyNoise",
    "night",
    "fieldname",
    *CORNERS,
]


def read_joined_chunks(path, chunksize=20000):
    """Stream only necessary columns; quoted WCS blobs are never retained."""
    with open(path) as stream:
        while True:
            pos = stream.tell()
            line = stream.readline()
            if not line:
                raise ValueError("No ECSV data header")
            if not line.startswith("#"):
                stream.seek(pos)
                break
        yield from pd.read_csv(
            stream, sep=" ", skipinitialspace=True, usecols=COLUMNS, chunksize=chunksize, dtype={"night": str}
        )


def convert_rows(frame, *, band, time_semantics):
    if time_semantics != "date-avg-tai":
        raise ValueError("Explicit audited date-avg-tai timing semantics are required")
    if not band or band.strip() != band:
        raise ValueError("A nonempty explicit observing band is required")
    rows = frame.copy()
    required = ["mjd_start", "mjd_mid", "exposureTime", "pointing_ra", "pointing_dec", *CORNERS]
    if not np.isfinite(rows[required].to_numpy(dtype=float)).all():
        raise ValueError("Nonfinite timing or geometry")
    if (rows.exposureTime <= 0).any():
        raise ValueError("Exposure duration must be positive")
    if (
        (rows.pointing_ra < 0)
        | (rows.pointing_ra >= 360)
        | (rows.pointing_dec < -90)
        | (rows.pointing_dec > 90)
    ).any():
        raise ValueError("Invalid pointing coordinates")
    # This consistency check identifies the legacy midpoint-as-start pattern.
    # Legacy collections add (exposureTime + 1) / 2: e.g. 60.5 for 120 seconds.
    # That extra 0.5 second is not part of the real exposure duration.
    delta = (rows.mjd_mid - rows.mjd_start) * 86400 - (rows.exposureTime + 1) / 2
    if np.max(np.abs(delta)) > 0.002:
        raise ValueError("Collection does not have the audited legacy timing pattern")
    rows["observationStartMJD"] = rows.mjd_start - rows.exposureTime / 2 / 86400
    rows["midpointMJD_TAI"] = rows.mjd_start
    rows["band"] = band
    # Diagnostic estimates only. No brightness/depth selection is enabled.
    rows["seeing"] = rows.psfSigma * 2.354820045 * 0.263
    with np.errstate(invalid="ignore", divide="ignore"):
        rows["depth"] = rows.zeroPoint - 2.5 * np.log10(5 * rows.skyNoise * np.sqrt(rows.psfArea))
    return rows


def collapse_visits(rows):
    """Deduplicate detectors while rejecting inconsistent exposure metadata."""
    grouped = rows.groupby("visit", sort=True)
    for column, tolerance in [
        ("observationStartMJD", 0.002 / 86400),
        ("exposureTime", 0.002),
        ("pointing_ra", 0.1 / 3600),
        ("pointing_dec", 0.1 / 3600),
    ]:
        spread = grouped[column].max() - grouped[column].min()
        if (spread > tolerance).any():
            raise ValueError(f"Conflicting {column} for visit {spread.idxmax()}")
    for column in ["band", "night", "fieldname"]:
        if (grouped[column].nunique() != 1).any():
            raise ValueError(f"Conflicting {column} within a visit")
    med = grouped[
        [
            "observationStartMJD",
            "midpointMJD_TAI",
            "exposureTime",
            "pointing_ra",
            "pointing_dec",
            "seeing",
            "depth",
        ]
    ].median()
    if not np.isfinite(med.to_numpy()).all():
        raise ValueError("No finite visit-level geometry or diagnostic seeing/depth")
    result = pd.DataFrame(
        {
            "observationId": med.index,
            "observationStartMJD": med.observationStartMJD.values,
            "visitTime": med.exposureTime.values,
            "visitExposureTime": med.exposureTime.values,
            "band": grouped.band.first().values,
            "seeingFwhmGeom": med.seeing.values,
            "seeingFwhmEff": med.seeing.values,
            "fiveSigmaDepth": med.depth.values,
            "fieldRA": med.pointing_ra.values,
            "fieldDec": med.pointing_dec.values,
            # Ignored by the circular candidate cone. Actual detector corners are retained separately.
            "rotSkyPos": 0.0,
            "midpointMJD_TAI": med.midpointMJD_TAI.values,
            "night": grouped.night.first().values,
            "fieldname": grouped.fieldname.first().values,
            "detectorCount": grouped.detector.nunique().values,
        }
    )
    return result


def export_deep(source, output, *, band, time_semantics):
    source, output = Path(source), Path(output)
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = pd.concat(read_joined_chunks(source), ignore_index=True)
    converted = convert_rows(raw, band=band, time_semantics=time_semantics)
    # Distinct dataset UUIDs may describe identical metadata; retain their aliases.
    duplicate = converted.duplicated(["visit", "detector"], keep=False)
    for _, group in converted.loc[duplicate].groupby(["visit", "detector"]):
        if len(group.drop(columns="dataId").drop_duplicates()) != 1:
            raise ValueError("Conflicting duplicate visit/detector record")
    aliases = converted.loc[duplicate, ["dataId", "visit", "detector"]].copy()
    converted = converted.drop_duplicates(["visit", "detector"])
    observations = collapse_visits(converted)
    footprints = converted[["dataId", "visit", "detector", *CORNERS]].rename(
        columns={"visit": "observationId"}
    )
    with sqlite3.connect(output) as con:
        observations.to_sql("observations", con, index=False)
        footprints.to_sql("detector_footprints", con, index=False)
        aliases.to_sql("duplicate_dataset_aliases", con, index=False)
        con.execute("CREATE UNIQUE INDEX observation_id ON observations(observationId)")
        con.execute("CREATE UNIQUE INDEX footprint_id ON detector_footprints(observationId,detector)")
        converted.groupby(["night", "fieldname"]).first().reset_index().to_sql(
            "audit_samples", con, index=False
        )
    with source.open("rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    manifest = dict(
        source=str(source.resolve()),
        source_sha256=digest,
        source_rows=len(raw),
        detector_rows=len(footprints),
        visits=len(observations),
        duplicate_dataset_alias_rows=len(aliases),
        band=band,
        observatory="W84",
        time_semantics=time_semantics,
        output_time_scale="TAI",
        output_time_origin="exposure start",
        formula="observationStartMJD = collection.mjd_start - exposureTime / 172800",
        warning="Explicit legacy convention; verify representative source FITS before science use. "
        "Circular cone is not detector coverage. Depth/seeing are diagnostic estimates, "
        "rotSkyPos=0 is ignored by the circle model, and no survey completeness is implied.",
        database_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
    )
    output.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collection", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--band", required=True)
    parser.add_argument("--time-semantics", choices=["date-avg-tai"], required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            export_deep(args.collection, args.output, band=args.band, time_semantics=args.time_semantics),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
