"""Explicit DEEP geometry backend using Sorcha 1.1.1's ASSIST/REBOUND API.

Sorcha's normal CLI only dispatches Rubin surveys. This backend calls the same
ephemeris generator with W84, real VR labels, and explicit TAI midpoints. It does
not run the Rubin postprocessing/photometric selection pipeline. Output rows
are predicted opportunities, never detections or a completeness estimate.
"""

import argparse
import importlib.metadata
import logging
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
from astropy.coordinates import SkyCoord
import astropy.units as u


def read_pointings(path, query):
    with sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True) as con:
        rows = pd.read_sql_query(query, con)
    if rows.observationId.duplicated().any():
        raise ValueError("Duplicate exposure IDs")
    rows = rows.rename(columns={"observationId": "FieldID"})
    rows["observationMidpointMJD_TAI"] = rows.observationStartMJD_TAI + rows.visitExposureTime / 172800
    if not np.isfinite(rows.observationMidpointMJD_TAI).all():
        raise ValueError("Invalid TAI midpoints")
    return rows.sort_values("observationMidpointMJD_TAI").reset_index(drop=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ["-c", "--ob", "-p", "--pd", "-o", "--ew", "-t"]:
        parser.add_argument(arg, required=True)
    parser.add_argument("-f", action="store_true")
    a = parser.parse_args()
    version = importlib.metadata.version("sorcha")
    if version != "1.1.1":
        raise RuntimeError(f"Validated DEEP backend requires Sorcha 1.1.1, got {version}")
    from sorcha.utilities.sorchaConfigs import sorchaConfigs
    from sorcha.ephemeris.simulation_setup import precompute_pointing_information
    from sorcha.ephemeris.simulation_driver import create_ephemeris

    output = Path(a.o)
    output.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, filename=output / (a.t + "_geometry.log"))
    logger = logging.getLogger(__name__)
    config = sorchaConfigs(a.c, "DEEP")
    if config.simulation.ar_obs_code != "W84" or config.filters.observing_filters != ["VR"]:
        raise ValueError("DEEP backend requires W84 and VR")
    if config.input.ephemerides_type != "ar" or config.fov.camera_model != "circle":
        raise ValueError("DEEP backend requires ASSIST/REBOUND and a circular candidate cone")
    # Private cache prevents Sorcha from changing a shared meta-kernel file.
    cache = os.environ.get("PONDER_SORCHA_CACHE")
    if not cache:
        raise ValueError("Set PONDER_SORCHA_CACHE to a private kernel cache directory")
    args = SimpleNamespace(
        pplogger=logger,
        loglevel=True,
        ar_data_file_path=cache,
        output_ephemeris_file=None,
        outpath=str(output),
    )
    pointings = read_pointings(a.pd, config.input.pointing_sql_query)
    if set(pointings.optFilter) != {"VR"}:
        raise ValueError("Pointing database must contain only VR for this backend")
    orbits = pd.read_csv(a.ob, dtype={"ObjID": str})
    physical = pd.read_csv(a.p, dtype={"ObjID": str})
    if "H_VR" not in physical or physical.ObjID.duplicated().any():
        raise ValueError("Unique measured H_VR physical parameters are required")
    orbits = orbits.merge(physical, on="ObjID", how="left", validate="one_to_one")
    if orbits.H_VR.isna().any():
        raise ValueError("Missing DEEP H_VR")
    prepared = precompute_pointing_information(pointings.copy(), args, config)
    predictions = create_ephemeris(orbits, prepared, args, config)
    raw_columns = [c for c in predictions if c not in set(orbits.columns) - {"ObjID"}]
    raw = predictions[raw_columns].copy()
    raw.to_csv(output / (a.ew + ".csv"), index=False)
    joined = raw.merge(
        pointings[["FieldID", "fieldRA_deg", "fieldDec_deg", "optFilter"]],
        on="FieldID",
        validate="many_to_one",
    )
    source = SkyCoord(joined.RA_deg.to_numpy() * u.deg, joined.Dec_deg.to_numpy() * u.deg)
    center = SkyCoord(joined.fieldRA_deg.to_numpy() * u.deg, joined.fieldDec_deg.to_numpy() * u.deg)
    joined["field_separation_deg"] = source.separation(center).deg
    selected = joined.loc[joined.field_separation_deg <= config.fov.circle_radius]
    selected.to_csv(output / (a.t + ".csv"), index=False)
    print(
        f"DEEP geometry: {len(orbits)} objects, {len(pointings)} visits, "
        f"{len(raw)} buffered predictions, {len(selected)} circle opportunities",
        flush=True,
    )


if __name__ == "__main__":
    main()
