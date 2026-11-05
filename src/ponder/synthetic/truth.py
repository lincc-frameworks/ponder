"""Bounded all-pairs validation backend using Sorcha before detection filtering.

This deliberately avoids faint-object cuts, stochastic selection, and HEALPix
pickets. It is a correctness pilot, not the scalable production Sorcha CLI.
"""

import logging
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
from astropy import units as u
from astropy.coordinates import EarthLocation
from astropy.time import Time
from astropy.utils import iers

from .model import BCART_COLUMNS, SOLAR_COLORS

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
POINTING_COLUMNS = ["observationId", "exp_midpt_mjd", "fieldRA", "fieldDec", "band", "visitExposureTime"]


def validate_inputs(orbits, physical, pointings):
    """Fail on ambiguous IDs, units/schema, or missing physical counterparts."""
    if len(orbits) == 0 or len(pointings) == 0:
        raise ValueError("at least one orbit and pointing required")
    if set(orbits.columns) != set(BCART_COLUMNS) or not (orbits.FORMAT == "BCART").all():
        raise ValueError("exact nine-column BCART orbit schema required")
    for frame, key in [(orbits, "ObjID"), (physical, "ObjID"), (pointings, "observationId")]:
        if frame[key].isna().any() or frame[key].astype(str).duplicated().any():
            raise ValueError(f"unique nonnull {key} required")
        if frame[key].astype(str).str.strip().eq("").any():
            raise ValueError(f"blank {key}")
    if set(orbits.ObjID.astype(str)) != set(physical.ObjID.astype(str)):
        raise ValueError("orbit and physical ObjID sets must match exactly")
    required_phys = ["H_r", "GS", *SOLAR_COLORS]
    if not np.isfinite(physical[required_phys].to_numpy(dtype=float)).all():
        raise ValueError("finite physical parameters required")
    if not physical.GS.between(0, 1).all():
        raise ValueError("HG GS must be in [0,1]")
    if not np.isfinite(orbits[BCART_COLUMNS[2:]].to_numpy(dtype=float)).all():
        raise ValueError("finite BCART values required")
    numeric = ["exp_midpt_mjd", "fieldRA", "fieldDec", "visitExposureTime"]
    if not np.isfinite(pointings[numeric].to_numpy(dtype=float)).all():
        raise ValueError("finite measured TAI midpoints and pointing geometry required")
    if (pointings.visitExposureTime <= 0).any() or not pointings.fieldDec.between(-90, 90).all():
        raise ValueError("positive exposure time and valid declination required")
    if not pointings.fieldRA.between(0, 360, inclusive="left").all():
        raise ValueError("fieldRA must lie in [0,360) degrees")
    if not pointings.band.isin(list("ugrizy")).all():
        raise ValueError("only Rubin ugrizy passbands are currently supported")


def geometry_candidates(truth):
    """Noiseless flux candidates, not detector-ready painting catalogs.

    Point-source total flux is retained even for trailed objects: trailing losses
    belong in detection sensitivity, never as a reduction of painted total flux.
    """
    keep = truth.loc[truth.inside_field_circle].copy()
    keep["ra"] = keep.RA_deg
    keep["dec"] = keep.Dec_deg
    keep["mag"] = keep.trailedSourceMagTrue
    keep["flux_nJy"] = 10 ** ((31.4 - keep.mag) / 2.5)
    keep["requires_detector_join"] = True
    return keep


def propagate_truth(
    orbits,
    physical,
    pointings,
    planets_path,
    asteroids_path=None,
    allow_planetary_only=False,
    field_radius_deg=1.75,
    max_pairs=1_000_000,
    longitude_deg=-70.74941,
    latitude_deg=-30.244628,
    height_m=2650,
):
    """Return every supplied object/visit pair, regardless of brightness or FoV.

    `exp_midpt_mjd` is the measured MJD TAI, never nominal start + duration/2.
    Geometric topocentric light-time RA/Dec is in inertial equatorial axes.
    The site offset uses Astropy GCRS; qualification against the full Sorcha SPICE
    observer adapter is still required before subpixel production injection.
    """
    validate_inputs(orbits, physical, pointings)
    pairs = len(orbits) * len(pointings)
    if pairs > max_pairs or max_pairs < 1:
        raise ValueError(f"{pairs} pairs exceed explicit max_pairs={max_pairs}; select a bounded pilot")
    if not 0 < field_radius_deg <= 180 or not np.isfinite(field_radius_deg):
        raise ValueError("field radius must be in (0,180] degrees")
    if asteroids_path is None and not allow_planetary_only:
        raise ValueError("asteroid kernel required; explicitly allow planetary-only for a pilot")
    for path in [planets_path] + ([asteroids_path] if asteroids_path is not None else []):
        if not Path(path).is_file():
            raise FileNotFoundError(path)
    import assist
    from sorcha.ephemeris.simulation_setup import generate_simulations
    from sorcha.ephemeris.simulation_geometry import integrate_light_time
    from sorcha.ephemeris.simulation_driver import EphemerisGeometryParameters, calculate_rates_and_geometry
    from sorcha.modules.PPCalculateApparentMagnitude import PPCalculateApparentMagnitude

    kwargs = {"planets_path": str(planets_path)}
    if asteroids_path is not None:
        kwargs["asteroids_path"] = str(asteroids_path)
    ephem = assist.Ephem(**kwargs)
    gm_sun = ephem.get_particle(0, 0).m
    gm_total = sum(ephem.get_particle(i, 0).m for i in range(27 if asteroids_path else 11))
    args = SimpleNamespace(pplogger=logging.getLogger(__name__))
    pointings = pointings.sort_values(["exp_midpt_mjd", "observationId"]).reset_index(drop=True).copy()
    pointings["observationId"] = pointings.observationId.astype(str)
    times = Time(pointings.exp_midpt_mjd.to_numpy(), format="mjd", scale="tai")
    location = EarthLocation.from_geodetic(longitude_deg * u.deg, latitude_deg * u.deg, height_m * u.m)
    with iers.conf.set_temp("auto_download", False):
        pos, vel = location.get_gcrs_posvel(times)
    offset, voffset = pos.xyz.to_value(u.au).T, vel.xyz.to_value(u.au / u.day).T
    epochs = times.tdb.jd - ephem.jd_ref
    cache = []
    for i, row in pointings.iterrows():
        earth = ephem.get_particle(3, epochs[i])
        sun = ephem.get_particle(0, epochs[i])
        p = {"FieldID": str(row.observationId), "fieldJD_TDB": float(times.tdb.jd[i])}
        for prefix, values in [
            ("r_obs", np.array(earth.xyz) + offset[i]),
            ("v_obs", np.array(earth.vxyz) + voffset[i]),
            ("r_sun", sun.xyz),
            ("v_sun", sun.vxyz),
        ]:
            p.update({f"{prefix}_{axis}": float(value) for axis, value in zip("xyz", values)})
        cache.append(p)
    rows = []
    for _, orbit in orbits.iterrows():
        simdata = generate_simulations(ephem, gm_sun, gm_total, pd.DataFrame([orbit]), args)[orbit.ObjID]
        for i, p in enumerate(cache):
            observer = np.array([p[f"r_obs_{axis}"] for axis in "xyz"])
            rho, distance, _, target, velocity = integrate_light_time(
                simdata["sim"], simdata["ex"], float(epochs[i]), observer, iter=5
            )
            geo = EphemerisGeometryParameters(
                obj_id=str(orbit.ObjID),
                mjd_tai=float(times.tai.mjd[i]),
                rho=rho,
                rho_hat=rho / distance,
                rho_mag=distance,
                r_ast=target,
                v_ast=velocity,
            )
            rows.append(calculate_rates_and_geometry(p, geo))
    result = pd.DataFrame(rows, columns=EPHEMERIS_COLUMNS)
    pointings = pointings.rename(columns={"observationId": "FieldID", "band": "optFilter"})
    result = result.merge(pointings, on="FieldID", validate="many_to_one")
    physical = physical.copy()
    physical["ObjID"] = physical.ObjID.astype(str)
    result = result.merge(physical, on="ObjID", validate="many_to_one")
    # Supply all configured filters to apply r-referenced colors even for i-only visits.
    result = PPCalculateApparentMagnitude(result, "HG", "r", list(SOLAR_COLORS), list("ugrizy"))
    ra, dec, bra, bdec = (
        np.radians(result[x].to_numpy()) for x in ["RA_deg", "Dec_deg", "fieldRA", "fieldDec"]
    )
    dot = np.sin(dec) * np.sin(bdec) + np.cos(dec) * np.cos(bdec) * np.cos(ra - bra)
    result["field_separation_deg"] = np.degrees(np.arccos(np.clip(dot, -1, 1)))
    result["inside_field_circle"] = result.field_separation_deg <= field_radius_deg
    rate = np.hypot(result.RARateCosDec_deg_day, result.DecRate_deg_day)
    result["trail_length_arcsec"] = rate * result.visitExposureTime / 24.0
    result["trail_pa_deg_east_of_north"] = (
        np.degrees(np.arctan2(result.RARateCosDec_deg_day, result.DecRate_deg_day)) % 360
    )
    result["trailedSourceMag"] = result.trailedSourceMagTrue  # noiseless, deliberately no randomization
    if len(result) != pairs or result[["ObjID", "FieldID"]].duplicated().any():
        raise RuntimeError("Lost or duplicated object/visit truth rows")
    metadata = {
        "pairs": pairs,
        "objects": len(orbits),
        "visits": len(pointings),
        "field_circle_rows": int(result.inside_field_circle.sum()),
        "field_circle_objects": int(result.loc[result.inside_field_circle, "ObjID"].nunique()),
        "brightness_selection": "none",
        "detector_coverage": "not applied",
        "photometry": "Sorcha HG with explicit H_r, GS, and colors; no activity or randomization",
        "observer": {
            "adapter": "Astropy GCRS offset + ASSIST Earth",
            "longitude_deg": longitude_deg,
            "latitude_deg": latitude_deg,
            "height_m": height_m,
        },
        "dynamics": "Sorcha generate_simulations, integrate_light_time (5 iterations), "
        "calculate_rates_and_geometry; not full Sorcha CLI",
        "planetary_only": asteroids_path is None,
        "limitations": [
            "Observer adapter not independently qualified against production SPICE",
            "No detector/WCS/mask/PSF join, image painting, or search recovery",
            "No population occurrence prediction",
            "N-body initialization at finite epoch; no matching at asymptotic infinity",
        ],
    }
    return result, metadata
