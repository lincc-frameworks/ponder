
# ponder



[![Template](https://img.shields.io/badge/Template-LINCC%20Frameworks%20Python%20Project%20Template-brightgreen)](https://lincc-ppt.readthedocs.io/en/latest/)

[![PyPI](https://img.shields.io/pypi/v/ponder?color=blue&logo=pypi&logoColor=white)](https://pypi.org/project/ponder/)
[![GitHub Workflow Status](https://img.shields.io/github/actions/workflow/status/dirac-institute/ponder/smoke-test.yml)](https://github.com/dirac-institute/ponder/actions/workflows/smoke-test.yml)
[![Codecov](https://codecov.io/gh/dirac-institute/ponder/branch/main/graph/badge.svg)](https://codecov.io/gh/dirac-institute/ponder)

This project was automatically generated using the LINCC-Frameworks 
[python-project-template](https://github.com/lincc-frameworks/python-project-template).

A repository badge was added to show that this project uses the python-project-template, however it's up to
you whether or not you'd like to display it!

For more information about the project template see the 
[documentation](https://lincc-ppt.readthedocs.io/en/latest/).

## Maintaining a DECam pointing catalog

After installing Ponder (`pip install .` from this checkout), run:

```bash
# First run builds the catalog; subsequent runs update it.
ponder-decam-pointings --output /path/to/decam_pointings.parquet

# Reconcile the complete archive, including removed or backdated products.
ponder-decam-pointings --output /path/to/decam_pointings.parquet --full-refresh

# Inspect local freshness, coverage, validation issues, and unfinished work.
ponder-decam-pointings --output /path/to/decam_pointings.parquet --status
```

The command uses the anonymous [NOIRLab Astro Data Archive metadata
API](https://astroarchive.noirlab.edu/api/docs/). It does not download images,
require a NOIRLab account, or depend on HARVEST's database. The first run queries
monthly observation-date windows from January 2012 onward and can take a while;
progress is printed after each completed window. The archive controls which
metadata are visible, and some visible exposures have images that are still
proprietary. No public-image release-date cutoff is applied.

The Parquet has one row per `(instrument, exposure_id)`, where `exposure_id` is
DECam's `EXPNUM`. It includes all filters for `obs_type=object`,
`proc_type=raw/instcal/resampled`, and `prod_type=image/image1`. Stacks,
calibration frames, masks, and weights are excluded. Among candidates with valid
coordinates, timing, and duration, selection prefers `instcal`, then `raw`, then
`resampled`. Within a processing type it chooses the latest archive update,
then ascending archive filename and checksum for deterministic ties. This is a
metadata-selection rule, not a guarantee that a product is the best scientific
reduction. If all candidates for an exposure have invalid pointing metadata,
the exposure remains in the catalog with nullable values and `quality_flags`.

Read it directly with pandas:

```python
import pandas as pd

pointings = pd.read_parquet("/path/to/decam_pointings.parquet")
usable = pointings[pointings["quality_flags"].map(len) == 0]
print(usable[["exposure_id", "ra_deg", "dec_deg", "start_mjd_utc", "filter"]])
```

The stable v1 columns are:

| Columns | Meaning |
| --- | --- |
| `instrument`, `exposure_id` | Instrument and positive integer DECam exposure counter |
| `ra_deg`, `dec_deg` | Archive-provided field center in degrees; not a CCD footprint |
| `start_utc`, `midpoint_utc` | UTC ISO strings with fractional seconds and `Z`; strings also preserve leap seconds |
| `start_mjd_utc`, `midpoint_mjd_utc` | Corresponding MJD values in **UTC**, not TAI |
| `exposure_seconds`, `filter`, `proposal`, `observing_date` | Exposure duration, full original filter string, proposal, local observing-night date |
| `proc_type`, `release_date`, `archive_updated_utc` | Selected product's processing, image-release date, and archive update time |
| `archive_filename`, `original_filename`, `md5sum` | Selected product provenance |
| `source_date_obs`, `source_timesys`, `source_mjd_obs`, `source_dateobs_center` | Source timing values retained as strings |
| `quality_flags` | List of validation issues, empty when checks pass |

Start time comes from `DATE-OBS` interpreted using `TIMESYS`; midpoint comes
from the archive's `dateobs_center`. Missing or unknown time systems are flagged
instead of assumed to be UTC. A midpoint inconsistent with start plus half the
duration by more than one second is flagged. Exposure numbers that are missing,
nonintegral, or outside the positive int64 range are quarantined in the sidecar,
not matched by approximate timestamps. Such excluded products are counted in
the command summary and `--status`, with up to ten examples in status output.
Other quality flags identify invalid coordinates, timing, duration, update
timestamps, or release dates. Review flags before using the catalog scientifically.

### Updates, reconciliation, and recovery

Keep `decam_pointings.parquet.sqlite3` alongside the Parquet. This private
sidecar stores candidate products, original metadata, validation flags, and
completed-window checkpoints. Allow disk space for both the published and
pending generations in SQLite plus a temporary Parquet during export. Parquet
is written in bounded batches, so the archive need not fit in memory. The
adjacent `.lock` file prevents simultaneous updaters for the same output path;
the OS releases the lock after a crash. Do not delete the lock file while an
updater is running. Use a filesystem with reliable SQLite and file-lock support.

Incremental runs search archive **update dates**, with a seven-day overlap, and
release dates. This catches metadata corrections, reprocessing, and newly
visible older observations. Windows are paginated with explicit ordering and
before/after count checks; incomplete or changing windows are rolled back and
retried. NOIRLab does not provide a transactional snapshot across requests, so
these checks cannot detect every simultaneous change. Run `--full-refresh`
monthly to reconcile removals, backdated changes, and other changes missed by
the incremental queries. `--status` recommends reconciliation after 30 days.
The command does not install a schedule.

Rerun the same command after interruption. Completed windows are reused, and
the previous Parquet remains readable until the replacement is fully written
and atomically published. A failed export resumes from committed local data
without contacting NOIRLab. A resumed run uses its original cutoff; run again
afterward to catch up to the present. An explicit full refresh supersedes an
unfinished incremental run; an unfinished full refresh resumes normally.

The Parquet footer records the schema version, archive API version, generation,
sync cutoff, and summary. `--status` reports whether it matches the sidecar. If
the sidecar is lost, restore it from backup or explicitly use `--full-refresh`
to rebuild it; the existing Parquet is preserved until rebuilding succeeds.
To inspect quarantined original records, open the sidecar read-only and query
`products` with `expnum IS NULL`; its `source` and `payload` columns contain JSON.
During an unfinished update, filter by the `active_generation` shown in status
to inspect the published generation.

This catalog is not yet an input for `ponder --db`. Sorcha-compatible SQLite
export, DECam footprints, and observing-condition mappings are separate work.

Tests run offline against recorded metadata and simulated archive responses.
To opt into the small live historical-window smoke test:

```bash
PONDER_LIVE_NOIRLAB=1 python -m pytest tests/ponder/test_decam_pointings.py -k live_noirlab
```

## Running Ponder

Ponder runs Sorcha against an orbit catalog and a pointing database:

```bash
ponder --config ../sorcha_ponder_config.ini --orbits work/asteroid_orbits_04-05-2026.json --db from_rubin_dp1.db
```

For MPCORB asteroid catalogs, Ponder filters the input catalog by default before
building Sorcha inputs. It keeps objects with semimajor axis greater than 30 au,
uncertainty code no greater than 6, or an observation arc of at least 3 days. If
the catalog row includes arc years, the object is treated as having a long enough
observation arc. Comet catalogs are not filtered by this default MPCORB rule. Use
`--no-filter-orbits` to disable this filter.

Sorcha execution is chunked by default so long runs can resume after failures.
The default chunk size is 5000 rows. Completed chunks are marked with `.done`
files and are skipped on later runs with the same inputs. Ponder shows a tqdm
progress bar for the current batch set, including the number of workers, and it
combines all completed chunks into the usual output CSVs when the full job
finishes. If a chunk fails, Ponder now splits it into 250-row debug chunks by
default, recursively isolates remaining failures to single catalog rows, and
combines the successful parent/debug ranges into the final output while leaving
the skipped rows in the debug reports.

Per-chunk work files and Sorcha outputs live under `work/chunk_runs/` and
`results/chunk_runs/` so the top-level `results/` directory stays readable.
Ponder keeps the authoritative combined files in the digest-scoped run directory
and also exposes hard links, or copies if hard links are not available, at
`results/<date>_job_<job>.csv` and `results/<date>_job_<job>_ew.csv` when no
conflicting top-level file already exists.

For incremental runs, Ponder separates the Sorcha jobs by the work they need to
cover. Unchanged objects run only against newly added pointings, new objects run
against the full pointing database, and updated objects also run against the full
pointing database. When there are no new pointings, the unchanged-object job is
skipped. State and object-hash files are scoped by pointing database and object
mode, so comet and asteroid runs can share one pointing database without sharing
the same incremental baseline. Chunked outputs are written inside the
digest-specific result directory so separate pointing database contexts do not
overwrite or resume each other's results.

Each run also stores the input MPC catalog as a gzipped JSON snapshot under
`results/catalogs/` and records that path in each job manifest. Ponder adds a
`ponder_catalog_row` column to catalog-row reports so row-number references can
be traced back to the saved snapshot.

Useful chunking options:

```bash
ponder --config ../sorcha_ponder_config.ini --orbits work/asteroid_orbits_04-05-2026.json --db from_rubin_dp1.db \
  --chunk-size 5000 --sorcha-workers 2 --sorcha-timeout 900
```

- `--chunk-size 0` runs one legacy, unchunked Sorcha job.
- `--sorcha-workers N` runs up to `N` Sorcha chunks in parallel.
- `--sorcha-timeout SECONDS` applies a per-chunk timeout.
- `--debug-failed-chunk-size N` changes the automatic failed-chunk debug size
  from the default 250 rows. Use `0` with `--no-isolate-failing-rows` to disable
  automatic debug splitting.
- `--no-isolate-failing-rows` stops after the first failed-chunk debug pass
  instead of recursively isolating bad rows.
- `--no-resume-chunks` reruns chunks even when completed markers exist.
- `--only-chunks 12,18-20` runs selected chunk indices for debugging and skips
  final combine and state updates.

When chunks fail, Ponder writes a failure summary and the associated original
catalog rows under the chunk result directory:

- `results/chunk_runs/<date>_job_<job>_<digest>/failures.csv`
- `results/chunk_runs/<date>_job_<job>_<digest>/failed_catalog_rows.csv`

When chunks finish and are combined, Ponder audits the per-chunk Sorcha outputs
against the combined result files by object ID and timestamp, then writes:

- `results/chunk_runs/<date>_job_<job>_<digest>/output_audit.csv`
- `results/chunk_runs/<date>_job_<job>_<digest>/missing_output_pairs.csv`

If any chunk output object/timestamp pairs are absent from the combined
detection or ephemeris files, `missing_output_pairs.csv` lists the object,
timestamp, missing count, and `ponder_catalog_row`.

The failed catalog CSV keeps the original JSON columns and adds chunk metadata,
so it can be inspected directly. When recursive isolation identifies specific
bad rows, prefer `debug/failing_rows.csv` as the narrow ignore list. To skip
known bad objects on a later run, pass either a file or repeated object IDs:

```bash
ponder --config ../sorcha_ponder_config.ini --orbits work/asteroid_orbits_04-05-2026.json --db from_rubin_dp1.db \
  --ignore-objects results/chunk_runs/<date>_job_<job>_<digest>/debug/failing_rows.csv
```

```bash
ponder --config ../sorcha_ponder_config.ini --orbits work/asteroid_orbits_04-05-2026.json --db from_rubin_dp1.db \
  --ignore-object K23A00A --ignore-object K23A01B
```

Failed 5000-row chunks are automatically narrowed to smaller groups during the
same run. To use a different first-pass debug size:

```bash
ponder --config ../sorcha_ponder_config.ini --orbits work/asteroid_orbits_04-05-2026.json --db from_rubin_dp1.db \
  --sorcha-timeout 900 --debug-failed-chunk-size 100
```

Debug subchunks get their own progress bar and write:

- `results/chunk_runs/<date>_job_<job>_<digest>/debug/subchunk_debug_report.csv`
- `results/chunk_runs/<date>_job_<job>_<digest>/debug/failed_subchunk_catalog_rows.csv`

If Sorcha completes a debug or isolation range but emits no output CSV because
there are no rows to write, Ponder treats that as a successful zero-output range
and skips the missing file during final combine.

To tidy an older run directory that predates the `chunk_runs/` layout, run a dry
run first and then apply it:

```bash
ponder-consolidate-chunks /path/to/pointing_dbs
ponder-consolidate-chunks /path/to/pointing_dbs --apply
```

The maintenance command consolidates complete manifest-backed runs, exposes
audited combined files in top-level `results/`, moves digest-scoped result and
work directories under `chunk_runs/`, and files old top-level Sorcha logs under
`results/logs/`. It does not delete chunk artifacts.

By default, Ponder keeps subdividing failed debug ranges until it identifies
individual failing rows. If you already know which parent chunks failed, add
`--force-debug-chunking` with `--only-chunks` to skip the 5000-row parent timeout
and go directly to resumable debug ranges:

```bash
ponder --config ../sorcha_ponder_config.ini --orbits work/asteroid_orbits_04-05-2026.json --db from_rubin_dp1.db \
  --only-chunks 289-291,301 \
  --sorcha-timeout 900 \
  --debug-failed-chunk-size 250 \
  --sorcha-workers 3 \
  --force-debug-chunking
```

Recursive isolation writes additive reports in the debug directory:

- `isolation_report.csv` lists every tested range and whether it ran, resumed,
  completed, or failed.
- `failing_rows.csv` contains only size-1 ranges that still fail, with the
  original catalog columns and absolute input row.
- `group_failures.csv` lists failed ranges whose smaller child ranges passed.
- `group_failure_catalog_rows.csv` lists the original catalog rows covered by
  those group-only failures.
- `debug_timing_summary.csv` summarizes timing by debug level and row count.

## Dev Guide - Getting Started

Before installing any dependencies or writing code, it's a great idea to create a
virtual environment. LINCC-Frameworks engineers primarily use `conda` to manage virtual
environments. If you have conda installed locally, you can run the following to
create and activate a new environment.

```
>> conda create -n <env_name> python=3.11
>> conda activate <env_name>
```

Once you have created a new environment, you can install this project for local
development using the following commands:

```
>> ./.setup_dev.sh
>> conda install pandoc
```

Notes:
1. `./.setup_dev.sh` will initialize pre-commit for this local repository, so
   that a set of tests will be run prior to completing a local commit. For more
   information, see the Python Project Template documentation on 
   [pre-commit](https://lincc-ppt.readthedocs.io/en/latest/practices/precommit.html)
