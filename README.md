
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
finishes.

Useful chunking options:

```bash
ponder --config ../sorcha_ponder_config.ini --orbits work/asteroid_orbits_04-05-2026.json --db from_rubin_dp1.db \
  --chunk-size 5000 --sorcha-workers 2 --sorcha-timeout 900
```

- `--chunk-size 0` runs one legacy, unchunked Sorcha job.
- `--sorcha-workers N` runs up to `N` Sorcha chunks in parallel.
- `--sorcha-timeout SECONDS` applies a per-chunk timeout.
- `--no-resume-chunks` reruns chunks even when completed markers exist.
- `--only-chunks 12,18-20` runs selected chunk indices for debugging and skips
  final combine and state updates.

When chunks fail, Ponder writes a failure summary and the associated original
catalog rows under the chunk result directory:

- `results/<date>_job_<job>_<digest>/failures.csv`
- `results/<date>_job_<job>_<digest>/failed_catalog_rows.csv`

The failed catalog CSV keeps the original JSON columns and adds chunk metadata,
so it can be inspected directly or reused as an ignore list. To skip known bad
objects on a later run, pass either a file or repeated object IDs:

```bash
ponder --config ../sorcha_ponder_config.ini --orbits work/asteroid_orbits_04-05-2026.json --db from_rubin_dp1.db \
  --ignore-objects results/<date>_job_<job>_<digest>/failed_catalog_rows.csv
```

```bash
ponder --config ../sorcha_ponder_config.ini --orbits work/asteroid_orbits_04-05-2026.json --db from_rubin_dp1.db \
  --ignore-object K23A00A --ignore-object K23A01B
```

To narrow a failed 5000-row chunk down to smaller groups during the same run, use
subchunk debugging:

```bash
ponder --config ../sorcha_ponder_config.ini --orbits work/asteroid_orbits_04-05-2026.json --db from_rubin_dp1.db \
  --sorcha-timeout 900 --debug-failed-chunk-size 250
```

Debug subchunks get their own progress bar and write:

- `results/<date>_job_<job>_<digest>/debug/subchunk_debug_report.csv`
- `results/<date>_job_<job>_<digest>/debug/failed_subchunk_catalog_rows.csv`

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
