#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
HELPER="${SCRIPT_DIR}/png_sequence_artifact.py"
BIN="${BIN:-${PROJECT_DIR}/build/insv_to_png_sequence}"
PYTHON_BIN="${PYTHON:-python3}"
FFPROBE_BIN="${FFPROBE_BIN:-ffprobe}"

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

usage() {
  cat >&2 <<EOF
Usage:
  $0 --input chapter_001.insv [--input chapter_002.insv ...] \\
     --output-dir /path/to/panorama_png --frames '0,5,10-20[:2]'

Frame ranges are inclusive and indices are global over the ordered chapters.
The final files are lossless PNGs named frame_000000000000.png, etc.

Inputs can also be supplied with INPUTS, OUTPUT_DIR and FRAMES environment variables.
Useful environment overrides: WIDTH, HEIGHT, CHUNK_SIZE, PARTIAL_DIR, MODEL_DIR,
STITCH_TYPE, FLOWSTATE, DIRECTION_LOCK, STITCH_FUSION, CUDA, TIMEOUT_SEC, BIN.
EOF
}

inputs=()
frame_values=()
frame_specs=()
frame_files=()
OUTPUT_DIR="${OUTPUT_DIR:-}"
PARTIAL_DIR="${PARTIAL_DIR:-}"

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --input|-i)
      [[ "$#" -ge 2 ]] || { echo "[error] missing value for $1" >&2; exit 1; }
      inputs+=("$2")
      shift 2
      ;;
    --output-dir|-o)
      [[ "$#" -ge 2 ]] || { echo "[error] missing value for $1" >&2; exit 1; }
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --partial-dir)
      [[ "$#" -ge 2 ]] || { echo "[error] missing value for $1" >&2; exit 1; }
      PARTIAL_DIR="$2"
      shift 2
      ;;
    --frame)
      [[ "$#" -ge 2 ]] || { echo "[error] missing value for $1" >&2; exit 1; }
      frame_values+=("$2")
      shift 2
      ;;
    --frames)
      [[ "$#" -ge 2 ]] || { echo "[error] missing value for $1" >&2; exit 1; }
      frame_specs+=("$2")
      shift 2
      ;;
    --frames-file)
      [[ "$#" -ge 2 ]] || { echo "[error] missing value for $1" >&2; exit 1; }
      frame_files+=("$2")
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "[error] unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ "${#inputs[@]}" -eq 0 && -n "${INPUTS:-}" ]]; then
  read -r -a inputs <<<"${INPUTS}"
fi
if [[ "${#frame_specs[@]}" -eq 0 && "${#frame_values[@]}" -eq 0 && -n "${FRAMES:-}" ]]; then
  frame_specs+=("${FRAMES}")
fi
if [[ "${#frame_files[@]}" -eq 0 && -n "${FRAMES_FILE:-}" ]]; then
  frame_files+=("${FRAMES_FILE}")
fi
if [[ "${#inputs[@]}" -eq 0 || -z "${OUTPUT_DIR}" ]]; then
  usage
  exit 1
fi
if [[ "${#frame_specs[@]}" -eq 0 && "${#frame_values[@]}" -eq 0 && "${#frame_files[@]}" -eq 0 ]]; then
  echo "[error] at least one --frame, --frames or --frames-file is required" >&2
  exit 1
fi
for input in "${inputs[@]}"; do
  [[ -f "${input}" ]] || { echo "[error] missing input chapter: ${input}" >&2; exit 1; }
done

WIDTH="${WIDTH:-3840}"
HEIGHT="${HEIGHT:-1920}"
CHUNK_SIZE="${CHUNK_SIZE:-256}"
STITCH_TYPE="${STITCH_TYPE:-optflow}"
FLOWSTATE="${FLOWSTATE:-off}"
DIRECTION_LOCK="${DIRECTION_LOCK:-off}"
STITCH_FUSION="${STITCH_FUSION:-off}"
CUDA="${CUDA:-on}"
TIMEOUT_SEC="${TIMEOUT_SEC:-0}"
MODEL_DIR="${MODEL_DIR:-}"
LOG_DIR="${LOG_DIR:-}"

REPO_ROOT="$(find_repo_root)"
SDK_BUNDLE_DIR="${SDK_BUNDLE_DIR:-${REPO_ROOT}/Linux_CameraSDK-2.1.1_MediaSDK-3.1.1}"
DEFAULT_MODEL_DIR="${SDK_BUNDLE_DIR}/libMediaSDK-dev-3.1.1.0-20250922_191110-amd64/models"
if [[ -z "${MODEL_DIR}" && -d "${DEFAULT_MODEL_DIR}" ]]; then
  MODEL_DIR="${DEFAULT_MODEL_DIR}"
fi

OUTPUT_DIR="$(realpath -m "${OUTPUT_DIR}")"
if [[ -z "${PARTIAL_DIR}" ]]; then
  PARTIAL_DIR="${OUTPUT_DIR}.partial"
else
  PARTIAL_DIR="$(realpath -m "${PARTIAL_DIR}")"
fi
mkdir -p "$(dirname "${OUTPUT_DIR}")"

lock_path="${OUTPUT_DIR}.export.lock"
exec 9>"${lock_path}"
flock 9

prepare_cmd=(
  "${PYTHON_BIN}" "${HELPER}" prepare
  --output-dir "${OUTPUT_DIR}"
  --partial-dir "${PARTIAL_DIR}"
  --width "${WIDTH}"
  --height "${HEIGHT}"
  --stitch-type "${STITCH_TYPE}"
  --flowstate "${FLOWSTATE}"
  --direction-lock "${DIRECTION_LOCK}"
  --stitch-fusion "${STITCH_FUSION}"
  --cuda "${CUDA}"
  --model-dir "${MODEL_DIR}"
  --ffprobe-bin "${FFPROBE_BIN}"
)
for input in "${inputs[@]}"; do
  prepare_cmd+=(--input "${input}")
done
for value in "${frame_values[@]}"; do
  prepare_cmd+=(--frame "${value}")
done
for spec in "${frame_specs[@]}"; do
  prepare_cmd+=(--frames "${spec}")
done
for path in "${frame_files[@]}"; do
  prepare_cmd+=(--frames-file "${path}")
done

status="$("${prepare_cmd[@]}")"
if [[ "${status}" == "COMPLETE" ]]; then
  exit 0
fi
PLAN_PATH="${status}"
if [[ ! -x "${BIN}" ]]; then
  echo "[error] missing executable: ${BIN}" >&2
  echo "        Run ${SCRIPT_DIR}/build_ubuntu.sh first." >&2
  exit 1
fi

missing_output="$(
  "${PYTHON_BIN}" "${HELPER}" missing \
    --plan "${PLAN_PATH}" \
    --partial-dir "${PARTIAL_DIR}" \
    --chunk-size "${CHUNK_SIZE}"
)"

batch_number=0
if [[ -n "${missing_output}" ]]; then
  while IFS=$'\t' read -r chapter_index global_frames local_frames; do
    [[ -n "${chapter_index}" ]] || continue
    if (( chapter_index < 0 || chapter_index >= ${#inputs[@]} )); then
      echo "[error] plan returned invalid chapter index: ${chapter_index}" >&2
      exit 1
    fi
    batch_dir="${PARTIAL_DIR}/.instaumi_partial/sdk_runs/chapter_${chapter_index}_$$_${batch_number}"
    rm -rf "${batch_dir}"
    mkdir -p "${batch_dir}"

    cmd=(
      "${BIN}"
      --input "${inputs[chapter_index]}"
      --output-dir "${batch_dir}"
      --frames "${local_frames}"
      --width "${WIDTH}"
      --height "${HEIGHT}"
      --stitch-type "${STITCH_TYPE}"
      --flowstate "${FLOWSTATE}"
      --direction-lock "${DIRECTION_LOCK}"
      --stitch-fusion "${STITCH_FUSION}"
      --cuda "${CUDA}"
      --timeout-sec "${TIMEOUT_SEC}"
    )
    if [[ -n "${MODEL_DIR}" ]]; then
      cmd+=(--model-dir "${MODEL_DIR}")
    fi
    if [[ -n "${LOG_DIR}" ]]; then
      cmd+=(--log-dir "${LOG_DIR}")
    fi

    echo "[batch] chapter=${chapter_index} count=$(awk -F, '{print NF}' <<<"${local_frames}")"
    printf '+'
    printf ' %q' "${cmd[@]}"
    printf '\n'
    "${cmd[@]}"

    "${PYTHON_BIN}" "${HELPER}" ingest \
      --plan "${PLAN_PATH}" \
      --partial-dir "${PARTIAL_DIR}" \
      --batch-dir "${batch_dir}" \
      --chapter-index "${chapter_index}" \
      --global-frames "${global_frames}" \
      --local-frames "${local_frames}"
    batch_number=$((batch_number + 1))
  done <<<"${missing_output}"
fi

"${PYTHON_BIN}" "${HELPER}" publish \
  --plan "${PLAN_PATH}" \
  --partial-dir "${PARTIAL_DIR}" \
  --output-dir "${OUTPUT_DIR}" \
  --ffprobe-bin "${FFPROBE_BIN}"
