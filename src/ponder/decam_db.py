"""Export archive exposures to the pointing interface used by Ponder predictions."""

import json
import math
import os
import sqlite3
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq
from astropy.time import Time

from .decam_pointings import writer_lock


def read_db(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def valid_night(value):
    try:
        return date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
        return False


def exposure_issues(row):
    issues = []
    for name, lower, upper in (("ra_deg", 0, 360), ("dec_deg", -90, 90), ("exposure_seconds", 0, math.inf)):
        value = row.get(name)
        if value is None or not math.isfinite(value) or not lower <= value <= upper:
            issues.append("invalid_" + name)
    if row.get("ra_deg") == 360:
        issues.append("invalid_ra_deg")
    if not valid_night(row.get("observing_date")):
        issues.append("invalid_observing_date")
    if not isinstance(row.get("exposure_id"), int) or row["exposure_id"] <= 0:
        issues.append("invalid_exposure_id")
    for field in ("start_utc", "midpoint_utc"):
        if not row.get(field):
            issues.append("invalid_" + field)
    if "inconsistent_midpoint" in (row.get("quality_flags") or []):
        issues.append("inconsistent_midpoint")
    return issues


def export_pointings(catalog, output):
    """Atomically export valid exposures, keeping excluded rows and all night counts.

    observationStartMJD is TAI for compatibility with Ponder's existing DB helpers.
    Predictions use observationMidpointMJD_TAI directly, never visitTime / 2.
    """
    catalog, output = Path(catalog), Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with writer_lock(output):
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.unlink(missing_ok=True)
        con = sqlite3.connect(temporary)
        try:
            con.executescript(
                """
                CREATE TABLE observations (
                    observationId INTEGER PRIMARY KEY, observationStartMJD REAL NOT NULL,
                    observationMidpointMJD_TAI REAL NOT NULL,
                    fieldRA REAL NOT NULL, fieldDec REAL NOT NULL,
                    visitTime REAL NOT NULL, visitExposureTime REAL NOT NULL,
                    filter TEXT, observing_date TEXT NOT NULL,
                    archive_filename TEXT, proposal TEXT, proc_type TEXT,
                    start_utc TEXT, midpoint_utc TEXT, quality_flags TEXT, md5sum TEXT
                );
                CREATE TABLE excluded_exposures (
                    exposure_id INTEGER, observing_date TEXT, reasons TEXT, source_json TEXT
                );
                CREATE TABLE nights (
                    observing_date TEXT PRIMARY KEY, catalog_exposures INTEGER NOT NULL,
                    usable_exposures INTEGER NOT NULL
                );
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
            """
            )
            counts = {"catalog": 0, "usable": 0, "excluded": 0}
            source = pq.ParquetFile(catalog)
            metadata = {k.decode(): v.decode() for k, v in (source.schema_arrow.metadata or {}).items()}
            for batch in source.iter_batches(batch_size=10000):
                accepted = []
                nights = {}
                for row in batch.to_pylist():
                    counts["catalog"] += 1
                    night = row.get("observing_date")
                    if valid_night(night):
                        nights.setdefault(night, [0, 0])[0] += 1
                    issues = exposure_issues(row)
                    if not issues:
                        accepted.append(row)
                    else:
                        counts["excluded"] += 1
                        con.execute(
                            "INSERT INTO excluded_exposures VALUES (?,?,?,?)",
                            (row.get("exposure_id"), night, json.dumps(issues), json.dumps(row)),
                        )
                # The archive catalog has already parsed these instants. Vector conversion
                # preserves leap seconds and avoids millions of individual Time objects.
                if accepted:
                    starts = Time(
                        [r["start_utc"].removesuffix("Z") for r in accepted], format="isot", scale="utc"
                    ).tai
                    mids = Time(
                        [r["midpoint_utc"].removesuffix("Z") for r in accepted], format="isot", scale="utc"
                    ).tai
                    for row, start, mid, delta in zip(accepted, starts.mjd, mids.mjd, (mids - starts).sec):
                        if delta < 0 or abs(delta - row["exposure_seconds"] / 2) > 1:
                            counts["excluded"] += 1
                            con.execute(
                                "INSERT INTO excluded_exposures VALUES (?,?,?,?)",
                                (
                                    row["exposure_id"],
                                    row["observing_date"],
                                    '["inconsistent_midpoint"]',
                                    json.dumps(row),
                                ),
                            )
                            continue
                        con.execute(
                            "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                row["exposure_id"],
                                float(start),
                                float(mid),
                                row["ra_deg"],
                                row["dec_deg"],
                                row["exposure_seconds"],
                                row["exposure_seconds"],
                                row["filter"],
                                row["observing_date"],
                                row["archive_filename"],
                                row["proposal"],
                                row["proc_type"],
                                row["start_utc"],
                                row["midpoint_utc"],
                                json.dumps(row["quality_flags"]),
                                row["md5sum"],
                            ),
                        )
                        counts["usable"] += 1
                        nights[row["observing_date"]][1] += 1
                con.executemany(
                    """INSERT INTO nights VALUES (?,?,?)
                    ON CONFLICT(observing_date) DO UPDATE SET
                    catalog_exposures=catalog_exposures+excluded.catalog_exposures,
                    usable_exposures=usable_exposures+excluded.usable_exposures""",
                    [(night, *values) for night, values in nights.items()],
                )
                con.commit()
            con.execute("CREATE INDEX observations_time ON observations(observationStartMJD)")
            con.execute("CREATE INDEX observations_night ON observations(observing_date)")
            metadata.update({"time_scale": "TAI", "export_counts": counts, "schema_version": 1})
            con.executemany(
                "INSERT INTO metadata VALUES (?,?)", [(k, json.dumps(v)) for k, v in metadata.items()]
            )
            con.commit()
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Pointing database integrity check failed")
        finally:
            con.close()
        os.replace(temporary, output)
        return counts
