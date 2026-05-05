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
