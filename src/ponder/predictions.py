"""Photometry-independent Sorcha predictions with explicit orbit provenance."""

import gzip
import hashlib
import json
import logging
import math
import os
import re
import threading
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import ijson
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from astropy.time import Time, TimeDelta

from .decam_db import read_db
from .utils import keep_mpcorb_object

PERTURBERS = {1, 2, 3, 4, 7, 10, 15, 16, 31, 52, 65, 87, 88, 107, 511, 704}
EPHEMERIS_COLUMNS = [
    "ObjID",
    "FieldID",
    "fieldMJD_TAI",
    "fieldJD_TDB",
    "Range_LTC_km",
    "RangeRate_LTC_km_s",
    "RA_deg",
    "RARateCosDec_deg_day",
    "Dec_deg",
    "DecRate_deg_day",
    "Obj_Sun_x_LTC_km",
    "Obj_Sun_y_LTC_km",
    "Obj_Sun_z_LTC_km",
    "Obj_Sun_vx_LTC_km_s",
    "Obj_Sun_vy_LTC_km_s",
    "Obj_Sun_vz_LTC_km_s",
    "Obs_Sun_x_km",
    "Obs_Sun_y_km",
    "Obs_Sun_z_km",
    "Obs_Sun_vx_km_s",
    "Obs_Sun_vy_km_s",
    "Obs_Sun_vz_km_s",
    "phase_deg",
]
OUTPUT_SCHEMA = pa.schema(
    [(c, pa.string() if c in ("ObjID", "FieldID") else pa.float64()) for c in EPHEMERIS_COLUMNS]
    + [
        ("exposure_id", pa.int64()),
        ("observing_date", pa.string()),
        ("object_type", pa.string()),
        ("orbit_solution_id", pa.string()),
        ("object_designation", pa.string()),
        ("ponder_catalog_row", pa.int64()),
        ("filter", pa.string()),
        ("archive_filename", pa.string()),
        ("field_separation_deg", pa.float64()),
    ]
)
ORBIT_SCHEMA = pa.schema(
    [(x, pa.string()) for x in ("ObjID", "FORMAT", "object_type", "object_designation")]
    + [("ponder_catalog_row", pa.int64())]
    + [
        (x, pa.float64())
        for x in ("a", "e", "inc", "node", "argPeri", "ma", "q", "t_p_MJD_TDB", "epochMJD_TDB")
    ]
)

_SORCHA_LOCK = threading.Lock()


class _SafeTrigonometry:
    """Local NumPy proxy for the pinned Sorcha driver's direction-cosine roundoff.

    The upstream driver feeds un-clipped unit-vector dot products to arccos;
    1 + one ULP silently drops exact field-center matches. Do not alter NumPy
    globally or conceal direction cosines outside floating-point tolerance.
    """

    def __getattr__(self, name):
        return getattr(np, name)

    @staticmethod
    def arccos(value):
        if np.any(np.abs(value) > 1 + 1e-12):
            raise ValueError("Invalid direction cosine in Sorcha")
        return np.arccos(np.clip(value, -1, 1))


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as src:
        for block in iter(lambda: src.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w") as out:
        json.dump(value, out, indent=2, sort_keys=True, allow_nan=False)
        out.flush()
        os.fsync(out.fileno())
    temp.replace(path)


def catalog_rows(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rb") as src:
        yield from ijson.items(src, "item", use_float=True)


def _number(row, key):
    value = float(row[key])
    if not math.isfinite(value):
        raise ValueError("Nonfinite " + key)
    return value


@lru_cache(maxsize=4096)
def epoch_mjd(jd_tt):
    return float(Time(jd_tt, format="jd", scale="tt").tdb.mjd)


def _comet_time(row, prefix):
    # MPC comet calendar dates, including fractional perihelion days, are TT.
    keys = (
        ("Epoch_year", "Epoch_month", "Epoch_day")
        if prefix == "epoch"
        else ("Year_of_perihelion", "Month_of_perihelion", "Day_of_perihelion")
    )
    year, month = int(_number(row, keys[0])), int(_number(row, keys[1]))
    day = _number(row, keys[2])
    if not 1 <= day < 32:
        raise ValueError("Invalid calendar day")
    t = Time(f"{year:04d}-{month:02d}-{int(day):02d}", format="iso", scale="tt")
    return float((t + TimeDelta(day - int(day), format="jd")).tdb.mjd)


def convert_orbit(row, index, comet=False, filter_orbits=True):
    """Return a uniquely identified solution or an explicit exclusion reason."""
    designation = str(
        (row.get("Designation_and_name") or row.get("Provisional_packed_desig"))
        if comet
        else row.get("Principal_desig") or ""
    )
    if not comet and filter_orbits and not keep_mpcorb_object(row):
        return None, "default_orbit_filter"
    number = re.fullmatch(r"\(?\s*(\d+)\s*\)?", str(row.get("Number", "")))
    if not comet and number and int(number[1]) in PERTURBERS:
        return None, "unsupported_massive_perturber"
    try:
        if not designation or designation == "None":
            raise ValueError("Missing designation")
        result = dict(
            object_type="comet" if comet else "non_comet",
            object_designation=designation,
            ponder_catalog_row=index,
            FORMAT="COM" if comet else "KEP",
            e=_number(row, "e"),
            inc=_number(row, "i"),
            node=_number(row, "Node") % 360,
            argPeri=_number(row, "Peri") % 360,
        )
        if result["e"] < 0 or not 0 <= result["inc"] <= 180:
            raise ValueError("Invalid eccentricity or inclination")
        if comet:
            result.update(
                q=_number(row, "Perihelion_dist"),
                epochMJD_TDB=_comet_time(row, "epoch"),
                t_p_MJD_TDB=_comet_time(row, "peri"),
            )
            if result["q"] <= 0:
                raise ValueError("Nonpositive perihelion distance")
        else:
            result.update(
                a=_number(row, "a"), ma=_number(row, "M") % 360, epochMJD_TDB=epoch_mjd(_number(row, "Epoch"))
            )
            if result["a"] <= 0 or result["e"] >= 1:
                raise ValueError("Invalid elliptic KEP orbit")
        digest = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()[:16]
        result["ObjID"] = f"{'c' if comet else 'a'}{index:09d}_{digest}"
        return result, None
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        return None, "invalid_orbit: " + str(exc)


def prepare_orbits(catalog, output_dir, comet=False, chunk_size=250, filter_orbits=True):
    """Stream an MPC catalog into immutable, independently recoverable checkpoints."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks, pending, counts = [], [], {"input": 0, "accepted": 0, "excluded": 0}

    def flush():
        path = output_dir / f"orbits-{len(chunks):06d}.parquet"
        pq.write_table(pa.Table.from_pylist(pending, schema=ORBIT_SCHEMA), path, compression="zstd")
        chunks.append({"path": str(path.resolve()), "rows": len(pending), "sha256": file_hash(path)})
        pending.clear()

    with (output_dir / "excluded_orbits.jsonl").open("w") as out:
        for index, row in enumerate(catalog_rows(catalog)):
            counts["input"] += 1
            converted, reason = convert_orbit(row, index, comet, filter_orbits)
            if converted is None:
                counts["excluded"] += 1
                out.write(
                    json.dumps(
                        {
                            "ponder_catalog_row": index,
                            "reason": reason,
                            "designation": (
                                row.get("Designation_and_name") if comet else row.get("Principal_desig")
                            ),
                        }
                    )
                    + "\n"
                )
            else:
                counts["accepted"] += 1
                pending.append(converted)
                if len(pending) == chunk_size:
                    flush()
        if pending:
            flush()
    return chunks, counts


def sorcha_context(auxiliary, radius=1.3, buffer=0.2):
    # Use only ephemeris configuration: no fake depths, colours or Rubin filter aliases.
    from importlib.metadata import version
    from sorcha.utilities.sorchaConfigs import simulationConfigs, auxiliaryConfigs

    if version("sorcha") != "1.2.1":
        raise RuntimeError("Prediction adapter requires sorcha==1.2.1")
    args = SimpleNamespace(
        ar_data_file_path=str(auxiliary),
        loglevel=False,
        pplogger=logging.getLogger(__name__),
        output_ephemeris_file=None,
        outpath=None,
    )
    configs = SimpleNamespace(
        simulation=simulationConfigs(
            ar_ang_fov=radius,
            ar_fov_buffer=buffer,
            ar_picket=1,
            ar_obs_code="W84",
            ar_healpix_order=6,
            _ephemerides_type="ar",
        ),
        auxiliary=auxiliaryConfigs(),
    )
    return args, configs


def prepare_geometry(db, auxiliary, output):
    import spiceypy as spice
    from sorcha.ephemeris.simulation_setup import create_assist_ephemeris, furnish_spiceypy
    from sorcha.ephemeris.simulation_geometry import barycentricObservatoryRates, ra_dec2vec
    from sorcha.ephemeris.simulation_parsing import Observatory
    from sorcha.ephemeris.simulation_constants import AU_KM

    with read_db(db) as con:
        pointings = pd.read_sql_query(
            """SELECT observationId,
            observationStartMJD AS observationStartMJD_TAI, observationMidpointMJD_TAI,
            fieldRA AS fieldRA_deg, fieldDec AS fieldDec_deg, visitExposureTime,
            filter AS optFilter, observing_date, archive_filename FROM observations
            ORDER BY observationMidpointMJD_TAI, observationId""",
            con,
        )
    if pointings.empty:
        raise ValueError("No usable exposures")
    pointings["FieldID"] = pointings["observationId"].astype(str)
    args, configs = sorcha_context(auxiliary)
    ephem, _, _ = create_assist_ephemeris(args, configs.auxiliary)
    furnish_spiceypy(args, configs.auxiliary)
    try:
        observatories = Observatory(args, configs.auxiliary)
        vectors = ra_dec2vec(pointings.fieldRA_deg.to_numpy(), pointings.fieldDec_deg.to_numpy())
        pointings[["visit_vector_x", "visit_vector_y", "visit_vector_z"]] = vectors
        # Sorcha 1.2.1's helper formats JD to six decimal places before converting
        # to TDB (up to 43 ms lost). Preserve the archive midpoint precision.
        pointings["fieldJD_TDB"] = Time(
            pointings.observationMidpointMJD_TAI.to_numpy(), format="mjd", scale="tai"
        ).tdb.jd
        r_obs, v_obs, r_sun, v_sun = (np.empty((len(pointings), 3)) for _ in range(4))
        for i, jd in enumerate(pointings.fieldJD_TDB):
            r_obs[i], v_obs[i] = barycentricObservatoryRates(
                (jd - spice.j2000()) * 86400, "W84", observatories=observatories
            )
            sun = ephem.get_particle("Sun", jd - ephem.jd_ref)
            r_sun[i], v_sun[i] = (sun.x, sun.y, sun.z), (sun.vx, sun.vy, sun.vz)
        for name, array in (
            ("r_obs", r_obs / AU_KM),
            ("v_obs", v_obs * 86400 / AU_KM),
            ("r_sun", r_sun),
            ("v_sun", v_sun),
        ):
            pointings[[f"{name}_{axis}" for axis in "xyz"]] = array
    finally:
        spice.kclear()
    temp = Path(output).with_suffix(".tmp")
    pointings.to_parquet(temp, index=False, compression="zstd")
    temp.replace(output)


def separation(ra, dec, center_ra, center_dec):
    ra, dec, cra, cdec = map(np.deg2rad, (ra, dec, center_ra, center_dec))
    hav = np.sin((dec - cdec) / 2) ** 2 + np.cos(dec) * np.cos(cdec) * np.sin((ra - cra) / 2) ** 2
    return np.rad2deg(2 * np.arcsin(np.sqrt(np.clip(hav, 0, 1))))


def enrich_ephemeris(ephemeris, orbits, pointings, radius=1.3):
    if ephemeris.empty:
        return pa.Table.from_pylist([], schema=OUTPUT_SCHEMA)
    frame = ephemeris[EPHEMERIS_COLUMNS].copy()
    frame["ObjID"] = frame["ObjID"].astype(str)
    frame["FieldID"] = frame["FieldID"].astype(str)
    frame["exposure_id"] = pd.to_numeric(frame["FieldID"], errors="raise").astype("int64")
    frame = frame.merge(
        orbits[["ObjID", "object_type", "object_designation", "ponder_catalog_row"]],
        on="ObjID",
        how="left",
        validate="many_to_one",
        indicator=True,
    )
    if not frame.pop("_merge").eq("both").all():
        raise ValueError("Ephemeris contains unknown orbit identifiers")
    frame = frame.merge(
        pointings[
            [
                "observationId",
                "fieldRA_deg",
                "fieldDec_deg",
                "optFilter",
                "observing_date",
                "archive_filename",
                "observationMidpointMJD_TAI",
            ]
        ],
        left_on="exposure_id",
        right_on="observationId",
        how="left",
        validate="many_to_one",
        indicator=True,
    )
    if not frame.pop("_merge").eq("both").all():
        raise ValueError("Ephemeris contains unknown exposure identifiers")
    if not np.allclose(frame["fieldMJD_TAI"], frame["observationMidpointMJD_TAI"], rtol=0, atol=1e-9):
        raise ValueError("Ephemeris midpoint mismatch")
    if not np.isfinite(frame[["RA_deg", "Dec_deg"]].to_numpy()).all():
        raise ValueError("Nonfinite ephemeris coordinates")
    frame["field_separation_deg"] = separation(
        frame.RA_deg, frame.Dec_deg, frame.fieldRA_deg, frame.fieldDec_deg
    )
    frame = frame.loc[frame.field_separation_deg <= radius].copy()
    frame["orbit_solution_id"] = frame.ObjID
    frame["ObjID"] = frame.object_designation
    frame["filter"] = frame.optFilter
    if frame.duplicated(["exposure_id", "object_type", "orbit_solution_id"]).any():
        raise ValueError("Duplicate object/exposure/solution keys")
    return pa.Table.from_pandas(frame[OUTPUT_SCHEMA.names], schema=OUTPUT_SCHEMA, preserve_index=False)


def predict(orbits, pointings, auxiliary):
    from sorcha.ephemeris import simulation_driver

    args, configs = sorcha_context(auxiliary)
    # Sorcha joins orbits back into its returned frame. Keep provenance separate.
    orbital = orbits.drop(columns=["object_type", "object_designation", "ponder_catalog_row"])
    # Workers are separate processes. Serialize callers within any one process
    # while applying this narrowly scoped, version-pinned upstream workaround.
    with _SORCHA_LOCK:
        original_numpy = simulation_driver.np
        try:
            simulation_driver.np = _SafeTrigonometry()
            ephemeris = simulation_driver.create_ephemeris(orbital, pointings, args, configs)
        finally:
            simulation_driver.np = original_numpy
    return enrich_ephemeris(ephemeris, orbits, pointings)
