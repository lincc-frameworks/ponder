import gzip

from ponder.utils import get_current_orbits


class MockResponse:
    status_code = 200
    content = gzip.compress(b'{"orbit": "data"}\n')


def test_get_current_orbits_creates_missing_work_dir(monkeypatch, tmp_path):
    work_dir = tmp_path / "missing" / "work"

    monkeypatch.setattr("ponder.utils.requests.get", lambda url: MockResponse())

    orbit_path = get_current_orbits("02-05-2026", work_dir)

    assert orbit_path == work_dir / "asteroid_orbits_02-05-2026.json"
    assert work_dir.is_dir()
    assert (work_dir / "asteroid_orbits_02-05-2026.json.gz").is_file()
    assert orbit_path.read_text() == '{"orbit": "data"}\n'
