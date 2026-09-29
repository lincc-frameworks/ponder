#!/usr/bin/env bash
#SBATCH --job-name=rcc-ponder-only-comets
#SBATCH --account=rubin:commissioning
#SBATCH --partition=roma
#SBATCH --time=12:00:00
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --output=/sdf/scratch/rubin/kbmod/ponder/logs/rcc-ponder-only-comets-%j.out
#SBATCH --error=/sdf/scratch/rubin/kbmod/ponder/logs/rcc-ponder-only-comets-%j.out

set -euo pipefail

RCC_REPO="${RCC_REPO:-/sdf/home/c/colinc/repos/Rubin-Comet-Catchers}"
CUTOUT_INPUT="${CUTOUT_INPUT:?Set CUTOUT_INPUT to the Ponder-only representative parquet}"
THUMBNAIL_OUTDIR="${THUMBNAIL_OUTDIR:-/sdf/scratch/rubin/kbmod/users/colinc/rcc/ponder_only_comets_20260824}"
LOOKUP_WORKERS="${LOOKUP_WORKERS:-8}"

export PYTHONPATH="${RCC_REPO}:${PYTHONPATH:-}"

python "${PONDER_REPO:-/sdf/home/c/colinc/repos/ponder}/scripts/generate_ponder_cutouts.py" \
  "${CUTOUT_INPUT}" \
  --instrument LSSTCam \
  --thumbnail-outdir "${THUMBNAIL_OUTDIR}" \
  --lookup-workers "${LOOKUP_WORKERS}" \
  --to-write png fits \
  --dims 600 600
