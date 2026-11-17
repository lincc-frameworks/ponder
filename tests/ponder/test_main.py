import sys
from pathlib import Path

import pytest

from ponder import __main__ as ponder_main


@pytest.mark.parametrize("master", [None, "custom/master.parquet"])
def test_cli_forwards_optional_master_path(monkeypatch, master):
    calls = []
    argv = ["ponder", "--db", "pointings.db", "--orbits", "objects.json", "--config", "config.ini"]
    if master:
        argv += ["--main-orbit-file", master]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(ponder_main, "run_ponder", lambda *args, **kwargs: calls.append(kwargs))

    ponder_main.main()

    assert calls[0]["main_orbit_file"] == (Path(master) if master else None)


def test_download_orbits_without_db_skips_analysis(monkeypatch, tmp_path, capsys):
    orbit_path = tmp_path / "comet_orbits.json"
    download_calls = []

    def fake_get_current_orbits(date_str, work_dir, comet=False):
        download_calls.append((date_str, work_dir, comet))
        return orbit_path

    def fake_run_ponder(*args, **kwargs):
        raise AssertionError("run_ponder should not be called without --db")

    monkeypatch.setattr(sys, "argv", ["ponder", "--work-dir", str(tmp_path), "--download_orbits", "--comet"])
    monkeypatch.setattr(ponder_main, "get_current_orbits", fake_get_current_orbits)
    monkeypatch.setattr(ponder_main, "run_ponder", fake_run_ponder)

    ponder_main.main()

    assert len(download_calls) == 1
    _, work_dir, comet = download_calls[0]
    assert work_dir == tmp_path
    assert comet is True
    out = capsys.readouterr().out
    assert "No database supplied" in out
    assert f"downloaded orbits to {orbit_path}" in out
    assert "Skipping Ponder analysis" in out
    assert "Running comet analysis" not in out
