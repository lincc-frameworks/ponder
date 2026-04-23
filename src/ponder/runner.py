import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .utils import *

# TODO: should be some sort of configurable
STATE_FILE = Path("state.json")
HASHES_FILE = Path("element_hashes.json")
WORK_DIR = Path("work")
RESULTS_DIR = Path("results")


def first_run_setup(objects, state, db_path):
    """If this is the first run, initialize state and hashes files."""
    print("First run — building baseline hashes.")
    prev_hashes = {obj_id(c): hash_orbit(c) for c in objects}
    state["last_mjd"] = 0.0
    HASHES_FILE.write_text(json.dumps(prev_hashes))
    STATE_FILE.write_text(json.dumps(state))
    print(f"  {len(prev_hashes)} comets hashed. Last MJD = {state['last_mjd']:.4f}")


def run_sorcha(orbits, physparams, output, db, config):
    print(output.with_suffix(""))
    subprocess.run(
        [
            "sorcha",
            "run",
            "-c",
            str(config),
            "--ob",
            str(orbits),
            "-p",
            str(physparams),
            "--pd",
            str(db),
            "-o",
            str(output.resolve().parent),
            "--ew",
            str(output.stem + "_ew"),
            "-t",
            str(output.stem),
            "-f",
        ],
        check=True,
    )


def setup_id_set(objects, ids, job_name, time, comet):
    if len(ids) == 0:
        print(f"  [skipping {job_name} — no objects to process]")
        return

    orbits_path = WORK_DIR / f"job_{job_name}_orbits.csv"
    physpar_path = WORK_DIR / f"job_{job_name}_physparams.csv"
    out_path = RESULTS_DIR / f"{time[:10]}_job_{job_name}.csv"

    if comet:
        orbs, phys = comets_to_sorcha_inputs(objects, ids)
    else:
        orbs, phys = mpcorb_to_sorcha_inputs(objects, ids)
    orbs.to_csv(orbits_path, index=False)
    phys.to_csv(physpar_path, index=False)

    return orbits_path, physpar_path, out_path


def run_ponder(
    db_path,
    object_path,
    config_path,
    comet,
):
    """Run Ponder on the given configs."""
    db_path = Path(db_path)
    object_path = Path(object_path)
    config_path = Path(config_path)

    WORK_DIR.mkdir(exist_ok=True)
    RESULTS_DIR.mkdir(exist_ok=True)

    # load persisted state
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"last_mjd": 0.0}
    prev_hashes = json.loads(HASHES_FILE.read_text()) if HASHES_FILE.exists() else {}

    objects = json.loads(object_path.read_text())

    if not prev_hashes:
        first_run_setup(objects, state, db_path)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n[{ts}] Starting cycle")

    # -- load and diff inputs --
    new_ids, updated_ids, unchanged_ids = diff_objects(objects, prev_hashes)
    print(f"  Comets — new: {len(new_ids)}  updated: {len(updated_ids)}  " f"unchanged: {len(unchanged_ids)}")

    new_pts_db = WORK_DIR / "new_pointings.db"
    n_new_pts = extract_new_pointings(db_path, state["last_mjd"], new_pts_db)
    total_pts = db_count(db_path)
    print(f"  Pointings — new: {n_new_pts}  total: {total_pts}")

    # TODO: better name
    #  -- job A: new pointings * unchanged + new objects --
    job_a_ids = set(unchanged_ids) | set(new_ids)
    job_a_inputs = setup_id_set(objects, job_a_ids, "A", ts, comet)
    if job_a_inputs:
        orbits, params, out_path = job_a_inputs
        run_sorcha(orbits, params, out_path, new_pts_db, config_path)

    updated_job_ids = setup_id_set(objects, updated_ids, "updated", ts, comet)
    if updated_job_ids:
        run_sorcha(*updated_job_ids, db_path, config_path)

    # -- persist state for next run --
    last_mjd = db_max_mjd(db_path)
    state["last_mjd"] = last_mjd
    STATE_FILE.write_text(json.dumps(state))
