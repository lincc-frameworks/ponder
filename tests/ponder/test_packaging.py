import ponder


def test_version():
    """Check to see that we can get the package version"""
    assert ponder.__version__ is not None
