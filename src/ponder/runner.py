import json
from pathlib import Path

from .utils import *

# TODO: should be some sort of configurable
STATE_FILE = Path("state.json")
HASHES_FILE = Path("element_hashes.json")
WORK_DIR = Path("work")
RESULTS_DIR = Path("results")


def first_run_setup(comets_path, state, db_path):
    """If this is the first run, initialize state and hashes files."""
    print("First run — building baseline hashes.")
    comets = json.loads(comets_path.read_text())
    prev_hashes = {obj_id(c): hash_orbit(c) for c in comets}
    state["last_mjd"] = db_max_mjd(db_path)
    HASHES_FILE.write_text(json.dumps(prev_hashes))
    STATE_FILE.write_text(json.dumps(state))
    print(f"  {len(prev_hashes)} comets hashed. Last MJD = {state['last_mjd']:.4f}")
    print("  Re-run the script tomorrow with the next night's files.")


def run_ponder(
    db_path,
    comets_path,
    config_path,
):
    """Run Ponder on the given configs."""
    db_path = Path(db_path)
    comets_path = Path(comets_path)
    config_path = Path(config_path)

    WORK_DIR.mkdir(exist_ok=True)
    RESULTS_DIR.mkdir(exist_ok=True)

    # load persisted state
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"last_mjd": 0.0}
    prev_hashes = json.loads(HASHES_FILE.read_text()) if HASHES_FILE.exists() else {}

    if not prev_hashes:
        first_run_setup(comets_path, state, db_path)
