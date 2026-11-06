import configparser
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ponder import runner
from ponder.comet_photometry import (
    complex_parameters_path,
    configured_model,
    metadata_signature,
    resolve_model,
    model_metadata,
    write_physical_inputs,
)
from ponder.utils import comets_to_sorcha_inputs, diff_objects


def comet(name="A", **overrides):
    obj = dict(
        Designation_and_name=name,
        Provisional_packed_desig=None,
        Perihelion_dist=2.0,
        e=0.5,
        i=1.0,
        Peri=2.0,
        Node=3.0,
        Epoch_year=2026,
        Epoch_month=1,
        Epoch_day=1,
        Year_of_perihelion=2026,
        Month_of_perihelion=2,
        Day_of_perihelion=1.5,
        H=12.0,
        G=6.0,
    )
    obj.update(overrides)
    return obj


def config(tmp_path):
    path = tmp_path / "input.ini"
    path.write_text(
        "[PHASECURVES]\nphase_function=none\n[ACTIVITY]\ncomet_activity=none\n"
        "[LIGHTCURVE]\nlc_model=none\n[SATURATION]\nbright_limit=16\n"
    )
    return path


def test_conversion_retains_alignment_and_flags_missing_g():
    objects = [
        comet("bad", Epoch_year=None),
        comet("known"),
        comet("missing", G=None),
        comet("invalid", G="bad"),
        comet("infinite", G=np.inf),
        comet("unknown", H=None),
    ]
    orbits, phys = comets_to_sorcha_inputs(objects, [o["Designation_and_name"] for o in objects])
    assert (
        orbits.ObjID.tolist() == phys.ObjID.tolist() == ["known", "missing", "invalid", "infinite", "unknown"]
    )
    assert phys.comet_G.tolist() == [6, 4, 4, 4, 6]
    assert phys.comet_G_assumed.tolist() == [False, True, True, True, False]
    assert np.isnan(phys.H_r.iloc[-1])
    _, legacy = comets_to_sorcha_inputs(objects, ["known"], "legacy")
    assert "comet_G" not in legacy


def test_sidecars_for_parent_and_debug_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")
    objects = [comet(str(n), G=None if n == 2 else 6) for n in range(4)]
    orbits, phys = comets_to_sorcha_inputs(objects, list("0123"), "mpc-hm")
    parent = runner.plan_sorcha_chunks("new", "2026-10-31", 4, 2, "abcd")[1]
    for chunk in [parent, *runner.plan_debug_subchunks(parent, 1)]:
        runner.write_chunk_inputs(chunk, orbits, phys)
        ordinary = pd.read_csv(chunk.physparams_path, dtype={"ObjID": str})
        extra = pd.read_csv(complex_parameters_path(chunk.physparams_path), dtype={"ObjID": str})
        assert ordinary.ObjID.tolist() == extra.ObjID.tolist()
        assert "comet_G" not in ordinary
        assert extra.comet_G.tolist() == phys.iloc[chunk.row_start : chunk.row_end].comet_G.tolist()


def test_effective_config_and_conflicts(tmp_path):
    source = config(tmp_path)
    original = source.read_text()
    effective = runner.prepare_sorcha_config_for_mode(source, True, tmp_path)
    parsed = configparser.ConfigParser()
    parsed.read(effective)
    assert configured_model(effective) == "mpc-hm"
    assert parsed.getboolean("EXPERT", "brute_force")
    assert not parsed.has_option("SATURATION", "bright_limit")
    assert source.read_text() == original
    for section, key, value in [
        ("PHASECURVES", "phase_function", "HG"),
        ("ACTIVITY", "comet_activity", "lsst_comet"),
        ("LIGHTCURVE", "lc_model", "sinusoidal"),
    ]:
        parser = configparser.ConfigParser()
        parser.read_string(original)
        parser.set(section, key, value)
        with source.open("w") as handle:
            parser.write(handle)
        with pytest.raises(ValueError, match="conflicts"):
            runner.prepare_sorcha_config_for_mode(source, True, tmp_path, "mpc-hm")
    with pytest.raises(ValueError, match="requires --comet"):
        runner.prepare_sorcha_config_for_mode(source, False, tmp_path, "mpc-hm")


def test_photometry_hashes_and_state_do_not_reuse_legacy(tmp_path):
    objects = [comet()]
    baseline = runner.object_hashes(objects, include_photometry=True)
    assert diff_objects(objects, baseline, include_photometry=True) == ([], [], ["A"])
    for field in ["H", "G"]:
        changed = [comet(**{field: 5.0})]
        assert diff_objects(changed, baseline, include_photometry=True) == (
            [],
            ["A"],
            [],
        )
    assert runner.state_files_for_run(tmp_path / "db", True) != runner.state_files_for_run(
        tmp_path / "db", True, photometry_signature="abc"
    )


def test_sorcha_command_supplies_complex_parameters(tmp_path, monkeypatch):
    source = config(tmp_path)
    effective = runner.prepare_sorcha_config_for_mode(source, True, tmp_path, "mpc-hm")
    _, phys = comets_to_sorcha_inputs([comet()], ["A"], "mpc-hm")
    physical = tmp_path / "phys.csv"
    write_physical_inputs(phys, physical)
    commands = []

    class Process:
        def __init__(self, command, **kwargs):
            commands.append(command)

        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(runner.subprocess, "Popen", Process)
    runner.run_sorcha(
        tmp_path / "orbit.csv",
        physical,
        tmp_path / "out.csv",
        tmp_path / "db",
        effective,
    )
    assert commands[0][-2:] == ["--cp", str(complex_parameters_path(physical))]


@pytest.mark.parametrize("chunk_size", [5000, 0])
def test_runner_isolates_products_and_reprocesses_photometry_changes(tmp_path, monkeypatch, chunk_size):
    pytest.importorskip("sorcha_addons.activity.mpc_comet")
    # Real chunk writing, consolidation, and state cycle; only expensive Sorcha execution is substituted.
    import sqlite3

    monkeypatch.chdir(tmp_path)
    source = config(tmp_path)
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps([comet()]))
    db = tmp_path / "pointings.db"
    with sqlite3.connect(db) as con:
        con.execute(
            "CREATE TABLE observations (observationId INTEGER, observationStartMJD REAL, fieldRA REAL, fieldDec REAL)"
        )
        con.execute("INSERT INTO observations VALUES (1, 61000., 10., -5.)")
    calls = []

    def fake_sorcha(orbits, physparams, output, db, config, timeout=None):
        calls.append(output)
        assert pd.read_csv(complex_parameters_path(physparams)).comet_G.iloc[0] in (
            6,
            7,
        )
        pd.DataFrame({"ObjID": ["A"], "fieldMJD_TAI": [61000.0]}).to_csv(output, index=False)
        pd.DataFrame({"ObjID": ["A"], "fieldMJD_TAI": [61000.0]}).to_csv(
            output.with_name(output.stem + "_ew.csv"), index=False
        )

    monkeypatch.setattr(runner, "run_sorcha", fake_sorcha)
    base = dict(
        db_path=db,
        object_path=catalog,
        config_path=source,
        comet=True,
        chunk_size=chunk_size,
        work_dir=tmp_path / "work",
        results_dir=tmp_path / "results",
    )
    original_dirs = runner.WORK_DIR, runner.RESULTS_DIR
    runner.run_ponder(**base)
    assert len(calls) == 1
    assert "photometry" in calls[0].parts
    assert (runner.WORK_DIR, runner.RESULTS_DIR) == original_dirs
    runner.run_ponder(**base)
    assert len(calls) == 1
    catalog.write_text(json.dumps([comet(G=7)]))
    runner.run_ponder(**base)
    assert len(calls) == 2
    assert "updated" in str(calls[-1])
    assert not list((tmp_path / "results").glob("*.parquet"))
    metadata = list((tmp_path / "results").glob("photometry/*/photometry.json"))
    assert len(metadata) == 1
    assert json.loads(metadata[0].read_text())["missing_G"] == 4


def test_external_ephemeris_cli_comparison(tmp_path):
    """Use real Sorcha subprocesses, readers, photometry, and detection filtering."""
    pytest.importorskip("sorcha_addons.activity.mpc_comet")
    import os
    import sqlite3
    import subprocess
    import sys

    objects = [
        comet("A", H=20.0, G=4.0),
        comet("B", H=12.0, G=6.0),
        comet("C", H=None, G=None),
    ]
    orbits, phys = comets_to_sorcha_inputs(objects, ["A", "B", "C"], "mpc-hm")
    orbits.to_csv(tmp_path / "orbits.csv", index=False)
    write_physical_inputs(phys, tmp_path / "phys.csv")
    # Deliberately reverse auxiliary order: the actual Sorcha reader must join by ID.
    cp = complex_parameters_path(tmp_path / "phys.csv")
    pd.read_csv(cp).iloc[::-1].to_csv(cp, index=False)
    au = 149597870.7
    rows = []
    for obj in objects:
        row = dict(
            ObjID=obj["Designation_and_name"],
            FieldID=1,
            fieldMJD_TAI=61000.0,
            Range_LTC_km=np.sqrt(3) * au,
            RangeRate_LTC_km_s=0.0,
            RA_deg=10.0,
            RARateCosDec_deg_day=0.0,
            Dec_deg=-5.0,
            DecRate_deg_day=0.0,
            phase_deg=30.0,
        )
        for prefix, suffix in [
            ("Obj_Sun_", "_LTC_km"),
            ("Obj_Sun_v", "_LTC_km_s"),
            ("Obs_Sun_", "_km"),
            ("Obs_Sun_v", "_km_s"),
        ]:
            row.update({prefix + axis + suffix: 0.0 for axis in "xyz"})
        row["Obj_Sun_x_LTC_km"] = 2 * au
        row["Obs_Sun_x_km"], row["Obs_Sun_y_km"] = 0.5 * au, np.sqrt(3) / 2 * au
        rows.append(row)
    pd.DataFrame(rows).to_csv(tmp_path / "ephem.csv", index=False)
    with sqlite3.connect(tmp_path / "pointings.db") as con:
        pd.DataFrame(
            [
                dict(
                    observationId=1,
                    observationStartMJD=61000.0,
                    visitTime=30.0,
                    visitExposureTime=30.0,
                    band="r",
                    seeingFwhmGeom=0.7,
                    seeingFwhmEff=0.8,
                    fiveSigmaDepth=24.0,
                    fieldRA=10.0,
                    fieldDec=-5.0,
                    rotSkyPos=0.0,
                )
            ]
        ).to_sql("observations", con, index=False)
    parser = configparser.ConfigParser()
    parser.read(Path(__file__).parents[2] / "configs/dp1_comcam_circle_0p50.ini")
    parser.set("INPUT", "ephemerides_type", "external")
    parser["SIMULATION"] = {}
    parser.set("FOV", "camera_model", "none")
    parser.set("OUTPUT", "output_columns", "all")
    for section in ["FADINGFUNCTION", "LINKINGFILTER", "SATURATION"]:
        parser.remove_section(section)
    parser["EXPERT"] = dict(
        default_snr_cut="False",
        randomization_on="False",
        trailing_losses_on="False",
        vignetting_on="False",
    )
    baseline = tmp_path / "baseline.ini"
    with baseline.open("w") as handle:
        parser.write(handle)
    active = runner.prepare_sorcha_config_for_mode(baseline, True, tmp_path, "mpc-hm")
    parser.read(active)
    parser.set("EXPERT", "mag_limit", "24")
    cut = tmp_path / "cut-config.ini"
    with cut.open("w") as handle:
        parser.write(handle)
    env = dict(os.environ, SORCHA_SEED="42")
    # Keep source Ponder importable even if the parent test was invoked with a relative PYTHONPATH.
    env["PYTHONPATH"] = str(Path(__file__).parents[2] / "src") + os.pathsep + env.get("PYTHONPATH", "")
    outputs = {}
    for name, cfg in [("legacy", baseline), ("mpc_hm", active), ("cut", cut)]:
        command = [
            sys.executable,
            "-m",
            "ponder.sorcha_wrapper",
            "-c",
            str(cfg),
            "--ob",
            str(tmp_path / "orbits.csv"),
            "-p",
            str(tmp_path / "phys.csv"),
            "--cp",
            str(cp),
            "--pd",
            str(tmp_path / "pointings.db"),
            "--er",
            str(tmp_path / "ephem.csv"),
            "-o",
            str(tmp_path),
            "-t",
            name,
            "-f",
        ]
        proc = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        outputs[name] = pd.read_csv(tmp_path / f"{name}.csv").set_index("ObjID")
    legacy, active = outputs["legacy"], outputs["mpc_hm"]
    assert set(active.index) == {"A", "B", "C"}
    assert np.isnan(active.loc["C", "trailedSourceMagTrue"])
    assert bool(active.loc["C", "comet_G_assumed"])
    assert active.loc["A", "trailedSourceMagTrue"] - legacy.loc["A", "trailedSourceMagTrue"] == pytest.approx(
        5 * np.log10(2) - 2.5 * np.log10(0.4142), abs=1e-6
    )
    assert set(outputs["cut"].index) == {"B"}


def test_cli_explicit_model_is_forwarded_and_requires_comet(tmp_path, monkeypatch):
    import sys
    from ponder import __main__ as cli

    calls = []
    monkeypatch.setattr(cli, "run_ponder", lambda *args, **kwargs: calls.append(kwargs))
    args = [
        "ponder",
        "--comet-photometry",
        "mpc-hm",
        "--orbits",
        "orbits.json",
        "--db",
        "db.sqlite",
        "--config",
        "config.ini",
    ]
    monkeypatch.setattr(sys, "argv", args + ["--comet"])
    cli.main()
    assert calls[0]["comet_photometry"] == "mpc-hm"
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(ValueError, match="requires --comet"):
        cli.main()


@pytest.mark.parametrize(
    "population, options, expected",
    [
        (["--comet"], [], "mpc-hm"),
        ([], [], "legacy"),
        (["--neo"], [], "legacy"),
        (["--comet"], ["--comet-photometry", "legacy"], "legacy"),
    ],
)
def test_cli_default_follows_population(monkeypatch, population, options, expected):
    import sys
    from ponder import __main__ as cli

    calls = []
    monkeypatch.setattr(cli, "run_ponder", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(
        sys,
        "argv",
        ["ponder", "--orbits", "orbits.json", "--db", "db.sqlite", "--config", "config.ini"]
        + population
        + options,
    )
    cli.main()
    assert calls[0]["comet_photometry"] == expected


def test_python_model_defaults_and_explicit_override():
    assert resolve_model(None, comet=True) == "mpc-hm"
    assert resolve_model(None, comet=False) == "legacy"
    assert resolve_model("legacy", comet=True) == "legacy"
    with pytest.raises(ValueError, match="requires --comet"):
        resolve_model("mpc-hm", comet=False)


def test_missing_addon_fails_before_creating_run_outputs(tmp_path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "sorcha_addons.activity.mpc_comet", None)
    with pytest.raises(RuntimeError, match="Install the matching sorcha-addons checkout"):
        runner.run_ponder(
            tmp_path / "db",
            tmp_path / "catalog",
            tmp_path / "config",
            comet=True,
            work_dir=tmp_path / "work",
            results_dir=tmp_path / "results",
        )
    assert not (tmp_path / "work").exists()
    assert not (tmp_path / "results").exists()
