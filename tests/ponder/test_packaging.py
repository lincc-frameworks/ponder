import ponder
from ponder_tools.exposures_exporter import main as exposures_exporter_main
from ponder.runner import main


def test_version():
    """Check to see that we can get the package version"""
    assert ponder.__version__ is not None


def test_console_script_target_imports():
    """Check that the generated console script import remains valid."""
    assert callable(main)
    assert callable(exposures_exporter_main)
