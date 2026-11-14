#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
RAW_EXTRACT_DIR="$(cd "${PROJECT_DIR}/.." && pwd)"
ARTIFACT_HELPER="${RAW_EXTRACT_DIR}/artifact_io.py"
find_repo_root() {
  local dir="${PROJECT_DIR}"
  while [[ "${dir}" != "/" ]]; do
    if [[ -d "${dir}/Linux_CameraSDK-2.1.1_MediaSDK-3.1.1" || -d "${dir}/.git" ]]; then
      echo "${dir}"
      return 0
    fi
    dir="$(dirname "${dir}")"
  done
  echo "[error] could not find instaumi repo root from ${PROJECT_DIR}" >&2
  return 1
}

REPO_ROOT="$(find_repo_root)"
SDK_BUNDLE_DIR="${SDK_BUNDLE_DIR:-${REPO_ROOT}/Linux_CameraSDK-2.1.1_MediaSDK-3.1.1}"
BIN="${BIN:-${PROJECT_DIR}/build/insv_to_pano}"

inputs=()
OUTPUT="${OUTPUT:-}"

if [[ "$#" -ge 2 ]]; then
  OUTPUT="${!#}"
  for ((i = 1; i < $#; i++)); do
    inputs+=("${!i}")
  done
elif [[ -n "${INPUTS:-}" && -n "${OUTPUT}" ]]; then
  read -r -a inputs <<<"${INPUTS}"
elif [[ -n "${INPUT:-}" && -n "${OUTPUT}" ]]; then
  inputs+=("${INPUT}")
fi

WIDTH="${WIDTH:-3840}"
HEIGHT="${HEIGHT:-1920}"
BITRATE="${BITRATE:-67M}"
FLOWSTATE="${FLOWSTATE:-off}"
if [[ -z "${STITCH_TYPE:-}" ]]; then
  if [[ "${FASTEST_EXPORT:-0}" == "1" ]]; then
    STITCH_TYPE="template"
  elif [[ "${FAST_EXPORT:-0}" == "1" ]]; then
    STITCH_TYPE="dynamic"
  else
    STITCH_TYPE="optflow"
  fi
fi
STITCH_FUSION="${STITCH_FUSION:-off}"
DIRECTION_LOCK="${DIRECTION_LOCK:-off}"
MODEL_DIR="${MODEL_DIR:-}"
LOG_DIR="${LOG_DIR:-}"
FPS="${FPS:-60000/1001}"
SKIP_EXISTING_PANO="${SKIP_EXISTING_PANO:-1}"
ADOPT_LEGACY_PANO="${ADOPT_LEGACY_PANO:-0}"
PYTHON_BIN="${PYTHON:-python3}"

DEFAULT_MODEL_DIR="${SDK_BUNDLE_DIR}/libMediaSDK-dev-3.1.1.0-20250922_191110-amd64/models"
if [[ -z "${MODEL_DIR}" && -d "${DEFAULT_MODEL_DIR}" ]]; then
  MODEL_DIR="${DEFAULT_MODEL_DIR}"
fi

if [[ "${#inputs[@]}" -eq 0 || -z "${OUTPUT}" ]]; then
  echo "Usage: $0 input.insv [input2.insv ...] output.mp4" >&2
  echo "       or: INPUTS=input1.insv input2.insv OUTPUT=output.mp4 $0" >&2
  exit 1
fi
if [[ ! -x "${BIN}" ]]; then
  echo "[error] missing executable: ${BIN}" >&2
  echo "        Run scripts/build_ubuntu.sh first." >&2
  exit 1
fi
for input in "${inputs[@]}"; do
  if [[ ! -f "${input}" ]]; then
    echo "[error] missing input insv: ${input}" >&2
    exit 1
  fi
done

mkdir -p "$(dirname "${OUTPUT}")"
lock_path="${OUTPUT}.export.lock"
exec 9>"${lock_path}"
flock 9

artifact_recipe_args=(
  --width "${WIDTH}"
  --height "${HEIGHT}"
  --fps "${FPS}"
  --bitrate "${BITRATE}"
  --flowstate "${FLOWSTATE}"
  --stitch-type "${STITCH_TYPE}"
  --stitch-fusion "${STITCH_FUSION}"
  --direction-lock "${DIRECTION_LOCK}"
  --input-mode "${INSV_INPUT_MODE:-auto}"
)
for input in "${inputs[@]}"; do
  artifact_recipe_args+=(--input "${input}")
done

if [[ "${SKIP_EXISTING_PANO}" == "1" ]]; then
  check_args=(
    "${PYTHON_BIN}" "${ARTIFACT_HELPER}" pano-check
    --video "${OUTPUT}"
    "${artifact_recipe_args[@]}"
  )
  if [[ "${ADOPT_LEGACY_PANO}" == "1" ]]; then
    check_args+=(--adopt-legacy)
  fi
  if "${check_args[@]}"; then
    echo "[skip] complete panorama: ${OUTPUT}"
    exit 0
  fi
fi

partial_output="${OUTPUT%.mp4}.partial.mp4"
plan_path="${partial_output}.plan.json"
rm -f "${partial_output}"
"${PYTHON_BIN}" "${ARTIFACT_HELPER}" pano-plan \
  --plan "${plan_path}" \
  "${artifact_recipe_args[@]}"

cmd=(
  "${BIN}"
  --output "${partial_output}"
  --width "${WIDTH}"
  --height "${HEIGHT}"
  --bitrate "${BITRATE}"
  --flowstate "${FLOWSTATE}"
  --direction-lock "${DIRECTION_LOCK}"
  --stitch-type "${STITCH_TYPE}"
  --stitch-fusion "${STITCH_FUSION}"
)
for input in "${inputs[@]}"; do
  cmd+=(--input "${input}")
done
if [[ -n "${MODEL_DIR}" ]]; then
  cmd+=(--model-dir "${MODEL_DIR}")
fi
if [[ -n "${LOG_DIR}" ]]; then
  cmd+=(--log-dir "${LOG_DIR}")
fi

printf '+'
printf ' %q' "${cmd[@]}"
printf '\n'
"${cmd[@]}"

"${PYTHON_BIN}" "${ARTIFACT_HELPER}" pano-publish \
  --partial "${partial_output}" \
  --output "${OUTPUT}" \
  --plan "${plan_path}"

echo "[done] validated panorama published atomically: ${OUTPUT}"
