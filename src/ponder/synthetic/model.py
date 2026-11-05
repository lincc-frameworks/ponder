"""Adapter for the external Ōtautahi–Oxford sampler, without modifying its files.

The model supplies kinematics, not a luminosity distribution. All time arguments
in this adapter are MJD TDB; model angles are Astropy mean-ecliptic J2000 radians.
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import numpy as np
import pandas as pd
from astropy import units as u
from astropy.coordinates import BarycentricMeanEcliptic, SkyCoord
from astropy.time import Time

BCART_COLUMNS = ["ObjID", "FORMAT", "x", "y", "z", "xdot", "ydot", "zdot", "epochMJD_TDB"]
SOLAR_COLORS = {"u-r": 1.63, "g-r": 0.42, "i-r": -0.12, "z-r": -0.13, "y-r": -0.14}


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def source_manifest(repo):
    repo = Path(repo).resolve()
    files = {name: sha256(repo / name) for name in ("orbitSampler.py", "velDist.csv")}
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True)
    return {
        "path": str(repo),
        "git_head": result.stdout.strip() if result.returncode == 0 else None,
        "sha256": files,
    }


def invert_impact_cdf(model, indices, radius, duration):
    """Invert the authors' CDF on its full [0,1] support, without clipping tails."""
    A = model.mu / model.vinf_table[indices] ** 2
    target = np.random.uniform(size=len(indices))
    lo = np.zeros(len(indices))
    hi = np.ones(len(indices))
    for _ in range(56):
        mid = (lo + hi) / 2
        value = model.Bcdf(mid, A, radius, duration)
        if not np.isfinite(value).all():
            raise ValueError("Nonfinite impact-parameter CDF")
        lower = value < target
        lo, hi = np.where(lower, mid, lo), np.where(lower, hi, mid)
    x = (lo + hi) / 2
    residual = np.max(np.abs(model.Bcdf(x, A, radius, duration) - target), initial=0)
    if residual > 1e-7:
        raise ValueError(f"Impact-parameter CDF inversion residual {residual}")
    model._iso_cdf_max_residual = float(residual)
    return x * np.sqrt(radius**2 + 2 * A * radius)


def sample_model(repo, count, radius, start_mjd_tdb, end_mjd_tdb, seed, weight_column="ISO_weights"):
    """Return unrotated draws and provenance; temporarily changes cwd and NumPy RNG.

    Call from a single process, not concurrently from threads. Both globals are
    restored even on failure. The external Python model must be trusted code.
    """
    if isinstance(count, bool) or int(count) != count or count < 1:
        raise ValueError("count must be a positive integer")
    if not np.isfinite([radius, start_mjd_tdb, end_mjd_tdb]).all() or radius <= 0:
        raise ValueError("finite times and positive radius required")
    if end_mjd_tdb <= start_mjd_tdb:
        raise ValueError("end must follow start")
    repo = Path(repo).resolve()
    provenance = source_manifest(repo)
    old_cwd, old_rng = Path.cwd(), np.random.get_state()
    try:
        os.chdir(repo)
        spec = importlib.util.spec_from_file_location("_ponder_oo_sampler", repo / "orbitSampler.py")
        model = importlib.util.module_from_spec(spec)
        source = (repo / "orbitSampler.py").read_text()
        legacy = "ISO_weights /= ISO_weights"
        if source.count(legacy) > 1:
            raise ValueError("Ambiguous legacy weight initialization; inspect model revision")
        import_override = legacy in source
        # The legacy self-division both flattens weights and can fail on read-only
        # pandas arrays before we can apply the explicit weighting policy.
        source = source.replace(legacy, "ISO_weights = ISO_weights.copy()")
        exec(compile(source, str(repo / "orbitSampler.py"), "exec"), model.__dict__)
        weights = model.gaiadf[weight_column].to_numpy(dtype=float).copy()
        if not np.isfinite(weights).all() or (weights < 0).any() or weights.sum() <= 0:
            raise ValueError("velocity weights must be finite, nonnegative, and nonzero")
        model.ISO_weights = weights / weights.sum()
        model.drawBs = lambda indices, r, t: invert_impact_cdf(model, indices, r, t)
        np.random.seed(seed)
        draws = model.sampleOrbits(int(count), radius, start_mjd_tdb, end_mjd_tdb)
        volume = float(model.calc_number(radius, end_mjd_tdb - start_mjd_tdb, 1.0))
    finally:
        os.chdir(old_cwd)
        np.random.set_state(old_rng)
    settings = {
        "count": int(count),
        "radius_au": radius,
        "start_mjd_tdb": start_mjd_tdb,
        "end_mjd_tdb": end_mjd_tdb,
        "seed": int(seed),
        "weight_column": weight_column,
        "model_source": provenance,
    }
    identity_settings = {**settings, "model_source": {k: v for k, v in provenance.items() if k != "path"}}
    tag = hashlib.sha256(json.dumps(identity_settings, sort_keys=True).encode()).hexdigest()[:12]
    draws.insert(0, "ObjID", [f"oo_{tag}_{i:08d}" for i in range(len(draws))])
    draws["effective_volume_au3"] = volume / count
    metadata = {
        **settings,
        "mode": "unrotated_population",
        "model_mu_au3_day2": float(model.mu),
        "effective_volume_au3": volume,
        "cdf_max_residual": model._iso_cdf_max_residual,
        "import_copy_override": import_override,
        "overrides": [
            f"Normalize supplied {weight_column} instead of import-time weights.",
            "Bounded inversion of original Bcdf with full uniform [0,1) draws.",
        ],
        "interpretation": "Gaia source_id identifies a velocity-table row, not an ISO parent star. "
        "Effective volume needs an explicit number density for this H cohort; "
        "it is not a discovery count. Overrides do not establish a published-paper error.",
    }
    return draws, metadata


def model_states(draws, epoch_mjd_tdb, mu):
    """Evaluate elliptic-axis hyperbolic states in the model's ecliptic frame."""
    columns = ["sma", "ecc", "inc", "raan", "argp", "tau"]
    data = draws[columns].to_numpy(dtype=float)
    if not np.isfinite(data).all() or not np.isfinite([epoch_mjd_tdb, mu]).all() or mu <= 0:
        raise ValueError("finite orbital elements, epoch and positive mu required")
    aa = -draws.sma.to_numpy(dtype=float)
    ee = draws.ecc.to_numpy(dtype=float)
    if (aa <= 0).any() or (ee <= 1).any():
        raise ValueError("ISO adapter requires sma < 0 and ecc > 1")
    inc, node, arg = (draws[c].to_numpy(dtype=float) for c in ["inc", "raan", "argp"])
    P = np.column_stack(
        (
            np.cos(node) * np.cos(arg) - np.sin(node) * np.sin(arg) * np.cos(inc),
            np.sin(node) * np.cos(arg) + np.cos(node) * np.sin(arg) * np.cos(inc),
            np.sin(arg) * np.sin(inc),
        )
    )
    Q = np.column_stack(
        (
            -np.cos(node) * np.sin(arg) - np.sin(node) * np.cos(arg) * np.cos(inc),
            -np.sin(node) * np.sin(arg) + np.cos(node) * np.cos(arg) * np.cos(inc),
            np.cos(arg) * np.sin(inc),
        )
    )
    rate = np.sqrt(mu / aa**3)
    M = rate * (epoch_mjd_tdb - draws.tau.to_numpy(dtype=float))
    absm = np.abs(M)
    lo, hi = np.arcsinh(absm / ee), np.arcsinh(absm / (ee - 1))
    for _ in range(64):
        mid = (lo + hi) / 2
        lower = ee * np.sinh(mid) - mid < absm
        lo, hi = np.where(lower, mid, lo), np.where(lower, hi, mid)
    F = np.sign(M) * (lo + hi) / 2
    if not np.allclose(ee * np.sinh(F) - F, M, rtol=1e-11, atol=1e-12):
        raise ValueError("Hyperbolic Kepler inversion did not converge")
    fdot = rate / (ee * np.cosh(F) - 1)
    pos = (aa * (ee - np.cosh(F)))[:, None] * P + (aa * np.sqrt(ee**2 - 1) * np.sinh(F))[:, None] * Q
    vel = (-aa * np.sinh(F) * fdot)[:, None] * P + (aa * np.sqrt(ee**2 - 1) * np.cosh(F) * fdot)[:, None] * Q
    return pos, vel


def to_bcart(draws, epoch_mjd_tdb, mu):
    """Preserve the model ICRS state when converting to Sorcha's ecliptic axes."""
    from sorcha.ephemeris.simulation_geometry import equatorial_to_ecliptic

    pos, vel = model_states(draws, epoch_mjd_tdb, mu)
    xyz = np.eye(3)
    rotation = SkyCoord(
        x=xyz[:, 0] * u.au,
        y=xyz[:, 1] * u.au,
        z=xyz[:, 2] * u.au,
        representation_type="cartesian",
        frame=BarycentricMeanEcliptic(equinox=Time("J2000")),
    ).icrs.cartesian.xyz.value
    pos = equatorial_to_ecliptic(pos @ rotation.T)
    vel = equatorial_to_ecliptic(vel @ rotation.T)
    result = pd.DataFrame(np.column_stack((pos, vel)), columns=BCART_COLUMNS[2:8])
    result.insert(0, "FORMAT", "BCART")
    result.insert(0, "ObjID", draws.ObjID.to_numpy())
    result["epochMJD_TDB"] = epoch_mjd_tdb
    return result[BCART_COLUMNS]


def physical_parameters(ids, h_r, slope=0.15):
    if not np.isfinite([h_r, slope]).all() or not 0 <= slope <= 1:
        raise ValueError("finite H_r and HG slope in [0,1] required")
    if pd.Series(ids).isna().any() or pd.Series(ids).duplicated().any():
        raise ValueError("unique nonnull ObjIDs required")
    return pd.DataFrame({"ObjID": np.asarray(ids), "H_r": h_r, "GS": slope, **SOLAR_COLORS})
