import argparse
from pathlib import Path

from .runner import run_ponder


def main():
    parser = argparse.ArgumentParser(description="Run Ponder comet analysis")
    parser.add_argument("db_path", type=Path, help="Path to the database file")
    parser.add_argument("comets_path", type=Path, help="Path to the comets JSON file")
    parser.add_argument("config_path", type=Path, help="Path to the configuration file")
    parser.add_argument("--comet", action="store_true", help="Whether to run comet analysis")

    args = parser.parse_args()
    if args.comet:
        print("Running comet analysis")
    run_ponder(args.db_path, args.comets_path, args.config_path, comet=args.comet)


if __name__ == "__main__":
    main()
