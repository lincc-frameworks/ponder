"""Catalog and configuration plumbing for Sorcha comet photometry.

The magnitude calculation lives in sorcha-addons, not in Ponder.
"""

import configparser
import hashlib
import inspect
import json
from importlib.metadata import version
from pathlib import Path

import numpy as np
import pandas as pd

COMET_PHOTOMETRY_CHOICES = ("legacy", "mpc-hm")
COMET_COMPLEX_COLUMNS = ("comet_G", "comet_G_assumed")


def validate_model(model, comet=True):
    if model not in COMET_PHOTOMETRY_CHOICES:
        raise ValueError(f"Unknown comet photometry model: {model}")
    if model != "legacy" and not comet:
        raise ValueError("--comet-photometry mpc-hm requires --comet")


def resolve_model(model, comet):
    """Default comet runs to MPC/Halley–Marcus, leaving asteroid runs unchanged."""
    if model is None:
        model = "mpc-hm" if comet else "legacy"
    validate_model(model, comet)
    return model


def comet_slopes(catalog):
    """Return catalog G and an explicit flag for the G=4 fallback."""
    slope = pd.to_numeric(catalog.reindex(columns=["G"])["G"], errors="coerce")
    assumed = ~np.isfinite(slope)
    return slope.mask(assumed, 4.0), assumed


def model_metadata():
    """Fail early if the selected add-on is absent; fingerprint its implementation."""
    try:
        from sorcha_addons.activity.mpc_comet import mpc_comet_activity as model
    except ImportError as exc:
        raise RuntimeError(
            "mpc-hm requires sorcha-addons with the mpc_comet activity model. "
            "Install the matching sorcha-addons checkout in this environment, or explicitly "
            "select --comet-photometry legacy to use the old distance-only law."
        ) from exc
    from sorcha_addons import __version__ as addons_version

    from .utils import SORCHA_DEFAULT_COLOR_OFFSETS

    return {
        "ponder_photometry_version": 1,
        "sorcha_addons_version": addons_version,
        "color_offsets": dict(SORCHA_DEFAULT_COLOR_OFFSETS),
        "model": "mpc-hm",
        "model_version": model.MODEL_VERSION,
        "model_sha256": hashlib.sha256(inspect.getsource(model).encode()).hexdigest(),
        "sorcha_version": version("sorcha"),
        "sbpy_version": version("sbpy"),
        "scipy_version": version("scipy"),
        "missing_G": 4.0,
        "H_band_assumption": "MPC H treated as H_r; existing fixed colors",
    }


def metadata_signature(metadata):
    return hashlib.sha256(json.dumps(metadata, sort_keys=True).encode()).hexdigest()[:12]


def configured_model(path):
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(path):
        raise FileNotFoundError(path)
    activity = parser.get("ACTIVITY", "comet_activity", fallback="none").strip().lower()
    return "mpc-hm" if activity == "mpc_hm" else "legacy"


def complex_parameters_path(physical_path):
    path = Path(physical_path)
    return path.with_name(path.stem + "_complex.csv")


def write_physical_inputs(phys, path):
    """Split paired physical/complex inputs after each chunk has been sliced."""
    if "comet_G" in phys:
        columns = ["ObjID", *COMET_COMPLEX_COLUMNS]
        phys[columns].to_csv(complex_parameters_path(path), index=False)
        phys = phys.drop(columns=list(COMET_COMPLEX_COLUMNS))
    phys.to_csv(path, index=False)
