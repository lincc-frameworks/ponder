import numpy as np
import pandas as pd
import pytest

from ponder_tools.deep_exporter import CORNERS, collapse_visits, convert_rows, read_joined_chunks
from ponder_tools.deep_exporter import export_deep


def sample():
    return pd.DataFrame(
        [
            dict(
                visit=1,
                detector=d,
                mjd_start=59815.25,
                mjd_mid=59815.25 + 60.5 / 86400,
                exposureTime=120.0,
                pointing_ra=357.0,
                pointing_dec=-2.0,
                psfSigma=2.0,
                psfArea=50.0,
                zeroPoint=31.0,
                skyNoise=10.0,
                night="20220823",
                fieldname="B1m",
                **{c: 1.0 for c in CORNERS},
            )
            for d in [1, 2]
        ]
    )


def test_tai_midpoint_is_not_treated_as_utc_start():
    converted = convert_rows(sample(), band="VR", time_semantics="date-avg-tai")
    visits = collapse_visits(converted)
    assert len(visits) == 1
    assert visits.detectorCount.iloc[0] == 2
    midpoint = visits.observationStartMJD.iloc[0] + visits.visitTime.iloc[0] / 172800
    assert abs(midpoint - 59815.25) < 1e-10
    assert visits.band.iloc[0] == "VR"
    # The real half-exposure is 60 seconds, not the legacy increment 60.5.
    assert abs((59815.25 - visits.observationStartMJD.iloc[0]) * 86400 - 60) < 1e-5


def test_inconsistent_detectors_rejected():
    rows = sample()
    rows.loc[1, "pointing_ra"] += 0.1
    converted = convert_rows(rows, band="VR", time_semantics="date-avg-tai")
    with pytest.raises(ValueError, match="Conflicting pointing_ra"):
        collapse_visits(converted)


@pytest.mark.parametrize(
    "column,value", [("exposureTime", 0), ("mjd_start", np.nan), ("pointing_dec", 100), ("mjd_mid", 59816)]
)
def test_invalid_or_unrecognised_metadata_rejected(column, value):
    rows = sample()
    rows.loc[0, column] = value
    with pytest.raises(ValueError):
        convert_rows(rows, band="VR", time_semantics="date-avg-tai")


def test_explicit_time_semantics_required():
    with pytest.raises(ValueError, match="Explicit"):
        convert_rows(sample(), band="VR", time_semantics="utc")


def test_quoted_ecsv_and_identical_uuid_aliases(tmp_path):
    import sqlite3

    rows = sample()
    rows["detector"] = 1
    rows["dataId"] = ["uuid-a", "uuid-b"]
    rows["wcs"] = '{"example": "large quoted WCS with spaces"}'
    source = tmp_path / "joined.collection"
    source.write_text("# %ECSV 1.0\n# metadata\n" + rows.to_csv(index=False, sep=" "))
    db = tmp_path / "output.sqlite"
    manifest = export_deep(source, db, band="VR", time_semantics="date-avg-tai")
    assert manifest["visits"] == 1
    assert manifest["detector_rows"] == 1
    assert manifest["duplicate_dataset_alias_rows"] == 2
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM duplicate_dataset_aliases").fetchone()[0] == 2
    with pytest.raises(FileExistsError):
        export_deep(source, db, band="VR", time_semantics="date-avg-tai")
