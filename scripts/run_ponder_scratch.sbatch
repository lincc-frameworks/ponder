#!/usr/bin/env bash
#SBATCH --job-name=ponder-baseline
#SBATCH --account=rubin:commissioning
#SBATCH --partition=roma
#SBATCH --time=48:00:00
#SBATCH --cpus-per-task=4
#SBATCH --mem=64G
#SBATCH --output=/sdf/scratch/rubin/kbmod/ponder/logs/ponder-%j.out
#SBATCH --error=/sdf/scratch/rubin/kbmod/ponder/logs/ponder-%j.out
#SBATCH --chdir=/sdf/scratch/rubin/kbmod/ponder

set -euo pipefail

PONDER_REPO="${PONDER_REPO:-/sdf/home/c/colinc/repos/ponder}"
PONDER_RUN_DIR="${PONDER_RUN_DIR:-/sdf/scratch/rubin/kbmod/ponder}"
PONDER_DB="${PONDER_DB:-${PONDER_RUN_DIR}/pointing_dbs/pointings.sqlite}"
PONDER_CONFIG="${PONDER_CONFIG:-${PONDER_RUN_DIR}/sorcha_ponder_config.ini}"
PONDER_WORK_DIR="${PONDER_WORK_DIR:-${PONDER_RUN_DIR}/work}"
MPLCONFIGDIR="${MPLCONFIGDIR:-${PONDER_RUN_DIR}/mplconfig}"

UPDATE_MODE="${UPDATE_MODE:-auto}"
CHUNK_SIZE="${CHUNK_SIZE:-5000}"
SORCHA_WORKERS="${SORCHA_WORKERS:-2}"
SORCHA_TIMEOUT="${SORCHA_TIMEOUT:-3600}"
DEBUG_FAILED_CHUNK_SIZE="${DEBUG_FAILED_CHUNK_SIZE:-250}"
DOWNLOAD_ORBITS="${DOWNLOAD_ORBITS:-1}"
ORBITS="${ORBITS:-}"
ONLY_CHUNKS="${ONLY_CHUNKS:-}"
COMET="${COMET:-0}"
NO_FILTER_ORBITS="${NO_FILTER_ORBITS:-0}"

mkdir -p "${PONDER_RUN_DIR}/logs" "${PONDER_WORK_DIR}" "${MPLCONFIGDIR}"
cd "${PONDER_RUN_DIR}"

export MPLCONFIGDIR
export PYTHONPATH="${PONDER_REPO}/src:${PYTHONPATH:-}"

echo "[$(date --iso-8601=seconds)] Starting Ponder Slurm run"
echo "host=$(hostname)"
echo "job_id=${SLURM_JOB_ID:-}"
echo "repo=${PONDER_REPO}"
echo "run_dir=${PONDER_RUN_DIR}"
echo "db=${PONDER_DB}"
echo "config=${PONDER_CONFIG}"
echo "work_dir=${PONDER_WORK_DIR}"
echo "update_mode=${UPDATE_MODE}"
echo "chunk_size=${CHUNK_SIZE}"
echo "sorcha_workers=${SORCHA_WORKERS}"
echo "sorcha_timeout=${SORCHA_TIMEOUT}"
echo "only_chunks=${ONLY_CHUNKS}"
echo "download_orbits=${DOWNLOAD_ORBITS}"

if [[ ! -f "${PONDER_DB}" ]]; then
  echo "Missing pointing DB: ${PONDER_DB}" >&2
  exit 2
fi

if [[ ! -f "${PONDER_CONFIG}" ]]; then
  echo "Missing Sorcha config: ${PONDER_CONFIG}" >&2
  exit 2
fi

command=(python -m ponder
  --db "${PONDER_DB}"
  --config "${PONDER_CONFIG}"
  --work-dir "${PONDER_WORK_DIR}"
  --update-mode "${UPDATE_MODE}"
  --chunk-size "${CHUNK_SIZE}"
  --sorcha-workers "${SORCHA_WORKERS}"
  --sorcha-timeout "${SORCHA_TIMEOUT}"
  --debug-failed-chunk-size "${DEBUG_FAILED_CHUNK_SIZE}"
)

if [[ "${DOWNLOAD_ORBITS}" == "1" ]]; then
  command+=(--download_orbits)
else
  if [[ -z "${ORBITS}" ]]; then
    echo "DOWNLOAD_ORBITS=0 requires ORBITS=/path/to/orbits.json" >&2
    exit 2
  fi
  command+=(--orbits "${ORBITS}")
fi

if [[ -n "${ONLY_CHUNKS}" ]]; then
  command+=(--only-chunks "${ONLY_CHUNKS}")
fi

if [[ "${COMET}" == "1" ]]; then
  command+=(--comet)
fi

if [[ "${NO_FILTER_ORBITS}" == "1" ]]; then
  command+=(--no-filter-orbits)
fi

printf 'Command:'
printf ' %q' "${command[@]}"
printf '\n'

"${command[@]}"
echo "[$(date --iso-8601=seconds)] Ponder Slurm run finished"
