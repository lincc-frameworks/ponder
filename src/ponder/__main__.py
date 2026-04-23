import argparse
from datetime import datetime, timezone
from pathlib import Path

from .runner import run_ponder
from .utils import get_current_orbits


def main():
    parser = argparse.ArgumentParser(description="Run Ponder comet analysis")
    parser.add_argument("db_path", type=Path, help="Path to the database file")
    parser.add_argument("comets_path", type=Path, help="Path to the comets JSON file")
    parser.add_argument("config_path", type=Path, help="Path to the configuration file")
    parser.add_argument("--comet", action="store_true", help="Whether to run comet analysis")
    parser.add_argument(
        "--work-dir", type=Path, default=Path.cwd() / "work", help="Directory to store intermediate files"
    )
    parser.add_argument(
        "--download_orbits", action="store_true", help="Whether to download the latest orbits from MPC"
    )

    args = parser.parse_args()
    if args.download_orbits:
        date_str = datetime.now(timezone.utc).strftime("%d-%m-%Y")
        get_current_orbits(date_str, args.work_dir, comet=args.comet)
    if args.comet:
        print("Running comet analysis")
    else:
        print("Running asteroid analysis")
    run_ponder(args.db_path, args.comets_path, args.config_path, comet=args.comet)


if __name__ == "__main__":
    main()
