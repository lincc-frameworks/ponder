import sqlite3

import pandas as pd

from ponder import runner
from ponder_tools.exposures_exporter import (
    OBSERVATION_COLUMNS,
    observations_dataframe_from_full_export,
    observations_dataframe_from_sorcha,
    write_observations_sqlite_from_csv,
    write_observations_sqlite_from_sorcha_csv,
)


def test_observations_dataframe_from_sorcha_maps_ini_columns():
    sorcha = pd.DataFrame(
        {
            "observationId": [101, 102],
            "observationStartMJD_TAI": [60000.1, 60001.2],
            "visitExposureTime": [30.0, 15.0],
            "filter": ["r", "g"],
            "seeingFwhmGeom_arcsec": [0.8, 0.9],
            "seeingFwhmEff_arcsec": [1.0, 1.1],
            "fieldFiveSigmaDepth_mag": [24.5, 25.0],
            "fieldRA_deg": [10.0, 11.0],
            "fieldDec_deg": [-5.0, -6.0],
            "fieldRotSkyPos_deg": [90.0, 91.0],
        }
    )

    observations = observations_dataframe_from_sorcha(sorcha)

    assert observations.columns.tolist() == OBSERVATION_COLUMNS
    assert observations["observationStartMJD"].tolist() == [60000.1, 60001.2]
    assert observations["visitTime"].tolist() == [30.0, 15.0]
    assert observations["visitExposureTime"].tolist() == [30.0, 15.0]
    assert observations["band"].tolist() == ["r", "g"]
    assert observations["seeingFwhmGeom"].tolist() == [0.8, 0.9]
    assert observations["seeingFwhmEff"].tolist() == [1.0, 1.1]
    assert observations["fiveSigmaDepth"].tolist() == [24.5, 25.0]
    assert observations["fieldRA"].tolist() == [10.0, 11.0]
    assert observations["fieldDec"].tolist() == [-5.0, -6.0]
    assert observations["rotSkyPos"].tolist() == [90.0, 91.0]


def test_write_observations_sqlite_from_sorcha_csv_supports_ponder_db_helpers(tmp_path):
    sorcha_path = tmp_path / "pointings_sorcha.csv"
    sqlite_path = tmp_path / "pointings.sqlite"
    pd.DataFrame(
        {
            "observationId": [101, 102],
            "observationStartMJD_TAI": [60000.1, 60001.2],
            "visitExposureTime": [30.0, 30.0],
            "filter": ["r", "g"],
            "seeingFwhmGeom_arcsec": [1.0, 1.0],
            "seeingFwhmEff_arcsec": [1.0, 1.0],
            "fieldFiveSigmaDepth_mag": [25.0, 25.0],
            "fieldRA_deg": [10.0, 11.0],
            "fieldDec_deg": [-5.0, -6.0],
            "fieldRotSkyPos_deg": [90.0, 91.0],
        }
    ).to_csv(sorcha_path, index=False)

    write_observations_sqlite_from_sorcha_csv(sorcha_path, sqlite_path)

    with sqlite3.connect(sqlite_path) as con:
        columns = [row[1] for row in con.execute("PRAGMA table_info(observations)").fetchall()]
        rows = con.execute(
            "SELECT observationId, observationStartMJD, visitTime, band FROM observations "
            "ORDER BY observationId"
        ).fetchall()

    assert columns == OBSERVATION_COLUMNS
    assert rows == [(101, 60000.1, 30.0, "r"), (102, 60001.2, 30.0, "g")]
    assert runner.db_count(sqlite_path) == 2
    assert runner.db_max_mjd(sqlite_path) == 60001.2

    new_db = tmp_path / "new_pointings.sqlite"
    assert runner.extract_new_pointings(sqlite_path, 60000.5, new_db) == 1
    with sqlite3.connect(new_db) as con:
        assert con.execute("SELECT observationId FROM observations").fetchall() == [(102,)]


def test_observations_dataframe_from_sorcha_drops_invalid_geometry():
    sorcha = pd.DataFrame(
        {
            "observationId": [101, 102, 103, 104, 105],
            "observationStartMJD_TAI": [60000.0, 60001.0, 60002.0, 60003.0, 60004.0],
            "visitExposureTime": [30.0] * 5,
            "filter": ["r"] * 5,
            "fieldRA_deg": [10.0, None, "bad", 360.0, 20.0],
            "fieldDec_deg": [-5.0, -5.0, -5.0, 0.0, 90.1],
        }
    )

    observations = observations_dataframe_from_sorcha(sorcha)

    assert observations["observationId"].tolist() == [101]
    assert observations["fieldRA"].tolist() == [10.0]
    assert observations["fieldDec"].tolist() == [-5.0]


def test_observations_dataframe_from_full_export_drops_calibrations_with_coordinates():
    full = pd.DataFrame(
        {
            "exposure_id": [1, 2, 3, 4, 5, 6, 7],
            "timespan_begin_tai_jd": [2460000.5 + i for i in range(7)],
            "exposure_time": [30.0] * 7,
            "physical_filter": ["r_03"] * 7,
            "tracking_ra": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, None],
            "tracking_dec": [-5.0, -6.0, -7.0, -8.0, -9.0, -10.0, -11.0],
            "sky_angle": [0.0] * 7,
            "observation_type": ["science", "dark", "ccob", "acq", "focus", "engtest", "science"],
            "observation_reason": [
                "object",
                "object",
                "ccob",
                "stray",
                "focus",
                "engtest",
                "object",
            ],
        }
    )

    observations = observations_dataframe_from_full_export(full)

    assert observations["observationId"].tolist() == [1, 4, 5, 6]


def test_full_export_sqlite_supports_ponder_db_helpers_after_filtering(tmp_path):
    source_path = tmp_path / "pointings_full.csv"
    sqlite_path = tmp_path / "pointings.sqlite"
    pd.DataFrame(
        {
            "exposure_id": [1, 2, 3, 4],
            "timespan_begin_tai_jd": [2460000.5, 2460001.5, 2460002.5, 2460003.5],
            "exposure_time": [30.0] * 4,
            "physical_filter": ["r_03"] * 4,
            "tracking_ra": [10.0, 20.0, 30.0, 40.0],
            "tracking_dec": [-5.0, -6.0, -7.0, -8.0],
            "sky_angle": [0.0] * 4,
            "observation_type": ["science", "flat", "acq", "science"],
            "observation_reason": ["object", "flat", "stray", "dark"],
        }
    ).to_csv(source_path, index=False)

    write_observations_sqlite_from_csv(source_path, sqlite_path)

    assert runner.db_count(sqlite_path) == 2
    assert runner.db_max_mjd(sqlite_path) == 60002.0
    new_db = tmp_path / "new_pointings.sqlite"
    assert runner.extract_new_pointings(sqlite_path, 60000.5, new_db) == 1
    with sqlite3.connect(new_db) as con:
        assert con.execute("SELECT observationId FROM observations").fetchall() == [(3,)]
