import ponder
from ponder.runner import main
from ponder_tools.exposures_exporter import main as exporter_main


def test_version():
    """Check to see that we can get the package version"""
    assert ponder.__version__ is not None


def test_console_script_target_imports():
    """Check that the generated console script import remains valid."""
    assert callable(main)


def test_exposures_exporter_console_script_target_imports():
    assert callable(exporter_main)
