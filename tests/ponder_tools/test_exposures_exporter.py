import astropy.units as u
import pandas as pd
import pytest
from astropy.coordinates import SkyCoord

from ponder_tools import exposures_exporter


@pytest.mark.parametrize(
    "ra, dec, center_ra, center_dec, expected",
    [
        (5.0, 1.0, 5.0, 1.0, True),
        (5.0, -1.0, 5.0, -1.0, True),
        (0.1, 0.1, 0.1, 0.1, True),
        (5.0, 1.0, 286.4788975654, 57.2957795131, False),
        (359.9, 0.0, 0.1, 0.0, True),
        (0.1, 0.0, 359.9, 0.0, True),
        (120.0, -30.0, 120.0, -30.0, True),
        (120.0, -30.0, 121.0, -30.0, False),
    ],
)
def test_cone_selection_uses_butler_degrees(ra, dec, center_ra, center_dec, expected):
    center = SkyCoord(ra=center_ra * u.deg, dec=center_dec * u.deg)
    record = {"tracking_ra": ra, "tracking_dec": dec}

    assert bool(exposures_exporter._passes_cone(record, center, 0.25)) is expected


@pytest.mark.parametrize("rotation", [0.0, 1.0, -5.0, 5.0, 6.48, 6.49, 90.0, 355.0, None])
def test_sorcha_export_preserves_degree_angles(rotation):
    frame = pd.DataFrame(
        {
            "exposure_id": [42],
            "timespan_begin_tai_jd": [2460000.5],
            "exposure_time": [30.0],
            "physical_filter": ["r"],
            "tracking_ra": [5.0],
            "tracking_dec": [1.0],
            "sky_angle": [rotation],
        }
    )

    output = exposures_exporter.make_sorcha_dataframe(frame)

    assert output.loc[0, "fieldRA_deg"] == 5.0
    assert output.loc[0, "fieldDec_deg"] == 1.0
    if rotation is None:
        assert pd.isna(output.loc[0, "fieldRotSkyPos_deg"])
    else:
        assert output.loc[0, "fieldRotSkyPos_deg"] == rotation


def test_parse_args_sets_export_paths(tmp_path):
    args = exposures_exporter._parse_args(
        [
            "--full-csv",
            str(tmp_path / "full.csv"),
            "--export-csv",
            str(tmp_path / "export.csv"),
            "--sorcha-csv",
            str(tmp_path / "sorcha.csv"),
        ]
    )

    assert args.full_csv == str(tmp_path / "full.csv")
    assert args.export_csv == str(tmp_path / "export.csv")
    assert args.sorcha_csv == str(tmp_path / "sorcha.csv")


def test_write_csv_creates_parent_directories(tmp_path):
    out_path = tmp_path / "nested" / "pointings" / "pointings_full.csv"

    exposures_exporter._write_csv(pd.DataFrame({"exposure_id": [1]}), out_path)

    assert out_path.exists()
    assert out_path.read_text().splitlines() == ["exposure_id", "1"]
