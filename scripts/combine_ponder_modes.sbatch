#!/usr/bin/env bash
#SBATCH --job-name=ponder-combine-modes
#SBATCH --account=rubin:commissioning
#SBATCH --partition=roma
#SBATCH --time=08:00:00
#SBATCH --cpus-per-task=1
#SBATCH --mem=32G
#SBATCH --output=/sdf/scratch/rubin/kbmod/ponder/logs/ponder-combine-%j.out
#SBATCH --error=/sdf/scratch/rubin/kbmod/ponder/logs/ponder-combine-%j.out

set -euo pipefail

PONDER_REPO="${PONDER_REPO:-/sdf/home/c/colinc/repos/ponder}"
ASTEROID_RESULTS_DIR="${ASTEROID_RESULTS_DIR:-/sdf/scratch/rubin/kbmod/ponder/asteroids/results}"
COMET_RESULTS_DIR="${COMET_RESULTS_DIR:-/sdf/scratch/rubin/kbmod/ponder/comets/results}"
OUTPUT_RESULTS_DIR="${OUTPUT_RESULTS_DIR:-/sdf/scratch/rubin/kbmod/ponder/combined/results}"
RUN_DATE="${RUN_DATE:-}"

export PYTHONPATH="${PONDER_REPO}/src:${PYTHONPATH:-}"

command=(python -m ponder.mode_combine
  --asteroid-results-dir "${ASTEROID_RESULTS_DIR}"
  --comet-results-dir "${COMET_RESULTS_DIR}"
  --output-results-dir "${OUTPUT_RESULTS_DIR}"
)
if [[ -n "${RUN_DATE}" ]]; then
  command+=(--run-date "${RUN_DATE}")
fi

printf 'Command:'
printf ' %q' "${command[@]}"
printf '\n'
"${command[@]}"
