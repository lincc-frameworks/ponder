import sqlite3
import pandas as pd
import pytest
from ponder_tools.deep_ephemeris import read_pointings, select_circle


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


def test_select_circle_keeps_only_predictions_inside_radius():
    joined = pd.DataFrame(
        dict(
            ObjID=["a", "b"],
            FieldID=[1, 1],
            RA_deg=[10.0, 10.0],
            Dec_deg=[0.5, 2.0],
            fieldRA_deg=[10.0, 10.0],
            fieldDec_deg=[0.0, 0.0],
        )
    )
    selected = select_circle(joined, 1.1)
    assert selected.ObjID.tolist() == ["a"]
    assert selected.field_separation_deg.tolist() == pytest.approx([0.5])


def test_select_circle_empty_chunk_returns_headed_empty_table():
    # Sorcha's ephemeris output is all object-dtype when a chunk has no predictions.
    columns = ["ObjID", "FieldID", "RA_deg", "Dec_deg", "fieldRA_deg", "fieldDec_deg"]
    joined = pd.DataFrame({c: pd.Series(dtype=object) for c in columns})
    selected = select_circle(joined, 1.1)
    assert selected.empty
    assert list(selected.columns) == columns + ["field_separation_deg"]
