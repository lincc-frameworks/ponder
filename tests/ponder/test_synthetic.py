"""Scientific-contract regressions for the opt-in synthetic path."""

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from astropy.coordinates import BarycentricMeanEcliptic, SkyCoord
from astropy import units as u
from astropy.time import Time

from ponder.synthetic.model import BCART_COLUMNS, model_states, physical_parameters, sample_model, to_bcart
from ponder.synthetic.truth import geometry_candidates, propagate_truth, validate_inputs
from ponder.synthetic.__main__ import main, read_bundle, write_bundle

MU = 4 * np.pi**2 / 365.256363**2


@pytest.fixture
def draws():
    return pd.DataFrame(
        {
            "ObjID": ["inbound", "outbound", "almost_parabolic"],
            "sma": [-2.0, -8.0, -10000.0],
            "ecc": [2.4, 1.3, 1.0001],
            "inc": [0.3, 1.1, 2.2],
            "raan": [2.0, 0.2, 3.0],
            "argp": [1.0, 2.0, 3.0],
            "tau": [61100.0, 60000.0, 60800.0],
        }
    )


@pytest.mark.parametrize("epoch", [60000.0, 60800.0, 61500.0])
def test_hyperbolic_state_against_independent_universal_solver(draws, epoch):
    from sorcha.ephemeris.orbit_conversion_utilities import universal_cartesian

    pos, vel = model_states(draws, epoch, MU)
    for i, row in draws.iterrows():
        expected = universal_cartesian(
            MU, row.sma * (1 - row.ecc), row.ecc, row.inc, row.raan, row.argp, row.tau, epoch
        )
        np.testing.assert_allclose(np.r_[pos[i], vel[i]], expected, atol=2e-8, rtol=1e-9)
    energy = np.sum(vel**2, axis=1) / 2 - MU / np.linalg.norm(pos, axis=1)
    np.testing.assert_allclose(energy, -MU / (2 * draws.sma), rtol=1e-8, atol=1e-14)


def test_bcart_preserves_icrs_state_not_just_ecliptic_numbers(draws):
    from sorcha.ephemeris.simulation_geometry import ecliptic_to_equatorial

    pos, vel = model_states(draws, 60800.0, MU)
    converted = to_bcart(draws, 60800.0, MU)
    assert converted.columns.tolist() == BCART_COLUMNS
    for state, columns in [(pos, ["x", "y", "z"]), (vel, ["xdot", "ydot", "zdot"])]:
        expected = SkyCoord(
            x=state[:, 0] * u.au,
            y=state[:, 1] * u.au,
            z=state[:, 2] * u.au,
            representation_type="cartesian",
            frame=BarycentricMeanEcliptic(equinox=Time("J2000")),
        ).icrs.cartesian.xyz.value.T
        np.testing.assert_allclose(
            ecliptic_to_equatorial(converted[columns].to_numpy()), expected, rtol=1e-13, atol=1e-13
        )
    assert not np.array_equal(pos, converted[["x", "y", "z"]].to_numpy())


@pytest.mark.parametrize("column,value", [("ecc", 1.0), ("sma", 2.0), ("inc", np.nan)])
def test_reject_invalid_orbits(draws, column, value):
    draws.loc[0, column] = value
    with pytest.raises(ValueError):
        to_bcart(draws, 60800.0, MU)


@pytest.fixture
def inputs(draws):
    orbits = to_bcart(draws.iloc[:2], 60800.0, MU)
    phys = physical_parameters(orbits.ObjID, 16.0)
    pts = pd.DataFrame(
        {
            "observationId": ["visit-a", "visit-b"],
            "exp_midpt_mjd": [60800.0, 60800.1],
            "fieldRA": [150.0, 150.0],
            "fieldDec": [2.0, 2.0],
            "band": ["r", "i"],
            "visitExposureTime": [30.0, 30.0],
            "observationStartMJD": [100.0, 100.0],
        }
    )
    return orbits, phys, pts


def test_join_by_identity_and_reject_missing_physics(inputs):
    o, p, v = inputs
    validate_inputs(o, p.iloc[::-1], v)
    with pytest.raises(ValueError, match="sets must match"):
        validate_inputs(o, p.iloc[:1], v)


@pytest.mark.parametrize("frame,key", [(0, "ObjID"), (1, "ObjID"), (2, "observationId")])
def test_reject_duplicate_identity(inputs, frame, key):
    frames = [x.copy() for x in inputs]
    frames[frame].loc[1, key] = frames[frame].loc[0, key]
    with pytest.raises(ValueError, match="unique"):
        validate_inputs(*frames)


def test_require_measured_midpoints(inputs):
    o, p, v = inputs
    with pytest.raises(KeyError):
        validate_inputs(o, p, v.drop(columns="exp_midpt_mjd"))


def test_geometry_candidate_selection_never_cuts_on_brightness():
    frame = pd.DataFrame(
        {
            "ObjID": ["bright", "faint", "outside"],
            "inside_field_circle": [True, True, False],
            "RA_deg": [150.0, 150.0, 180.0],
            "Dec_deg": [2.0, 2.0, 2.0],
            "trailedSourceMagTrue": [24.0, 40.0, 20.0],
            "trail_length_arcsec": [0.0, 20.0, 0.0],
        }
    )
    candidates = geometry_candidates(frame)
    assert candidates.ObjID.tolist() == ["bright", "faint"]
    np.testing.assert_allclose(candidates.flux_nJy, 10 ** ((31.4 - np.array([24.0, 40.0])) / 2.5))
    assert candidates.requires_detector_join.all()


def test_pair_budget_before_loading_kernels(inputs):
    with pytest.raises(ValueError, match="pairs exceed"):
        propagate_truth(*inputs, "missing", max_pairs=3)


def test_planetary_only_requires_explicit_flag(inputs):
    with pytest.raises(ValueError, match="explicitly allow"):
        propagate_truth(*inputs, "missing")


def test_checksum_protects_brightness_and_orbits(inputs, draws, tmp_path):
    o, p, _ = inputs
    write_bundle(tmp_path, o, p, draws.iloc[:2], {"mode": "test"})
    read_bundle(tmp_path)
    p.loc[0, "H_r"] = 5.0
    p.to_csv(tmp_path / "physical.csv", index=False)
    with pytest.raises(ValueError, match="checksum"):
        read_bundle(tmp_path)


def test_selection_keeps_ids_and_physics_together(inputs, draws, tmp_path):
    o, p, _ = inputs
    parent = tmp_path / "parent"
    parent.mkdir()
    write_bundle(
        parent,
        o,
        p.iloc[::-1],
        draws.iloc[:2],
        {"mode": "unrotated_population", "population_draw_count": 2, "effective_volume_au3": 10.0},
    )
    pd.DataFrame({"ObjID": ["outbound"]}).to_csv(tmp_path / "ids.csv", index=False)
    main(
        [
            "select",
            "--bundle",
            str(parent),
            "--ids",
            str(tmp_path / "ids.csv"),
            "--reason",
            "test selection",
            "--output",
            str(tmp_path / "selected"),
        ]
    )
    oo, pp, dd, meta = read_bundle(tmp_path / "selected")
    assert oo.ObjID.tolist() == pp.ObjID.tolist() == dd.ObjID.tolist() == ["outbound"]
    assert meta["population_draw_count"] == 2
    assert meta["selection_completeness"].startswith("not established")
    with pytest.raises(FileExistsError):
        main(
            [
                "select",
                "--bundle",
                str(parent),
                "--ids",
                str(tmp_path / "ids.csv"),
                "--reason",
                "retry",
                "--output",
                str(tmp_path / "selected"),
            ]
        )


def test_actual_model_repeatable_and_globals_restored():
    path = os.environ.get("PONDER_OO_MODEL")
    if not path:
        pytest.skip("Set PONDER_OO_MODEL to the pinned external sampler checkout")
    cwd = Path.cwd()
    np.random.seed(199)
    before = np.random.get_state()
    a, ma = sample_model(path, 128, 10.0, 60800.0, 60900.0, 91)
    b, mb = sample_model(path, 128, 10.0, 60800.0, 60900.0, 91)
    pd.testing.assert_frame_equal(a, b)
    after = np.random.get_state()
    assert Path.cwd() == cwd
    np.testing.assert_array_equal(before[1], after[1])
    assert before[2:] == after[2:]
    assert ma["cdf_max_residual"] < 1e-7
    assert (a.ecc > 1).all()
    np.testing.assert_allclose(a.effective_volume_au3.sum(), ma["effective_volume_au3"])
    assert ma == mb


def test_actual_sorcha_truth_keeps_faint_objects_and_exact_times(inputs):
    kernel = os.environ.get("PONDER_PLANETS_KERNEL")
    if not kernel:
        pytest.skip("Set PONDER_PLANETS_KERNEL for real ASSIST propagation")
    o, p, v = inputs
    p.loc[1, "H_r"] = 40.0
    v["exp_midpt_mjd"] = 60800.0
    a, meta = propagate_truth(o, p.iloc[::-1], v, kernel, allow_planetary_only=True, field_radius_deg=180)
    assert len(a) == 4 and meta["pairs"] == 4
    assert set(a.ObjID) == set(o.ObjID)
    assert a.loc[a.ObjID == "outbound", "trailedSourceMagTrue"].min() > 40
    np.testing.assert_allclose(a.fieldMJD_TAI, a.exp_midpt_mjd, atol=0, rtol=0)
    assert meta["brightness_selection"] == "none"
    # The intentionally absurd nominal start was ignored, not added to half the exposure.
    assert a.fieldMJD_TAI.min() > 60000
    assert np.isfinite(a[["RA_deg", "Dec_deg", "trailedSourceMagTrue"]]).all().all()
    assert len(geometry_candidates(a)) == 4
    for _, obj in a.groupby("ObjID"):
        mags = obj.set_index("optFilter").trailedSourceMagTrue
        assert mags["i"] - mags["r"] == pytest.approx(-0.12, abs=1e-10)
