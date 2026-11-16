"""Offline synchronization/recovery tests; live checks are explicitly opt-in."""

import copy
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest
import requests

from ponder import decam_pointings as dp
from ponder.noirlab import ArchiveClient, ArchiveError, IncompleteWindow


def product(expnum=1, proc="raw", **changes):
    row = {
        "instrument": "decam",
        "obs_type": "object",
        "prod_type": "image",
        "proc_type": proc,
        "EXPNUM": expnum,
        "ra_center": 48.750675,
        "dec_center": -26.100277,
        "DATE-OBS": "2012-01-02T00:39:06.380685",
        "TIMESYS": "UTC",
        "MJD-OBS": 55928.02715718,
        "dateobs_center": "2012-01-02T00:39:08.880685Z",
        "exposure": 5.0,
        "ifilter": "r DECam SDSS c0002 6415.0 1480.0",
        "proposal": "test",
        "caldat": "2012-01-01",
        "release_date": "2013-01-01",
        "updated": "2012-01-03T00:00:00.000000Z",
        "archive_filename": f"/archive/{expnum}_{proc}.fits.fz",
        "original_filename": f"{expnum}_{proc}.fits.fz",
        "md5sum": f"{expnum:032x}",
    }
    row.update(changes)
    return row


class FakeArchive:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []
        self.fail_on = None
        self.preflights = 0

    def preflight(self):
        self.preflights += 1
        return "7.1"

    def window(self, field, lower, upper):
        self.calls.append((field, lower, upper))
        if self.fail_on and self.fail_on(field, lower, upper):
            raise ArchiveError("network failure")
        for row in self.rows:
            if row.get(field) and dp._utc(lower) <= dp._utc(row[field]) <= dp._utc(upper):
                yield copy.deepcopy(row)


NOW = datetime(2012, 2, 3, tzinfo=timezone.utc)


def sync(path, client, now=NOW, **kwargs):
    return dp.sync_catalog(path, client=client, now=now, log=lambda _: None, **kwargs)


def read(path):
    return pq.read_table(path).to_pylist()


def test_recorded_noirlab_metadata_roundtrip(tmp_path):
    fixture = json.loads((Path(__file__).parent / "data" / "noirlab_decam.json").read_text())
    path = tmp_path / "decam.parquet"
    result = sync(path, FakeArchive(fixture["rows"]), now=datetime(2024, 1, 4, tzinfo=timezone.utc))
    assert result["total"] == 3
    assert result["flagged_exposures"] == result["excluded_products"] == 0
    rows = read(path)
    assert rows[0]["start_utc"] == "2024-01-02T05:08:11.095307000Z"
    assert rows[0]["source_mjd_obs"] == "60311.21401731"
    assert {row["proc_type"] for row in rows} == {"raw", "instcal", "resampled"}


def test_unique_exposures_preference_ties_and_unreleased_images(tmp_path):
    path = tmp_path / "decam.parquet"
    rows = [product(1), product(1, "resampled"), product(1, "instcal"), product(2)]
    rows += [product(1, "instcal", archive_filename="/archive/0.fits", md5sum="f" * 32)]
    result = sync(path, FakeArchive(list(reversed(rows))))
    assert result["added"] == result["total"] == 2
    catalog = read(path)
    assert catalog[0]["proc_type"] == "instcal"
    assert catalog[0]["archive_filename"] == "/archive/0.fits"
    assert catalog[0]["release_date"] == "2013-01-01"
    assert catalog[1]["proc_type"] == "raw"
    assert pq.read_schema(path).remove_metadata() == dp.SCHEMA
    assert pq.read_metadata(path).metadata[b"ponder.api_version"] == b"7.1"


def test_incremental_upgrade_correction_late_addition_and_no_downgrade(tmp_path):
    path = tmp_path / "decam.parquet"
    sync(path, FakeArchive([product(1), product(2, "instcal")]))
    next_day = NOW + timedelta(days=1)
    update = dp._iso(NOW + timedelta(hours=1))
    archive = FakeArchive(
        [
            product(1, "instcal", updated=update),
            product(2, updated=update),
            product(3, updated=update),  # Old observation ingested late.
        ]
    )
    result = sync(path, archive, now=next_day)
    assert (result["added"], result["changed"], result["total"]) == (1, 1, 3)
    assert [r["proc_type"] for r in read(path)] == ["instcal", "instcal", "raw"]
    assert {call[0] for call in archive.calls} == {"updated", "release_date"}
    archive.rows = [product(1, "instcal", updated=dp._iso(next_day), ra_center=100, md5sum="c" * 32)]
    result = sync(path, archive, now=next_day + timedelta(days=1))
    assert result["changed"] == 1
    assert read(path)[0]["ra_deg"] == 100
    result = sync(path, archive, now=next_day + timedelta(days=2))
    assert result["added"] == result["changed"] == 0


def test_release_sweep_finds_old_metadata_newly_visible(tmp_path):
    path = tmp_path / "decam.parquet"
    sync(path, FakeArchive())
    row = product(1, release_date="2012-02-03")  # updated lies outside overlap.
    result = sync(path, FakeArchive([row]), now=NOW + timedelta(days=1))
    assert result["added"] == 1


def test_full_refresh_removes_products_and_reselects_candidates(tmp_path):
    path = tmp_path / "decam.parquet"
    sync(path, FakeArchive([product(1), product(1, "instcal"), product(2)]))
    result = sync(path, FakeArchive([product(1)]), now=NOW + timedelta(days=1), full_refresh=True)
    assert result["removed"] == result["changed"] == 1
    assert read(path)[0]["proc_type"] == "raw"


def test_valid_raw_preferred_over_invalid_calibrated_and_quarantine(tmp_path):
    path = tmp_path / "decam.parquet"
    rows = [
        product(1),
        product(1, "instcal", ra_center=400),
        product(2, EXPNUM=None),
        product(3, dec_center=None),
    ]
    result = sync(path, FakeArchive(rows))
    assert result["excluded_products"] == result["flagged_exposures"] == 1
    assert read(path)[0]["proc_type"] == "raw"
    assert read(path)[1]["quality_flags"] == ["invalid_dec_center"]
    status = dp.catalog_status(path)
    assert status["excluded_product_examples"][0]["archive_filename"] == "/archive/2_raw.fits.fz"
    with sqlite3.connect(dp.sidecar_path(path)) as con:
        assert con.execute("SELECT COUNT(*) FROM products WHERE expnum IS NULL").fetchone()[0] == 1


@pytest.mark.parametrize("expnum", [None, "", "abc", 0, -1, 1.5, "NaN", "Infinity", 2**63, True])
def test_bad_exposure_ids_are_not_guessed(expnum):
    row, _ = dp.normalize(product(EXPNUM=expnum))
    assert row["exposure_id"] is None
    assert "missing_or_invalid_exposure_id" in row["quality_flags"]


def test_time_precision_and_time_scales():
    row, valid = dp.normalize(product())
    assert valid
    assert row["start_utc"] == "2012-01-02T00:39:06.380685000Z"
    assert row["midpoint_utc"] == "2012-01-02T00:39:08.880685000Z"
    assert (row["midpoint_mjd_utc"] - row["start_mjd_utc"]) * 86400 == pytest.approx(2.5, abs=1e-6)
    tai, valid = dp.normalize(product(TIMESYS="TAI", **{"DATE-OBS": "2012-01-02T00:39:40.380685"}))
    assert valid
    assert tai["start_utc"] == row["start_utc"]
    assert tai["source_timesys"] == "TAI"
    missing, valid = dp.normalize(product(TIMESYS=None))
    assert not valid
    assert missing["start_utc"] is None
    assert missing["source_date_obs"] == product()["DATE-OBS"]


@pytest.mark.parametrize(
    "changes,flag",
    [
        ({"ra_center": 360}, "invalid_ra_center"),
        ({"dec_center": -91}, "invalid_dec_center"),
        ({"exposure": float("nan")}, "invalid_exposure"),
        ({"exposure": -2}, "invalid_exposure"),
        ({"DATE-OBS": "bad"}, "invalid_DATE-OBS"),
        ({"dateobs_center": "2012-01-02T00:40:00Z"}, "inconsistent_midpoint"),
    ],
)
def test_invalid_pointing_values(changes, flag):
    row, valid = dp.normalize(product(**changes))
    assert not valid
    assert flag in row["quality_flags"]
    dp._json(row)  # No NaN/Infinity in published payloads.


def test_newest_product_within_processing_type(tmp_path):
    path = tmp_path / "decam.parquet"
    old = product(1, "instcal", archive_filename="/a", ra_center=1)
    new = product(1, "instcal", archive_filename="/z", ra_center=2, updated="2012-02-01T01:00:00Z")
    sync(path, FakeArchive([new, old]))
    assert read(path)[0]["ra_deg"] == 2


def test_resume_bootstrap_skips_completed_windows(tmp_path):
    path = tmp_path / "decam.parquet"
    archive = FakeArchive([product()])
    archive.fail_on = lambda field, lower, upper: lower == "2012-02-01"
    with pytest.raises(ArchiveError, match="network"):
        sync(path, archive)
    assert not path.exists()
    assert dp.catalog_status(path)["completed_windows"] == 1
    archive.fail_on = None
    archive.calls.clear()
    sync(path, archive, now=NOW + timedelta(days=1))
    assert archive.calls == [("caldat", "2012-02-01", "2012-02-03")]
    assert dp.catalog_status(path)["last_sync"] == dp._iso(NOW)
    assert len(read(path)) == 1


def test_incomplete_window_rolls_back_and_retries(tmp_path):
    class ChangingArchive(FakeArchive):
        attempts = 0

        def window(self, *window):
            self.attempts += 1
            if self.attempts == 1:
                yield product(99)
                raise IncompleteWindow("changed")
            yield product(1)

    path = tmp_path / "decam.parquet"
    archive = ChangingArchive()
    sync(path, archive, now=datetime(2012, 1, 10, tzinfo=timezone.utc))
    assert archive.attempts == 2
    assert [r["exposure_id"] for r in read(path)] == [1]


def test_persistent_incomplete_window_preserves_previous_output(tmp_path):
    class BrokenArchive(FakeArchive):
        attempts = 0

        def window(self, *window):
            self.attempts += 1
            yield product(99)
            raise IncompleteWindow("still incomplete")

    path = tmp_path / "decam.parquet"
    sync(path, FakeArchive([product()]))
    previous = path.read_bytes()
    archive = BrokenArchive()
    with pytest.raises(IncompleteWindow):
        sync(path, archive, now=NOW + timedelta(days=1))
    assert path.read_bytes() == previous
    assert archive.attempts == 3


def test_export_failure_recovers_without_network(tmp_path, monkeypatch):
    path = tmp_path / "decam.parquet"
    sync(path, FakeArchive([product()]))
    previous = path.read_bytes()
    original_replace = os.replace
    with monkeypatch.context() as patch:
        patch.setattr(dp.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("disk failure")))
        with pytest.raises(OSError, match="disk failure"):
            sync(path, FakeArchive([product(2)]), now=NOW + timedelta(days=1), full_refresh=True)
    assert os.replace is original_replace
    assert path.read_bytes() == previous
    assert dp.catalog_status(path)["pending_run"]["phase"] == "export"
    assert not list(tmp_path.glob("*.tmp"))
    archive = FakeArchive()
    sync(path, archive, now=NOW + timedelta(days=2))
    assert archive.preflights == 0
    assert [r["exposure_id"] for r in read(path)] == [2]
    assert dp.catalog_status(path)["parquet_matches_sidecar"]


def test_full_refresh_can_supersede_pending_incremental(tmp_path):
    path = tmp_path / "decam.parquet"
    sync(path, FakeArchive([product()]))
    broken = FakeArchive()
    broken.fail_on = lambda *_: True
    with pytest.raises(ArchiveError):
        sync(path, broken, now=NOW + timedelta(days=1))
    sync(path, FakeArchive([product(2)]), now=NOW + timedelta(days=2), full_refresh=True)
    assert [r["exposure_id"] for r in read(path)] == [2]


def test_crash_after_publish_before_checkpoint_is_recoverable(tmp_path, monkeypatch):
    path = tmp_path / "decam.parquet"
    sync(path, FakeArchive([product()]))
    original_set = dp._set

    def fail_checkpoint(con, key, value):
        if key == "active_generation":
            raise sqlite3.OperationalError("checkpoint failed")
        original_set(con, key, value)

    with monkeypatch.context() as patch:
        patch.setattr(dp, "_set", fail_checkpoint)
        with pytest.raises(sqlite3.OperationalError):
            sync(path, FakeArchive([product(2)]), full_refresh=True)
    status = dp.catalog_status(path)
    assert status["state"] == "pending"
    assert not status["parquet_matches_sidecar"]
    assert [r["exposure_id"] for r in read(path)] == [2]
    archive = FakeArchive()
    sync(path, archive)
    assert archive.preflights == 0
    assert dp.catalog_status(path)["parquet_matches_sidecar"]


def test_status_after_failed_preflight_and_missing_parquet(tmp_path):
    class Unavailable(FakeArchive):
        def preflight(self):
            raise ArchiveError("offline")

    path = tmp_path / "decam.parquet"
    with pytest.raises(ArchiveError):
        sync(path, Unavailable())
    assert dp.catalog_status(path)["state"] == "not_initialized"
    sync(path, FakeArchive([product()]))
    path.unlink()
    assert dp.catalog_status(path)["state"] == "missing_parquet"
    sync(path, FakeArchive())
    assert len(read(path)) == 1


def test_lock_rejects_concurrent_writer_and_releases(tmp_path):
    path = tmp_path / "decam.parquet"
    with dp.writer_lock(path):
        with pytest.raises(ArchiveError, match="Another updater"):
            sync(path, FakeArchive())
    sync(path, FakeArchive())
    assert read(path) == []
    assert pq.read_schema(path).remove_metadata() == dp.SCHEMA


def test_status_is_read_only_and_missing_sidecar_requires_explicit_rebuild(tmp_path, monkeypatch):
    path = tmp_path / "decam.parquet"
    monkeypatch.setattr(dp, "ArchiveClient", lambda: pytest.fail("Status contacted network"))
    assert dp.catalog_status(path)["state"] == "not_initialized"
    assert list(tmp_path.iterdir()) == []
    sync(path, FakeArchive([product()]))
    assert dp.catalog_status(path)["state"] == "ready"
    dp.sidecar_path(path).unlink()
    assert dp.catalog_status(path)["state"] == "missing_sidecar"
    with pytest.raises(ArchiveError, match="without its sidecar"):
        sync(path, FakeArchive())
    sync(path, FakeArchive(), full_refresh=True)
    assert read(path) == []


def test_cli_status_and_failure(tmp_path, capsys):
    path = tmp_path / "decam.parquet"
    assert dp.main(["--output", str(path), "--status"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "not_initialized"
    path.write_bytes(b"existing file")
    assert dp.main(["--output", str(path)]) == 1
    assert "without its sidecar" in capsys.readouterr().err


class Session:
    def __init__(self, responses):
        self.headers = {}
        self.responses = iter(responses)
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        item = next(self.responses)
        if isinstance(item, Exception):
            raise item
        return item


def response(rows=(), status=200, headers=None, raw=None):
    r = requests.Response()
    r.status_code = status
    r.headers.update(headers or {})
    r._content = (
        raw if raw is not None else json.dumps([{"META": {}, "HEADER": {}, "PARAMETERS": {}}, *rows]).encode()
    )
    return r


def test_client_pagination_count_checks_and_aliases():
    one = product(1)
    one["file:EXPNUM"] = 1
    session = Session(
        [
            response([{"count": 3}]),
            response([one, product(2)]),
            response([product(3)]),
            response([{"count": 3}]),
        ]
    )
    rows = list(ArchiveClient(session=session, page_size=2).window("caldat", "2012-01-01", "2012-01-02"))
    assert len(rows) == 3 and "file:EXPNUM" not in rows[0]
    assert [c[1]["params"].get("offset") for c in session.calls] == [None, 0, 2, None]
    assert all(c[1]["timeout"] == (10, 60) for c in session.calls)


@pytest.mark.parametrize(
    "responses",
    [
        [response([{"count": 2}]), response([product()]), response([product()])],
        [response([{"count": 2}]), response([])],
        [response([{"count": 1}]), response([product()]), response([{"count": 2}])],
        [response([{"count": 1}]), response([product(1), product(2)])],
    ],
)
def test_client_rejects_incomplete_or_repeated_pages(responses):
    with pytest.raises(IncompleteWindow):
        list(ArchiveClient(session=Session(responses), page_size=1).window("caldat", "a", "b"))


@pytest.mark.parametrize("bad", [b"not json", b"{}", b"[]", b'[{"error":"bad query"}]'])
def test_client_rejects_malformed_response(bad):
    with pytest.raises(ArchiveError):
        list(ArchiveClient(session=Session([response(raw=bad)])).window("caldat", "a", "b"))


def test_client_respects_retry_after_and_connection_retries():
    sleeps = []
    session = Session(
        [
            requests.Timeout(),
            response(status=429, headers={"Retry-After": "3"}),
            response(status=503),
            response([{"count": 0}]),
            response([{"count": 0}]),
        ]
    )
    client = ArchiveClient(session=session, sleep=sleeps.append)
    assert list(client.window("caldat", "a", "b")) == []
    assert sleeps == [1, 3, 4]


def test_client_does_not_retry_before_long_retry_after():
    session = Session([response(status=429, headers={"Retry-After": "120"})])
    with pytest.raises(ArchiveError, match="rerun later"):
        list(
            ArchiveClient(session=session, sleep=lambda _: pytest.fail("unexpected sleep")).window(
                "caldat", "a", "b"
            )
        )
    assert len(session.calls) == 1


def test_client_exhausted_retries_and_permanent_http_failure():
    session = Session([requests.Timeout()] * 3)
    with pytest.raises(ArchiveError, match="connection failed"):
        list(ArchiveClient(session=session, attempts=3, sleep=lambda _: None).window("caldat", "a", "b"))
    assert len(session.calls) == 3
    session = Session([response(status=400)])
    with pytest.raises(ArchiveError, match="Invalid NOIRLab response"):
        list(ArchiveClient(session=session).window("caldat", "a", "b"))
    assert len(session.calls) == 1


def test_client_conflicting_aliases_are_rejected():
    row = product()
    row["file:EXPNUM"] = 999
    session = Session([response([{"count": 1}]), response([row])])
    with pytest.raises(ArchiveError, match="Conflicting archive aliases"):
        list(ArchiveClient(session=session).window("caldat", "a", "b"))


def test_preflight_rejects_missing_fields():
    session = Session([response(raw=b"7.1"), response(raw=b'[{"Field":"md5sum"}]')])
    with pytest.raises(ArchiveError, match="Missing archive fields"):
        ArchiveClient(session=session).preflight()


@pytest.mark.skipif(os.environ.get("PONDER_LIVE_NOIRLAB") != "1", reason="opt-in NOIRLab smoke test")
def test_live_noirlab_historical_window():
    client = ArchiveClient(page_size=200)
    assert client.preflight()
    rows = list(client.window("caldat", "2024-01-01", "2024-01-01"))
    assert rows
    assert all(dp.normalize(row)[0]["instrument"] == "decam" for row in rows)
