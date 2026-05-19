import argparse
from datetime import datetime, timezone
from pathlib import Path

from .runner import run_ponder
from .utils import get_current_orbits


def main():
    parser = argparse.ArgumentParser(description="Run Ponder comet analysis")
    parser.add_argument("--db", type=Path, help="Path to the database file")
    parser.add_argument("--orbits", type=Path, help="Path to the orbits JSON file")
    parser.add_argument("--config", type=Path, help="Path to the configuration file")
    parser.add_argument("--comet", action="store_true", help="Whether to run comet analysis")
    parser.add_argument(
        "--work-dir", type=Path, default=Path.cwd() / "work", help="Directory to store intermediate files"
    )
    parser.add_argument(
        "--download_orbits", action="store_true", help="Whether to download the latest orbits from MPC"
    )

    args = parser.parse_args()
    orbit_path = args.orbits
    if args.download_orbits:
        date_str = datetime.now(timezone.utc).strftime("%d-%m-%Y")
        orbit_path = get_current_orbits(date_str, args.work_dir, comet=args.comet)
    if not orbit_path:
        print("Error: Must provide --orbits or --download_orbits")
        return
    if args.db is None:
        if args.download_orbits:
            print(f"No database supplied; downloaded orbits to {orbit_path}. Skipping Ponder analysis.")
        else:
            print("Error: Must provide --db to run Ponder analysis")
        return
    if args.comet:
        print("Running comet analysis")
    else:
        print("Running asteroid analysis")
    run_ponder(args.db, orbit_path, args.config, comet=args.comet)


if __name__ == "__main__":
    main()
