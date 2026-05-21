from ponder.utils import filter_orbit_objects, keep_mpcorb_object, mpcorb_observation_arc_at_least


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


import gzip

from ponder.utils import get_current_orbits


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
