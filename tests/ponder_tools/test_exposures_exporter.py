import pandas as pd

from ponder_tools import exposures_exporter


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
