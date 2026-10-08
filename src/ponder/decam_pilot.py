"""Compute-node validation against direct integrations without the HEALPix selection."""

import os
from pathlib import Path
import time

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from astropy.coordinates import SkyCoord
import astropy.units as u

from .predictions import (
    EPHEMERIS_COLUMNS,
    atomic_json,
    enrich_ephemeris,
    file_hash,
    predict,
    separation,
    sorcha_context,
)


def direct_ephemeris(orbits, pointings, auxiliary):
    import spiceypy as spice
    from sorcha.ephemeris.simulation_setup import (
        create_assist_ephemeris,
        furnish_spiceypy,
        generate_simulations,
    )
    from sorcha.ephemeris.simulation_driver import (
        EphemerisGeometryParameters,
        calculate_rates_and_geometry,
        get_vec,
    )
    from sorcha.ephemeris.simulation_geometry import integrate_light_time

    args, configs = sorcha_context(auxiliary)
    ephem, gm_sun, gm_total = create_assist_ephemeris(args, configs.auxiliary)
    furnish_spiceypy(args, configs.auxiliary)
    sims = generate_simulations(ephem, gm_sun, gm_total, orbits, args)
    rows = []
    try:
        for _, pointing in pointings.iterrows():
            for oid, simulation in sims.items():
                rho, mag, _, position, velocity = integrate_light_time(
                    simulation["sim"],
                    simulation["ex"],
                    pointing.fieldJD_TDB - ephem.jd_ref,
                    get_vec(pointing, "r_obs"),
                    lt0=0.01,
                )
                parameters = EphemerisGeometryParameters(
                    obj_id=oid,
                    mjd_tai=pointing.observationMidpointMJD_TAI,
                    rho=rho,
                    rho_hat=rho / mag,
                    rho_mag=mag,
                    r_ast=position,
                    v_ast=velocity,
                )
                rows.append(calculate_rates_and_geometry(pointing, parameters))
    finally:
        spice.kclear()
    return pd.DataFrame(rows, columns=EPHEMERIS_COLUMNS)


def pilot(run, manifest):
    if not os.environ.get("SLURM_JOB_ID"):
        raise RuntimeError("Run the pilot on a compute node")
    run = Path(run)
    started = time.monotonic()
    pointings = pd.read_parquet(run / "geometry.parquet")
    nights = sorted(pointings.observing_date.unique())
    selected_nights = [nights[i] for i in sorted({0, len(nights) // 2, len(nights) - 1})]
    samples = []
    for night in selected_nights:
        rows = pointings[pointings.observing_date == night]
        samples.append(rows.iloc[np.unique(np.linspace(0, len(rows) - 1, min(8, len(rows))).astype(int))])
    samples = pd.concat(samples).sort_values("observationMidpointMJD_TAI").reset_index(drop=True)
    orbit_samples = []
    for mode in ("comet", "non_comet"):
        chunks = [c for c in manifest["chunks"] if c["object_type"] == mode]
        for index in sorted({0, len(chunks) // 2, len(chunks) - 1}):
            if not chunks:
                continue
            rows = pd.read_parquet(chunks[index]["path"])
            # Include the smaller-perihelion solutions from several catalog regions.
            q = rows.q if mode == "comet" else rows.a * (1 - rows.e)
            orbit_samples.append(rows.loc[q.nsmallest(4).index])
    orbits = pd.concat(orbit_samples).drop_duplicates("ObjID").reset_index(drop=True)
    direct = direct_ephemeris(orbits, samples, manifest["auxiliary"])
    expected = enrich_ephemeris(direct, orbits, samples).to_pandas()
    actual = predict(orbits, samples, manifest["auxiliary"]).to_pandas()

    def compare(expected, actual):
        keys = ["exposure_id", "object_type", "orbit_solution_id"]
        merged = expected.merge(
            actual, on=keys, how="outer", suffixes=("_direct", "_indexed"), indicator=True
        )
        if not merged._merge.eq("both").all():
            raise RuntimeError("Direct integration and indexed search disagree on candidate keys")
        if len(merged):
            error = (
                separation(
                    merged.RA_deg_direct, merged.Dec_deg_direct, merged.RA_deg_indexed, merged.Dec_deg_indexed
                )
                * 3600
            )
            if max(error) > 0.1:
                raise RuntimeError(f"Direct/indexed position disagreement: {max(error)} arcsec")
            return float(max(error))
        return 0.0

    error = compare(expected, actual)
    # Explicit edge cases, including the fastest sampled apparent motion, ensure
    # validation is meaningful even if these sparse real fields have no matches.
    direct["speed"] = np.hypot(direct.RARateCosDec_deg_day, direct.DecRate_deg_day)
    direct = direct.merge(orbits[["ObjID", "object_type"]], on="ObjID", validate="many_to_one")
    targets = direct.sort_values("speed").groupby(["fieldMJD_TAI", "object_type"]).tail(1)
    targets = targets.iloc[np.unique(np.linspace(0, len(targets) - 1, min(6, len(targets))).astype(int))]
    synthetic = []
    for _, row in targets.iterrows():
        pointing = samples[samples.observationId == int(row.FieldID)].iloc[0]
        for distance in (0.0, 1.299, 1.301):
            copy = pointing.copy()
            center = SkyCoord(row.RA_deg * u.deg, row.Dec_deg * u.deg).directional_offset_by(
                90 * u.deg, distance * u.deg
            )
            copy["observationId"] = -(len(synthetic) + 1)
            copy["FieldID"] = str(copy.observationId)
            copy["fieldRA_deg"], copy["fieldDec_deg"] = center.ra.deg, center.dec.deg
            copy["visit_vector_x"], copy["visit_vector_y"], copy["visit_vector_z"] = (
                center.cartesian.xyz.value
            )
            synthetic.append(copy)
    synthetic = pd.DataFrame(synthetic).sort_values("observationMidpointMJD_TAI").reset_index(drop=True)
    expected_edges = enrich_ephemeris(
        direct_ephemeris(orbits, synthetic, manifest["auxiliary"]), orbits, synthetic
    ).to_pandas()
    actual_edges = predict(orbits, synthetic, manifest["auxiliary"]).to_pandas()
    error = max(error, compare(expected_edges, actual_edges))
    if len(actual_edges) < 2 * len(targets):
        raise RuntimeError("Synthetic field-center and inside-edge matches were not recovered")
    report = {
        "passed": True,
        "slurm_job_id": os.environ["SLURM_JOB_ID"],
        "manifest_sha256": file_hash(run / "manifest.json"),
        "selected_nights": selected_nights,
        "sampled_orbits": len(orbits),
        "real_matches": len(actual),
        "edge_matches": len(actual_edges),
        "max_position_error_arcsec": error,
        "max_sampled_rate_deg_day": float(direct.speed.max()),
        "elapsed_seconds": time.monotonic() - started,
    }
    atomic_json(run / "pilot.json", report)
    return report
