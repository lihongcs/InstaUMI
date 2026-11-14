#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 2 || ! -d "$1" || -z "$2" ]]; then
  echo "Usage: bash $0 /path/to/bimanual_insv_session 'Task description'" >&2
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export INPUT_DIR="$(cd "$1" && pwd -P)"
export TASK_PROMPT="$2"
export PYTHON="${PYTHON:-python3}"

if [[ -f "${INPUT_DIR}/uvc_capture_complete.json" ]]; then
  echo "This exporter requires an INSV/HTC session. For USB sessions, see dataprocess/README.md." >&2
  exit 2
fi

"${PYTHON}" "${script_dir}/pipeline_preflight.py" --profile ordinary
"${PYTHON}" "${script_dir}/inspect_capture_session.py" "${INPUT_DIR}" --require-media

DIRECT_IMAGE_MODE=1 MATERIALIZE_FRONT_VIDEO=0 CLOCK_CALIBRATION_REQUIRED=1 \
  bash "${script_dir}/_01_raw_extrect/run_01_raw_extract_biarm.sh"

ADAPTIVE_CPU_LIMIT="${ADAPTIVE_CPU_LIMIT:-0}" RAW_INSV_INPUT=1 ACTIVE_SEGMENTS=1 \
  bash "${script_dir}/_03_gripper_estimation/run_03_gripper_estimation_biarm.sh"

DIRECT_OPENPI_IMAGES=1 DIRECT_IMAGE_FRONT_CACHE="${DIRECT_IMAGE_FRONT_CACHE:-off}" \
  bash "${script_dir}/_05_convert_bimanual_lerobot/run_05_convert_bimanual_lerobot_htc.sh" "${INPUT_DIR}"

"${PYTHON}" "${script_dir}/audit_lerobot_sync.py" "${OUTPUT_ROOT:-${INPUT_DIR}/lerobot}" \
  --require-embedded-calibration
