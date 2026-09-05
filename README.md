
# ponder

### Comets with unknown brightness

Comet H is optional: an otherwise usable orbit is retained and its `H_r`
is null/NaN when H is missing. No brightness is imputed. To retain such
objects in the final predictions, use a geometry-only Sorcha configuration:
omit `mag_limit`, `snr_limit`, and `bright_limit`; set `default_snr_cut`,
`randomization_on`, `vignetting_on`, and `trailing_losses_on` to False;
disable fading and linking filters. Numeric magnitude cuts reject unknowns.
Required epoch and perihelion dates are still mandatory. Use a fresh run
directory when recovering objects excluded by an earlier version; old
completion state does not automatically invalidate on a code change.



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

## Running Ponder

Ponder runs Sorcha against an orbit catalog and a pointing database:

```bash
ponder --config ../sorcha_ponder_config.ini --orbits work/asteroid_orbits_04-05-2026.json --db from_rubin_dp1.db
```

## Exporting Rubin Pointings

Ponder also ships `ponder-exposures-exporter`, a Ponder-adjacent convenience
command for querying Rubin Butler exposure records and writing pointing inputs.
It scans one or more Butler repos, deduplicates pointings by exposure start
time, writes a full CSV, writes a reduced export CSV, and can write both a
Sorcha-formatted observations CSV and a SQLite pointing database.

The exporter needs an LSST stack environment with `lsst.daf.butler` available
when querying Butler. When running from a source checkout, prepend `src` to the
existing `PYTHONPATH` instead of replacing it so LSST stack paths remain
available:

```bash
PYTHONPATH=src:$PYTHONPATH python -m ponder_tools.exposures_exporter \
  --full-csv /path/to/pointing_dbs/pointings_full.csv \
  --export-csv /path/to/pointing_dbs/pointings_export.csv \
  --sorcha-csv /path/to/pointing_dbs/pointings_sorcha.csv \
  --sqlite-db /path/to/pointing_dbs/pointings.sqlite
```

If a Sorcha-formatted pointing CSV already exists, create or refresh only the
SQLite DB without querying Butler:

```bash
PYTHONPATH=src:$PYTHONPATH python -m ponder_tools.exposures_exporter \
  --sqlite-source-csv /path/to/pointing_dbs/pointings_sorcha.csv \
  --sqlite-db /path/to/pointing_dbs/pointings.sqlite
```

The SQLite output contains an `observations` table with the column names used by
the Ponder Sorcha config: `observationId`, `observationStartMJD`, `visitTime`,
`visitExposureTime`, `band`, `seeingFwhmGeom`, `seeingFwhmEff`,
`fiveSigmaDepth`, `fieldRA`, `fieldDec`, and `rotSkyPos`.

## Running the Scratch Baseline on Slurm

The production scratch run can be launched with:

```bash
sbatch scripts/run_ponder_scratch.sbatch
```

The default script targets `/sdf/scratch/rubin/kbmod/ponder`, uses
`pointing_dbs/pointings.sqlite` and `sorcha_ponder_config.ini`, downloads current
MPC orbits, runs `--update-mode auto`, and uses two Sorcha workers with 5000-row
chunks. Logs are written to `/sdf/scratch/rubin/kbmod/ponder/logs/ponder-%j.out`.

To run a tiny synthetic Slurm smoke job before the full baseline, point the same
script at a one-object scratch workspace:

```bash
PONDER_RUN_DIR=/sdf/scratch/rubin/kbmod/ponder/slurm_smoke \
PONDER_DB=/sdf/scratch/rubin/kbmod/ponder/slurm_smoke/pointings.sqlite \
PONDER_CONFIG=/sdf/scratch/rubin/kbmod/ponder/slurm_smoke/sorcha_ponder_config.ini \
PONDER_WORK_DIR=/sdf/scratch/rubin/kbmod/ponder/slurm_smoke/work \
DOWNLOAD_ORBITS=0 \
ORBITS=/sdf/scratch/rubin/kbmod/ponder/slurm_smoke/objects.json \
CHUNK_SIZE=10 \
SORCHA_WORKERS=1 \
SORCHA_TIMEOUT=300 \
sbatch --job-name=ponder-smoke --time=00:30:00 --cpus-per-task=1 --mem=16G \
  scripts/run_ponder_scratch.sbatch
```

To run a later newly discovered objects pass against the same pointing DB and
saved baseline:

```bash
UPDATE_MODE=new-objects sbatch scripts/run_ponder_scratch.sbatch
```

Completed chunks are resumable through their `.done` markers under
`results/chunk_runs/`. If a Slurm job times out before Ponder writes final state,
relaunch the same `sbatch` command; completed chunks are skipped, failed chunks
resume into debug/isolation handling, and pending chunks continue.

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

## DP1 Heliocentric-Distance Reports

Use `scripts/dp1_rh_report.py` to summarize a combined Ponder ephemeris file by
object for the DP1 distant-object audit:

The maintained central-raft cone configuration is
`configs/dp1_comcam_circle_0p50.ini`. Its 0.50 degree radius encloses all nine
LSSTComCam sensors while keeping Sorcha's 0.20 degree ephemeris buffer separate
from the science footprint.

```bash
PYTHONPATH=src:$PYTHONPATH python scripts/dp1_rh_report.py \
  --ephemeris /path/to/results/2026-07-15_job_new_ew.parquet \
  --db /path/to/pointings_dp1_pre2025.sqlite \
  --orbits /path/to/asteroid_orbits_15-07-2026.json \
  --config /path/to/sorcha_ponder_config.ini \
  --object-mode asteroid \
  --out-dir /path/to/reports
```

The script writes `dp1_rh_all_by_object.csv` for the full pre-cutoff object
summary, `dp1_rh_lt5_by_object.csv` for objects with at least one possible image
below 5 au, `dp1_rh_gt5_by_object.csv`, `dp1_rh_ge5_reduced_by_object.csv`, and
`dp1_rh_report.md`. It defaults the detection file to the ephemeris path without
`_ew`, so `2026-07-15_job_new_ew.parquet` pairs with
`2026-07-15_job_new.parquet`; pass `--detections` to override that path.
The report reads the circular FOV radius from `--config` and applies that
strict pointing-centered cone to the buffered ephemeris output before any
object summaries are calculated. Use `--object-mode comet` for an MPC comet
catalog; comet output IDs are matched through `Designation_and_name`.
Apparent magnitude is derived from `H_r`, the run's fixed Sorcha color offsets,
and `phase_function = none` distances.
Mean positional uncertainty is read from matched Ponder detections as
`astrometricSigma_deg * 3600` and may be blank for ephemeris-only rows without a
matched detection. Local obsnight is derived from the `YYYYMMDD` prefix of the
pointing `observationId`. Filter lists are formatted in Rubin `u,g,r,i,z,y`
order.

For incremental runs, Ponder separates the Sorcha jobs by the work they need to
cover. The default `--update-mode auto` preserves the full incremental cycle:
unchanged objects run only against newly added pointings, new objects run against
the full pointing database, and updated objects also run against the full
pointing database. When there are no new pointings, the unchanged-object job is
skipped. State and object-hash files are scoped by pointing database and object
mode, so comet and asteroid runs can share one pointing database without sharing
the same incremental baseline. Chunked outputs are written inside the
digest-specific result directory so separate pointing database contexts do not
overwrite or resume each other's results.

Use `--update-mode new-objects` when the only intended update is newly
discovered objects. This mode downloads or reads the MPC orbit file exactly like
the default mode, diffs it against the existing object-hash baseline, runs only
object IDs that are absent from that baseline against the full pointing
database, and skips both new-pointing work for unchanged objects and updated
orbit work for existing objects. Because it does not process existing objects
against new pointings, it does not advance `last_mjd`; it only adds hashes for
the newly processed object IDs. Each run writes
`results/<date>_job_new_objects_report.csv` with the selected object IDs, run
timestamp, mode, object type, DB/config paths, catalog snapshot path, and status.

Orbit-change-triggered reruns currently use element hashes in the default auto
mode. A future configurable threshold mode should store the prior element values
themselves, not only hashes, so users can choose which columns and deltas count
as meaningful orbit improvement.

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


## NEO-only runs

Use `--neo` with an MPCORB catalog to select asteroids with the standard
`q = a(1-e) < 1.3 au` criterion. The population cut is always applied;
`--no-filter-orbits` disables only Ponder's additional orbit-quality filter. NEO state
and chunk-resume digests use a separate `neos` namespace, so they cannot collide
with full-asteroid or comet runs against the same pointing database.

```bash
ponder --neo --db pointings.db --orbits mpcorb_extended.json --config sorcha.ini \
  --no-filter-orbits
```
