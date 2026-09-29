#!/usr/bin/env bash
#SBATCH --job-name=rcc-ponder-combined
#SBATCH --account=rubin:commissioning
#SBATCH --partition=roma
#SBATCH --time=24:00:00
#SBATCH --cpus-per-task=12
#SBATCH --mem=256G
#SBATCH --output=/sdf/scratch/rubin/kbmod/ponder/logs/rcc-ponder-combined-%j.out
#SBATCH --error=/sdf/scratch/rubin/kbmod/ponder/logs/rcc-ponder-combined-%j.out

set -euo pipefail

PONDER_REPO="${PONDER_REPO:-/sdf/home/c/colinc/repos/ponder}"
RCC_REPO="${RCC_REPO:-/sdf/home/c/colinc/repos/Rubin-Comet-Catchers}"
RCC_DATA_ROOT="${RCC_DATA_ROOT:-/sdf/scratch/rubin/kbmod/users/colinc/rcc/data}"
COMBINED_RESULTS_DIR="${COMBINED_RESULTS_DIR:-/sdf/scratch/rubin/kbmod/ponder/combined/results}"
PONDER_RESULTS_DIR="${PONDER_RESULTS_DIR:-/sdf/scratch/rubin/kbmod/ponder/comets/results}"
RCC_INSTRUMENT_ROOT="${RCC_INSTRUMENT_ROOT:-${RCC_DATA_ROOT}/nightly/lsstcam}"
LEDGER="${LEDGER:-${RCC_DATA_ROOT}/ponder_updates/comet_run_ledger.parquet}"
SUMMARY_JSON="${SUMMARY_JSON:-${RCC_DATA_ROOT}/ponder_updates/comet_last_update_summary.json}"
UNIFIED_WORKERS="${UNIFIED_WORKERS:-12}"
START_OBSNIGHT="${START_OBSNIGHT:-2025-04-15}"
END_OBSNIGHT="${END_OBSNIGHT:-2026-07-14}"

export PYTHONPATH="${RCC_REPO}:${PONDER_REPO}/src:${PYTHONPATH:-}"

# The full asteroid+comet parquet is the audited archival product.  RCC already
# contains the asteroid baseline, so nightly ingestion applies only the small
# comet delta before rebuilding unified products.
test -f "${COMBINED_RESULTS_DIR}/2026-08-24_job_new.parquet"

python "${PONDER_REPO}/scripts/backfill_rcc_object_mode.py" \
  "${RCC_INSTRUMENT_ROOT}" --object-mode asteroid --apply

python "${PONDER_REPO}/scripts/update_rcc_ponder_sources_only.py" \
  --data-root "${RCC_DATA_ROOT}" \
  --ponder-results-dir "${PONDER_RESULTS_DIR}" \
  --ledger "${LEDGER}" \
  --summary-json "${SUMMARY_JSON}" \
  --start-obsnight "${START_OBSNIGHT}" \
  --end-obsnight "${END_OBSNIGHT}"

python -m rubin_comet_catchers.unified_results \
  "${RCC_INSTRUMENT_ROOT}" \
  --recursive \
  --services skybot jpl ponder \
  --max-workers "${UNIFIED_WORKERS}" \
  --no-propagate-source-updates

# RCC reports per-night worker failures as warnings, so assert that every
# selected output is fresh and every Ponder-bearing night retains object_mode.
python "${PONDER_REPO}/scripts/audit_rcc_unified_results.py" \
  "${RCC_INSTRUMENT_ROOT}"
