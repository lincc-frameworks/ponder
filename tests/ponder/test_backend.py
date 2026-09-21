import sys
from pathlib import Path
import pytest
from ponder import runner


@pytest.mark.parametrize(
    "setting,expected",
    [
        ("", ["sorcha", "run"]),
        ("[PONDER]\nbackend=deep_geometry\n", [sys.executable, "-m", "ponder_tools.deep_ephemeris"]),
    ],
)
def test_backend_dispatch_preserves_default(tmp_path, monkeypatch, setting, expected):
    config = tmp_path / "config.ini"
    config.write_text(setting)
    calls = []

    class Process:
        def __init__(self, args, **kwargs):
            calls.append(args)

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(runner.subprocess, "Popen", Process)
    runner.run_sorcha("orbits", "phys", Path("out.csv"), "db", config)
    assert calls[0][: len(expected)] == expected


def test_unknown_backend_fails_before_launch(tmp_path):
    config = tmp_path / "config.ini"
    config.write_text("[PONDER]\nbackend=typo\n")
    with pytest.raises(ValueError, match="Unknown"):
        runner.run_sorcha("orbits", "phys", Path("out.csv"), "db", config)
