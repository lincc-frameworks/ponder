import sqlite3
import pandas as pd
import pytest
from ponder_tools.deep_ephemeris import read_pointings


def test_midpoints_use_exposure_duration_and_preserve_vr(tmp_path):
    db = tmp_path / "pointings.sqlite"
    with sqlite3.connect(db) as con:
        pd.DataFrame(
            dict(
                observationId=[2, 1],
                observationStartMJD_TAI=[59000.1, 59000.0],
                visitExposureTime=[30.0, 120.0],
                optFilter=["VR", "VR"],
            )
        ).to_sql("observations", con, index=False)
    result = read_pointings(db, "SELECT * FROM observations")
    assert result.FieldID.tolist() == [1, 2]
    assert result.optFilter.tolist() == ["VR", "VR"]
    assert result.observationMidpointMJD_TAI.tolist() == pytest.approx(
        [59000.0 + 60.0 / 86400, 59000.1 + 15.0 / 86400], abs=1e-10
    )


def test_duplicate_visits_rejected(tmp_path):
    db = tmp_path / "pointings.sqlite"
    with sqlite3.connect(db) as con:
        pd.DataFrame(dict(observationId=[1, 1])).to_sql("observations", con, index=False)
    with pytest.raises(ValueError, match="Duplicate"):
        read_pointings(db, "SELECT * FROM observations")
