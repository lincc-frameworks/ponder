import gzip
import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "dp1_rh_report.py"
SPEC = importlib.util.spec_from_file_location("dp1_rh_report", SCRIPT_PATH)
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


def test_read_circle_radius_requires_circular_camera(tmp_path):
    config = tmp_path / "config.ini"
    config.write_text("[FOV]\ncamera_model = circle\ncircle_radius = 0.50\n")
    assert REPORT.read_circle_radius(config) == 0.5

    config.write_text("[FOV]\ncamera_model = footprint\n")
    with pytest.raises(ValueError, match="camera_model=circle"):
        REPORT.read_circle_radius(config)


def test_read_detections_accepts_schema_less_empty_parquet(tmp_path):
    path = tmp_path / "detections.parquet"
    pd.DataFrame().to_parquet(path)

    result = REPORT.read_detections(path)

    assert result.empty
    assert result.columns.tolist() == REPORT.DETECTION_COLUMNS


@pytest.mark.parametrize(
    ("object_mode", "id_column", "object_id"),
    [
        ("asteroid", "Principal_desig", "2026 AB"),
        ("comet", "Designation_and_name", "C/2026 A1"),
    ],
)
def test_read_orbits_uses_mode_specific_output_id(tmp_path, object_mode, id_column, object_id):
    path = tmp_path / "orbits.json.gz"
    with gzip.open(path, "wt") as handle:
        json.dump([{id_column: object_id, "H": 12.3}], handle)

    result = REPORT.read_orbits(path, object_mode)
    assert result.to_dict("records") == [{"ObjID": object_id, "H_r": 12.3}]


def test_filter_to_circle_handles_ra_wraparound_and_strict_cut():
    ephem = pd.DataFrame(
        {
            "ObjID": ["wrapped", "inside", "near_outside", "outside"],
            "RA_deg": [359.9, 10.499999, 10.500001, 10.501],
            "Dec_deg": [0.0, 0.0, 0.0, 0.0],
            "fieldRA": [0.1, 10.0, 10.0, 10.0],
            "fieldDec": [0.0, 0.0, 0.0, 0.0],
        }
    )

    result = REPORT.filter_to_circle(ephem, 0.5)

    assert result["ObjID"].tolist() == ["wrapped", "inside"]
    assert result["fov_separation_deg"].max() < 0.5


def test_summary_records_mode_and_radius():
    ephem = pd.DataFrame(
        {
            "ObjID": ["A"],
            "object_mode": ["comet"],
            "fov_radius_deg": [0.5],
            "FieldID": [1],
            "rH_au": [6.0],
            "apparent_mag": [22.0],
            "positional_uncertainty_arcsec": [0.1],
            "local_obsnight": [20241108],
            "filter_band": ["r"],
            "band": ["r_03"],
        }
    )

    result = REPORT.summarize_rows(ephem)

    assert result.loc[0, "object_mode"] == "comet"
    assert result.loc[0, "fov_radius_deg"] == 0.5
    assert result.columns.tolist() == REPORT.SUMMARY_COLUMNS


def test_empty_summary_uses_same_output_schema():
    result = REPORT.summarize_rows(pd.DataFrame())

    assert result.columns.tolist() == REPORT.SUMMARY_COLUMNS
