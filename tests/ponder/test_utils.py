import gzip

import pandas as pd

from ponder.utils import (
    SORCHA_DEFAULT_COLOR_OFFSETS,
    SORCHA_DEFAULT_GS,
    comets_to_sorcha_inputs,
    filter_orbit_objects,
    format_rubin_filter_list,
    get_current_orbits,
    keep_mpcorb_object,
    mpcorb_observation_arc_at_least,
    mpcorb_to_sorcha_inputs,
    rubin_band,
)


def test_mpcorb_filter_keeps_distant_objects_with_short_arcs():
    obj = {"a": 31, "U": "9", "Arc_length": 1}

    assert keep_mpcorb_object(obj)


def test_mpcorb_filter_keeps_low_uncertainty_objects_with_short_arcs():
    obj = {"a": 2.5, "U": "6", "Arc_length": 1}

    assert keep_mpcorb_object(obj)


def test_mpcorb_filter_keeps_objects_with_three_day_arcs():
    obj = {"a": 2.5, "U": "9", "Arc_length": "3 days"}

    assert keep_mpcorb_object(obj)


def test_mpcorb_filter_treats_arc_years_as_long_enough():
    assert mpcorb_observation_arc_at_least({"Arc_years": "2024-2026"})
    assert mpcorb_observation_arc_at_least({"Arc_length": "2024-2026"})


def test_mpcorb_filter_drops_short_arc_high_uncertainty_inner_objects():
    obj = {"a": 2.5, "U": "9", "Arc_length": 2}

    assert not keep_mpcorb_object(obj)


def test_filter_orbit_objects_leaves_comets_unchanged():
    objects = [{"a": 2.5, "U": "9", "Arc_length": 2}]

    assert filter_orbit_objects(objects, comet=True) == objects
    assert filter_orbit_objects(objects, comet=False) == []


def _assert_default_sorcha_phys_columns(phys):
    assert phys["GS"].tolist() == [SORCHA_DEFAULT_GS]
    for column, value in SORCHA_DEFAULT_COLOR_OFFSETS.items():
        assert phys[column].tolist() == [value]


def test_mpcorb_to_sorcha_inputs_adds_all_filter_colour_offsets():
    objects = [
        {
            "Principal_desig": "A",
            "a": 2.0,
            "e": 0.1,
            "i": 1.0,
            "Node": 2.0,
            "Peri": 3.0,
            "M": 4.0,
            "Epoch": 2461000.5,
            "H": 15.0,
        }
    ]

    _, phys = mpcorb_to_sorcha_inputs(objects, ["A"])

    assert phys["ObjID"].tolist() == ["A"]
    assert phys["H_r"].tolist() == [15.0]
    _assert_default_sorcha_phys_columns(phys)


def test_comets_to_sorcha_inputs_adds_all_filter_colour_offsets():
    objects = [
        {
            "Designation_and_name": "C/2026 A1",
            "Provisional_packed_desig": None,
            "Perihelion_dist": 4.0,
            "e": 0.9,
            "i": 1.0,
            "Peri": 2.0,
            "Node": 3.0,
            "Epoch_year": 2026,
            "Epoch_month": 1,
            "Epoch_day": 1,
            "Year_of_perihelion": 2026,
            "Month_of_perihelion": 2,
            "Day_of_perihelion": 1.5,
            "H": 12.0,
        }
    ]

    _, phys = comets_to_sorcha_inputs(objects, ["C/2026 A1"])

    assert phys["ObjID"].tolist() == ["C/2026 A1"]
    assert phys["H_r"].tolist() == [12.0]
    _assert_default_sorcha_phys_columns(phys)


def test_comets_to_sorcha_inputs_preserves_rows_without_h():
    objects = [
        {
            "Designation_and_name": "C/2026 A1",
            "Provisional_packed_desig": None,
            "Epoch_year": 2026,
            "Epoch_month": 1,
            "Epoch_day": 1,
            "Year_of_perihelion": 2026,
            "Month_of_perihelion": 2,
            "Day_of_perihelion": 1.5,
            "Perihelion_dist": 1.0,
            "e": 0.9,
            "i": 1.0,
            "Peri": 2.0,
            "Node": 3.0,
            "H": None,
        }
    ]

    orbits, phys = comets_to_sorcha_inputs(objects, ["C/2026 A1"])

    assert orbits["ObjID"].tolist() == ["C/2026 A1"]
    assert phys["H_r"].isna().all()
    del objects[0]["H"]
    orbits, phys = comets_to_sorcha_inputs(objects, ["C/2026 A1"])
    assert len(orbits) == 1
    assert phys["H_r"].isna().all()


def test_dp1_report_normalizes_physical_filter_to_band():
    assert rubin_band("r_03") == "r"
    assert rubin_band("g") == "g"
    assert pd.isna(rubin_band("not_a_filter"))


def test_filter_list_uses_rubin_filter_order():
    values = ["z", "g", "r", "u", "i", "y", "g", None, "not_a_filter"]

    assert format_rubin_filter_list(values) == "u,g,r,i,z,y"


def test_physical_filter_list_uses_rubin_filter_order():
    values = ["z_03", "r_03", "u_02", "g_01", "i_06", "y_04"]

    assert format_rubin_filter_list(values, normalize=False) == "u_02,g_01,r_03,i_06,z_03,y_04"


class MockResponse:
    status_code = 200
    content = gzip.compress(b'{"orbit": "data"}\n')


def test_get_current_orbits_creates_missing_work_dir(monkeypatch, tmp_path):
    work_dir = tmp_path / "missing" / "work"

    monkeypatch.setattr("ponder.utils.requests.get", lambda url: MockResponse())

    orbit_path = get_current_orbits("02-05-2026", work_dir)

    assert orbit_path == work_dir / "asteroid_orbits_02-05-2026.json"
    assert work_dir.is_dir()
    assert (work_dir / "asteroid_orbits_02-05-2026.json.gz").is_file()
    assert orbit_path.read_text() == '{"orbit": "data"}\n'
