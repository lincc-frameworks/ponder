"""Resumable DECam metadata synchronization and atomic exposure-catalog export."""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from astropy.time import Time

from .noirlab import ARCHIVE_URL, ArchiveClient, ArchiveError, IncompleteWindow

SCHEMA_VERSION = 1
START = datetime(2012, 1, 1, tzinfo=timezone.utc)
PRIORITY = {"instcal": 0, "raw": 1, "resampled": 2}
SCHEMA = pa.schema(
    [
        ("instrument", pa.string()),
        ("exposure_id", pa.int64()),
        ("ra_deg", pa.float64()),
        ("dec_deg", pa.float64()),
        ("start_utc", pa.string()),
        ("midpoint_utc", pa.string()),
        ("start_mjd_utc", pa.float64()),
        ("midpoint_mjd_utc", pa.float64()),
        ("exposure_seconds", pa.float64()),
        ("filter", pa.string()),
        ("proposal", pa.string()),
        ("observing_date", pa.string()),
        ("proc_type", pa.string()),
        ("release_date", pa.string()),
        ("archive_updated_utc", pa.string()),
        ("archive_filename", pa.string()),
        ("original_filename", pa.string()),
        ("md5sum", pa.string()),
        ("source_date_obs", pa.string()),
        ("source_timesys", pa.string()),
        ("source_mjd_obs", pa.string()),
        ("source_dateobs_center", pa.string()),
        ("quality_flags", pa.list_(pa.string())),
    ]
)


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def _utc(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _text(value):
    return None if value is None or value == "" else str(value)


def normalize(row):
    """Return a typed exposure candidate, retaining source values and quality issues."""
    if (
        row.get("instrument") != "decam"
        or row.get("obs_type") != "object"
        or row.get("proc_type") not in PRIORITY
        or row.get("prod_type") not in {"image", "image1"}
    ):
        raise ArchiveError("NOIRLab returned a record outside the science-image selection")
    for field in ("archive_filename", "md5sum"):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise ArchiveError(f"Archive product has no usable {field}")
    flags = []
    try:
        expnum = Decimal(str(row.get("EXPNUM")))
        if not expnum.is_finite() or expnum != expnum.to_integral_value() or not 0 < expnum < 2**63:
            raise ValueError
        expnum = int(expnum)
    except (InvalidOperation, ValueError):
        expnum = None
        flags.append("missing_or_invalid_exposure_id")

    def number(key, lower, upper, upper_inclusive=True):
        try:
            value = float(row[key])
            if not math.isfinite(value) or value < lower or value > upper:
                raise ValueError
            if not upper_inclusive and value == upper:
                raise ValueError
            return value
        except (KeyError, TypeError, ValueError):
            flags.append(f"invalid_{key}")
            return None

    def instant(key, scale):
        value = row.get(key)
        try:
            if not value or not scale or scale.lower() not in Time.SCALES:
                raise ValueError
            text = str(value).removesuffix("Z").removesuffix("+00:00").replace(" ", "T")
            t = Time(text, format="isot", scale=scale.lower(), precision=9).utc
            return t.isot + "Z", float(t.mjd)
        except (TypeError, ValueError):
            flags.append(f"invalid_{key}")
            return None, None

    ra = number("ra_center", 0, 360, upper_inclusive=False)
    dec = number("dec_center", -90, 90)
    duration = number("exposure", 0, float("inf"))
    start, start_mjd = instant("DATE-OBS", _text(row.get("TIMESYS")))
    midpoint, midpoint_mjd = instant("dateobs_center", "utc")
    # Do not assume a missing/unknown TIMESYS is UTC, or invent a start from the midpoint.
    valid = all(v is not None for v in (ra, dec, duration, start, midpoint))
    if valid and (midpoint_mjd < start_mjd or abs((midpoint_mjd - start_mjd) * 86400 - duration / 2) > 1):
        flags.append("inconsistent_midpoint")
        valid = False
    try:
        updated = _iso(_utc(row["updated"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        updated = None
        flags.append("invalid_updated")
    release = _text(row.get("release_date"))
    try:
        if release is None:
            raise ValueError
        _utc(release)
    except ValueError:
        flags.append("invalid_release_date")
    result = {
        "instrument": "decam",
        "exposure_id": expnum,
        "ra_deg": ra,
        "dec_deg": dec,
        "start_utc": start,
        "midpoint_utc": midpoint,
        "start_mjd_utc": start_mjd,
        "midpoint_mjd_utc": midpoint_mjd,
        "exposure_seconds": duration,
        "filter": _text(row.get("ifilter")),
        "proposal": _text(row.get("proposal")),
        "observing_date": _text(row.get("caldat")),
        "proc_type": row["proc_type"],
        "release_date": release,
        "archive_updated_utc": updated,
        "archive_filename": row["archive_filename"],
        "original_filename": _text(row.get("original_filename")),
        "md5sum": row["md5sum"],
        "source_date_obs": _text(row.get("DATE-OBS")),
        "source_timesys": _text(row.get("TIMESYS")),
        "source_mjd_obs": _text(row.get("MJD-OBS")),
        "source_dateobs_center": _text(row.get("dateobs_center")),
        "quality_flags": flags,
    }
    return result, valid


def sidecar_path(output):
    return Path(str(output) + ".sqlite3")


@contextmanager
def writer_lock(output):
    """OS-held lock, automatically released after a crash; never unlink the lock file."""
    path = Path(str(output) + ".lock")
    with path.open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
                handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ArchiveError(f"Another updater holds {path}") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ArchiveError(f"Another updater holds {path}") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _open_store(path):
    con = sqlite3.connect(path)
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, SCHEMA_VERSION):
        con.close()
        raise ArchiveError(f"Unsupported DECam sidecar schema {version}")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS products (
            generation TEXT NOT NULL, filename TEXT NOT NULL, checksum TEXT NOT NULL,
            expnum INTEGER, valid INTEGER NOT NULL, priority INTEGER NOT NULL,
            updated TEXT NOT NULL, payload TEXT NOT NULL, source TEXT NOT NULL,
            PRIMARY KEY (generation, filename)
        );
        CREATE INDEX IF NOT EXISTS products_exposure ON products
            (generation, expnum, valid DESC, priority, updated DESC, filename, checksum);
        CREATE TABLE IF NOT EXISTS windows (
            generation TEXT NOT NULL, field TEXT NOT NULL, lower TEXT NOT NULL, upper TEXT NOT NULL,
            PRIMARY KEY (generation, field, lower, upper)
        );
        CREATE TABLE IF NOT EXISTS catalog (
            generation TEXT NOT NULL, expnum INTEGER NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY (generation, expnum)
        );
    """)
    con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    con.commit()
    return con


def _get(con, key, default=None):
    row = con.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _set(con, key, value):
    con.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (key, _json(value)))


def month_windows(field, lower, upper):
    while lower < upper:
        boundary = (
            lower.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + timedelta(days=32)
        ).replace(day=1)
        end = min(boundary, upper)
        if field in {"caldat", "release_date"}:
            yield field, lower.date().isoformat(), end.date().isoformat()
        else:
            yield field, _iso(lower), _iso(end)
        lower = end


def _windows(run):
    cutoff = _utc(run["cutoff"])
    if run["mode"] == "full":
        yield from month_windows("caldat", START, cutoff)
    else:
        lower = max(START, _utc(run["previous_sync"]) - timedelta(days=7))
        yield from month_windows("updated", lower, cutoff)
        yield from month_windows("release_date", lower, cutoff)


def _new_run(con, cutoff, full, api_version):
    active = _get(con, "active_generation")
    previous = _get(con, "last_sync")
    if previous and cutoff < _utc(previous):
        raise ArchiveError("Clock precedes the last successful synchronization")
    run = {
        "generation": uuid.uuid4().hex,
        "mode": "full" if full or not active else "incremental",
        "cutoff": _iso(cutoff),
        "previous_sync": previous,
        "phase": "fetch",
        "api_version": api_version,
    }
    with con:
        if run["mode"] == "incremental":
            con.execute(
                "INSERT INTO products SELECT ?, filename, checksum, expnum, valid, priority, updated, "
                "payload, source FROM products WHERE generation=?",
                (run["generation"], active),
            )
        _set(con, "pending_run", run)
    return run


def _ingest_window(con, client, generation, window, log):
    for attempt in range(3):
        try:
            count = 0
            with con:
                for row in client.window(*window):
                    candidate, valid = normalize(row)
                    con.execute(
                        """INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(generation, filename) DO UPDATE SET
                             checksum=excluded.checksum, expnum=excluded.expnum, valid=excluded.valid,
                             priority=excluded.priority, updated=excluded.updated,
                             payload=excluded.payload, source=excluded.source
                           WHERE excluded.updated >= products.updated""",
                        (
                            generation,
                            candidate["archive_filename"],
                            candidate["md5sum"],
                            candidate["exposure_id"],
                            int(valid),
                            PRIORITY[candidate["proc_type"]],
                            candidate["archive_updated_utc"] or "",
                            _json(candidate),
                            json.dumps(row, sort_keys=True),
                        ),
                    )
                    count += 1
                con.execute("INSERT INTO windows VALUES (?, ?, ?, ?)", (generation, *window))
            log(f"  {window[0]} {window[1]} → {window[2]}: {count:,} products")
            return
        except IncompleteWindow:
            if attempt == 2:
                raise
            log(f"  Archive window changed or was incomplete; retrying ({attempt + 1}/2)")


def _prepare_catalog(con, run):
    generation = run["generation"]
    active = _get(con, "active_generation", "")
    with con:
        con.execute("DELETE FROM catalog WHERE generation=?", (generation,))
        con.execute(
            """INSERT INTO catalog
               SELECT ?, expnum, payload FROM (
                   SELECT expnum, payload, ROW_NUMBER() OVER (
                       PARTITION BY expnum ORDER BY valid DESC, priority, updated DESC, filename, checksum
                   ) AS choice FROM products WHERE generation=? AND expnum IS NOT NULL
               ) WHERE choice=1""",
            (generation, generation),
        )
        added = con.execute(
            "SELECT COUNT(*) FROM catalog n LEFT JOIN catalog o ON o.generation=? AND o.expnum=n.expnum "
            "WHERE n.generation=? AND o.expnum IS NULL",
            (active, generation),
        ).fetchone()[0]
        changed = con.execute(
            "SELECT COUNT(*) FROM catalog n JOIN catalog o ON o.generation=? AND o.expnum=n.expnum "
            "WHERE n.generation=? AND n.payload != o.payload",
            (active, generation),
        ).fetchone()[0]
        removed = con.execute(
            "SELECT COUNT(*) FROM catalog o LEFT JOIN catalog n ON n.generation=? AND n.expnum=o.expnum "
            "WHERE o.generation=? AND n.expnum IS NULL",
            (generation, active),
        ).fetchone()[0]
        total = con.execute("SELECT COUNT(*) FROM catalog WHERE generation=?", (generation,)).fetchone()[0]
        excluded = con.execute(
            "SELECT COUNT(*) FROM products WHERE generation=? AND expnum IS NULL", (generation,)
        ).fetchone()[0]
        # Exact JSON serialization gives a cheap check without SQLite JSON-extension requirements.
        flagged = con.execute(
            "SELECT COUNT(*) FROM catalog WHERE generation=? AND payload NOT LIKE ?",
            (generation, '%"quality_flags": []%'),
        ).fetchone()[0]
        coverage = con.execute(
            "SELECT MIN(updated), MAX(updated) FROM products WHERE generation=? AND updated != ''",
            (generation,),
        ).fetchone()
        run.update(
            phase="export",
            summary={
                "added": added,
                "changed": changed,
                "removed": removed,
                "total": total,
                "excluded_products": excluded,
                "flagged_exposures": flagged,
                "archive_updates_from": coverage[0],
                "archive_updates_through": coverage[1],
            },
        )
        _set(con, "pending_run", run)


def _export(con, run, output):
    """Publish first, then checkpoint; a crash between them safely repeats this export."""
    metadata = {
        b"ponder.schema_version": str(SCHEMA_VERSION).encode(),
        b"ponder.generation": run["generation"].encode(),
        b"ponder.synced_through_utc": run["cutoff"].encode(),
        b"ponder.archive_url": ARCHIVE_URL.encode(),
        b"ponder.api_version": run["api_version"].encode(),
        b"ponder.summary": _json(run["summary"]).encode(),
        b"ponder.coverage_start": START.date().isoformat().encode(),
    }
    schema = SCHEMA.with_metadata(metadata)
    fd, name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        cursor = con.execute(
            "SELECT payload FROM catalog WHERE generation=? ORDER BY expnum", (run["generation"],)
        )
        with pq.ParquetWriter(temporary, schema, compression="zstd") as writer:
            while rows := cursor.fetchmany(10000):
                writer.write_table(pa.Table.from_pylist([json.loads(row[0]) for row in rows], schema=schema))
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, output)
        if os.name != "nt":
            directory = os.open(output.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    with con:
        _set(con, "active_generation", run["generation"])
        _set(con, "last_sync", run["cutoff"])
        _set(con, "last_summary", run["summary"])
        if run["mode"] == "full":
            _set(con, "last_full_refresh", run["cutoff"])
        _set(con, "pending_run", None)
        for table in ("products", "catalog", "windows"):
            con.execute(f"DELETE FROM {table} WHERE generation != ?", (run["generation"],))


def sync_catalog(output, *, full_refresh=False, client=None, now=None, log=print):
    """Synchronize once. An interrupted run is completed before starting newer work."""
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    cutoff = now or datetime.now(timezone.utc)
    if cutoff.tzinfo is None or cutoff < START:
        raise ValueError("Synchronization time must be timezone-aware and after 2012-01-01")
    with writer_lock(output):
        sidecar = sidecar_path(output)
        if output.exists() and not sidecar.exists() and not full_refresh:
            raise ArchiveError(
                "Parquet exists without its sidecar; restore the sidecar or use --full-refresh"
            )
        con = _open_store(sidecar)
        try:
            run = _get(con, "pending_run")
            if run and full_refresh and run["mode"] != "full":
                # Explicit full refresh supersedes unfinished incremental work, preserving the published data.
                with con:
                    for table in ("products", "catalog", "windows"):
                        con.execute(f"DELETE FROM {table} WHERE generation=?", (run["generation"],))
                    _set(con, "pending_run", None)
                run = None
            if run and run["phase"] == "export":
                log(f"Recovering pending Parquet export through {run['cutoff']}")
            else:
                client = client or ArchiveClient()
                version = client.preflight()
                if run is None:
                    run = _new_run(con, cutoff, full_refresh, version)
                log(f"{run['mode'].capitalize()} sync through {run['cutoff']} (NOIRLab API {version})")
                for window in _windows(run):
                    if con.execute(
                        "SELECT 1 FROM windows WHERE generation=? AND field=? AND lower=? AND upper=?",
                        (run["generation"], *window),
                    ).fetchone():
                        continue
                    _ingest_window(con, client, run["generation"], window, log)
                _prepare_catalog(con, run)
            _export(con, run, output)
            summary = run["summary"]
            log(
                f"Published {output}: {summary['total']:,} exposures; {summary['added']:,} added, "
                f"{summary['changed']:,} changed, {summary['removed']:,} removed; "
                f"synced through {run['cutoff']}"
            )
            if summary["excluded_products"] or summary["flagged_exposures"]:
                log(
                    f"WARNING: {summary['excluded_products']:,} products excluded for missing/invalid EXPNUM; "
                    f"{summary['flagged_exposures']:,} selected exposures have quality flags. "
                    "Inspect --status and the sidecar before treating coverage as complete."
                )
            if _utc(run["cutoff"]) < cutoff:
                log("Resumed the earlier run; run the command again to catch up to the present.")
            return summary
        finally:
            con.close()


def catalog_status(output):
    """Inspect only local files; never initialize a database or contact NOIRLab."""
    output = Path(output).expanduser().resolve()
    path = sidecar_path(output)
    status = {
        "output": str(output),
        "sidecar": str(path),
        "parquet_exists": output.exists(),
        "coverage_start": START.date().isoformat(),
    }
    if not path.exists():
        return {**status, "state": "missing_sidecar" if output.exists() else "not_initialized"}
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        if con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ArchiveError("Unsupported DECam sidecar schema")
        # Keep all the sidecar reads on one snapshot while another process may commit a window.
        con.execute("BEGIN")
        for key in ("last_sync", "last_full_refresh", "last_summary", "pending_run", "active_generation"):
            status[key] = _get(con, key)
        run = status["pending_run"]
        active = status["active_generation"]
        status["state"] = "pending" if run else ("ready" if active else "not_initialized")
        if run:
            status["completed_windows"] = con.execute(
                "SELECT COUNT(*) FROM windows WHERE generation=?", (run["generation"],)
            ).fetchone()[0]
        status["excluded_product_examples"] = [
            dict(archive_filename=name, quality_flags=json.loads(data)["quality_flags"])
            for name, data in con.execute(
                "SELECT filename, payload FROM products WHERE generation=? AND expnum IS NULL "
                "ORDER BY filename LIMIT 10",
                (active,),
            )
        ]
        if output.exists():
            meta = pq.read_metadata(output).metadata or {}
            status["parquet_generation"] = meta.get(b"ponder.generation", b"").decode()
            status["parquet_matches_sidecar"] = status["parquet_generation"] == active
            if not run and not status["parquet_matches_sidecar"]:
                status["state"] = "out_of_sync"
        elif active and not run:
            status["state"] = "missing_parquet"
        status["full_refresh_recommended"] = not status["last_full_refresh"] or datetime.now(
            timezone.utc
        ) - _utc(status["last_full_refresh"]) >= timedelta(days=30)
        return status
    finally:
        con.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="Local exposure Parquet path")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--full-refresh", action="store_true", help="Reconcile the entire archive")
    modes.add_argument("--status", action="store_true", help="Inspect local status without network access")
    args = parser.parse_args(argv)
    try:
        if args.status:
            print(json.dumps(catalog_status(args.output), indent=2))
        else:
            sync_catalog(args.output, full_refresh=args.full_refresh, log=lambda msg: print(msg, flush=True))
    except (ArchiveError, OSError, sqlite3.Error, ValueError, pa.ArrowException) as exc:
        print(f"DECam update failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; rerun the same command to resume.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
