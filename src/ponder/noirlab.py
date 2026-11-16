"""Small, bounded client for NOIRLab's anonymous file-metadata API."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

ARCHIVE_URL = "https://astroarchive.noirlab.edu"
CORE_FIELDS = [
    "instrument",
    "obs_type",
    "proc_type",
    "prod_type",
    "EXPNUM",
    "ra_center",
    "dec_center",
    "dateobs_center",
    "exposure",
    "ifilter",
    "proposal",
    "caldat",
    "release_date",
    "updated",
    "archive_filename",
    "original_filename",
    "md5sum",
]
SOURCE_FIELDS = ["DATE-OBS", "TIMESYS", "MJD-OBS"]
OUTFIELDS = CORE_FIELDS + SOURCE_FIELDS
SELECTION = [
    ["instrument", "decam"],
    ["obs_type", "object"],
    ["proc_type", "raw", "instcal", "resampled"],
    ["prod_type", "image", "image1"],
]


class ArchiveError(RuntimeError):
    """An archive response cannot safely be ingested."""


class IncompleteWindow(ArchiveError):
    """Retry the whole window, rolling back all its records first."""


class ArchiveClient:
    def __init__(self, *, session=None, page_size=5000, attempts=4, sleep=time.sleep):
        if page_size < 1 or attempts < 1:
            raise ValueError("page_size and attempts must be positive")
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "ponder-decam-pointings/1"})
        self.page_size = page_size
        self.attempts = attempts
        self.sleep = sleep

    def _request(self, method, path, **kwargs):
        for attempt in range(self.attempts):
            try:
                response = self.session.request(method, ARCHIVE_URL + path, timeout=(10, 60), **kwargs)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == self.attempts - 1:
                    raise ArchiveError(f"NOIRLab connection failed: {exc}") from exc
                self.sleep(min(2**attempt, 30))
                continue
            if response.status_code in {429, 500, 502, 503, 504}:
                if attempt == self.attempts - 1:
                    raise ArchiveError(f"NOIRLab returned HTTP {response.status_code}; retry later")
                delay = min(2**attempt, 30)
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        try:
                            until = parsedate_to_datetime(retry_after)
                            delay = max(delay, (until - datetime.now(timezone.utc)).total_seconds())
                        except (TypeError, ValueError):
                            pass
                if delay > 60:
                    raise ArchiveError(f"NOIRLab requests a {delay:.0f}s pause; rerun later")
                self.sleep(delay)
                continue
            try:
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                raise ArchiveError(f"Invalid NOIRLab response for {path}: {exc}") from exc
        raise AssertionError("unreachable")

    def preflight(self):
        """Check capabilities rather than inheriting HARVEST's API-5 version guard."""
        version = self._request("GET", "/api/version")
        core = self._request("GET", "/api/adv_search/core_file_fields/")
        try:
            core_names = {row["Field"] for row in core}
            required = set(CORE_FIELDS) - {"EXPNUM"}
            if not required <= core_names:
                raise ArchiveError(f"Missing archive fields: {sorted(required - core_names)}")
            for proc in ("raw", "instcal", "resampled"):
                aux = self._request("GET", f"/api/adv_search/aux_file_fields/decam/{proc}/")
                names = core_names | {row["Field"] for row in aux}
                missing = set(OUTFIELDS) - names
                if missing:
                    raise ArchiveError(f"Missing {proc} fields: {sorted(missing)}")
        except (TypeError, KeyError) as exc:
            raise ArchiveError("Malformed archive field catalog") from exc
        return str(version)

    def _search(self, search, **params):
        data = self._request(
            "POST",
            "/api/adv_search/find/",
            params={"rectype": "file", "format": "json", "sort": "md5sum", **params},
            json={"outfields": OUTFIELDS, "search": search},
        )
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise ArchiveError("Expected a NOIRLab metadata envelope followed by records")
        if not {"META", "HEADER", "PARAMETERS"} <= data[0].keys():
            raise ArchiveError("Missing NOIRLab response envelope")
        if not all(isinstance(row, dict) for row in data[1:]):
            raise ArchiveError("Malformed NOIRLab records")
        return data[1:]

    def _count(self, search):
        rows = self._search(search, count="Y", limit=1)
        if len(rows) != 1 or type(rows[0].get("count")) is not int or rows[0]["count"] < 0:
            raise ArchiveError("Malformed NOIRLab match count")
        return rows[0]["count"]

    def window(self, field, lower, upper):
        """Yield bounded pages; consumers must roll back if final validation fails.

        Archive ranges are inclusive. Neighboring windows intentionally overlap;
        the local product key removes repeated boundary records.
        """
        search = SELECTION + [[field, lower, upper]]
        expected = self._count(search)
        offset = 0
        previous = ""
        while offset < expected:
            rows = self._search(search, limit=self.page_size, offset=offset)
            if not rows or len(rows) > min(self.page_size, expected - offset):
                raise IncompleteWindow("Archive page length disagrees with match count")
            for original in rows:
                # API 7 includes duplicate file: aliases. Reject contradictory aliases.
                row = dict(original)
                for key, value in original.items():
                    if key.startswith("file:"):
                        name = key[5:]
                        if name in row and row[name] != value:
                            raise ArchiveError(f"Conflicting archive aliases for {name}")
                        row[name] = value
                        del row[key]
                checksum = row.get("md5sum")
                if not isinstance(checksum, str) or not checksum:
                    raise ArchiveError("Archive record has no checksum")
                if checksum <= previous:
                    raise IncompleteWindow("Repeated or unsorted archive page")
                previous = checksum
                yield row
            offset += len(rows)
        if self._count(search) != expected:
            raise IncompleteWindow("Archive changed during pagination")
