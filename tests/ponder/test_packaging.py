import pytest

import ponder
import ponder.__main__ as cli
from ponder.runner import main


def test_version():
    """Check to see that we can get the package version"""
    assert ponder.__version__ is not None


def test_console_script_target_imports():
    """Check that the generated console script import remains valid."""
    assert callable(main)


def test_cli_downloads_orbits_before_requiring_analysis_inputs(monkeypatch, capsys, tmp_path):
    """The CLI can fetch orbit files before validating analysis-only inputs."""
    downloaded_orbits = tmp_path / "orbits.json"
    download_calls = []

    def mock_get_current_orbits(*args, **kwargs):
        download_calls.append((args, kwargs))
        return downloaded_orbits

    monkeypatch.setattr(
        "sys.argv",
        ["ponder", "--comet", "--download_orbits"],
    )
    monkeypatch.setattr(cli, "get_current_orbits", mock_get_current_orbits)
    monkeypatch.setattr(cli, "run_ponder", lambda *args, **kwargs: pytest.fail("ran analysis"))

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    assert len(download_calls) == 1
    assert "--db and --config are required" in capsys.readouterr().err
