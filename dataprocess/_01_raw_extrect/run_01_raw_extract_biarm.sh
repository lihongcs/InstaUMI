#!/usr/bin/env bash
set -euo pipefail

# Parse this complete long-running stage before starting either arm.  The live
# job then cannot observe a partially updated source file hours later.
main() {

# Bimanual raw extraction wrapper.
#
# This script intentionally does not modify the single-arm run_01_raw_extract.sh
# or extract_raw_views_imu.py. It creates two tiny staging input directories so
# the existing single-arm extractor can be reused once for each arm.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
WORKSPACE_ROOT="$(cd "${REPO_ROOT}/.." && pwd)"
if [[ -z "${DATA_ROOT:-}" ]]; then
  if [[ -d "${WORKSPACE_ROOT}/data" ]]; then
    DATA_ROOT="${WORKSPACE_ROOT}/data"
  elif [[ -d "${WORKSPACE_ROOT}/Data" ]]; then
    DATA_ROOT="${WORKSPACE_ROOT}/Data"
  else
    DATA_ROOT="${WORKSPACE_ROOT}/data"
  fi
fi

if [[ -z "${INPUT_DIR:-}" ]]; then
  latest_biarm_dir="$(find "${DATA_ROOT}" -maxdepth 1 -type d \( -name "pp_biarm_*" -o -name "biarm_pp_*" \) -print 2>/dev/null | sort | tail -n 1)"
  INPUT_DIR="${latest_biarm_dir:-${DATA_ROOT}/instaumi/biarm_pp_630}"
fi

# A completed USB/UVC capture is a distinct source family.  Dispatch before
# any INSV discovery/MediaSDK setup so the UVC path cannot be mistaken for an
# INSV capture.  The dedicated entry reads files only and never opens a camera.
if [[ -f "${INPUT_DIR}/uvc_capture_complete.json" ]]; then
  exec env INPUT_DIR="${INPUT_DIR}" \
    TMP_ROOT="${TMP_ROOT:-${INPUT_DIR}/tmp}" \
    bash "${SCRIPT_DIR}/run_01_uvc_extract_biarm.sh"
fi
TMP_ROOT="${TMP_ROOT:-${INPUT_DIR}/tmp}"
MODEL_IMAGE_WIDTH="${MODEL_IMAGE_WIDTH:-224}"
MODEL_IMAGE_HEIGHT="${MODEL_IMAGE_HEIGHT:-224}"
PANO_EXPORT_DIR="${PANO_EXPORT_DIR:-${SCRIPT_DIR}/insta360_pano_export}"
ARTIFACT_HELPER="${SCRIPT_DIR}/artifact_io.py"
EXPORT_PANO="${EXPORT_PANO:-auto}" # auto|1|0
DIRECT_IMAGE_MODE="${DIRECT_IMAGE_MODE:-0}"
MATERIALIZE_FRONT_VIDEO="${MATERIALIZE_FRONT_VIDEO:-1}"
if [[ "${DIRECT_IMAGE_MODE}" != "0" && "${DIRECT_IMAGE_MODE}" != "1" ]]; then
  echo "[error] DIRECT_IMAGE_MODE must be 0 or 1" >&2
  exit 2
fi
if [[ "${MATERIALIZE_FRONT_VIDEO}" != "0" && "${MATERIALIZE_FRONT_VIDEO}" != "1" ]]; then
  echo "[error] MATERIALIZE_FRONT_VIDEO must be 0 or 1" >&2
  exit 2
fi
if [[ "${DIRECT_IMAGE_MODE}" != "1" && "${MATERIALIZE_FRONT_VIDEO}" == "0" ]]; then
  echo "[error] MATERIALIZE_FRONT_VIDEO=0 requires DIRECT_IMAGE_MODE=1" >&2
  exit 2
fi
if [[ "${DIRECT_IMAGE_MODE}" == "1" ]]; then
  # Side images are stitched only after the calibrated 60 Hz training-frame
  # plan is known.  Do not create a lossy full-session panorama cache here.
  EXPORT_PANO=0
fi
SKIP_EXISTING_PANO="${SKIP_EXISTING_PANO:-1}"
WAIT_EXISTING_EXPORT="${WAIT_EXISTING_EXPORT:-1}"
ADOPT_LEGACY_PANO="${ADOPT_LEGACY_PANO:-0}"
PANO_WIDTH="${WIDTH:-3840}"
PANO_HEIGHT="${HEIGHT:-1920}"
PANO_FPS="${FPS:-60000/1001}"
PANO_BITRATE="${BITRATE:-67M}"
PANO_FLOWSTATE="${FLOWSTATE:-off}"
PANO_STITCH_FUSION="${STITCH_FUSION:-off}"
PANO_DIRECTION_LOCK="${DIRECTION_LOCK:-off}"
FAST_PIPELINE="${FAST_PIPELINE:-1}"
PARALLEL_ARMS_WAS_SET="${PARALLEL_ARMS+x}"
FRONT_SEGMENT_WORKERS_WAS_SET="${FRONT_SEGMENT_WORKERS+x}"
if [[ "${FAST_PIPELINE}" == "1" ]]; then
  FAST_EXTRACT="${FAST_EXTRACT:-1}"
  SKIP_EXISTING_VIDEOS="${SKIP_EXISTING_VIDEOS:-1}"
fi
SKIP_EXISTING_VIDEOS="${SKIP_EXISTING_VIDEOS:-1}"
FAST_EXPORT="${FAST_EXPORT:-0}"
FASTEST_EXPORT="${FASTEST_EXPORT:-0}"
FAST_EXTRACT="${FAST_EXTRACT:-0}"
if [[ -n "${STITCH_TYPE:-}" ]]; then
  PANO_STITCH_TYPE="${STITCH_TYPE}"
elif [[ "${FASTEST_EXPORT}" == "1" ]]; then
  PANO_STITCH_TYPE="template"
elif [[ "${FAST_EXPORT}" == "1" ]]; then
  PANO_STITCH_TYPE="dynamic"
else
  PANO_STITCH_TYPE="optflow"
fi

if [[ "${FAST_EXTRACT}" == "1" ]]; then
  PARALLEL_ARMS="${PARALLEL_ARMS:-1}"
  PARALLEL_SIDE_VIEWS="${PARALLEL_SIDE_VIEWS:-1}"
  PARALLEL_FRONT_SEGMENTS="${PARALLEL_FRONT_SEGMENTS:-1}"
fi
FFMPEG_PRESET="${FFMPEG_PRESET:-veryfast}"
PARALLEL_ARMS="${PARALLEL_ARMS:-0}"
PARALLEL_FRONT_SEGMENTS="${PARALLEL_FRONT_SEGMENTS:-0}"
FRONT_SEGMENT_WORKERS="${FRONT_SEGMENT_WORKERS:-0}"
VIDEO_QUALITY="${VIDEO_QUALITY:-0}"
FFMPEG_BIN="${FFMPEG_BIN:-$(command -v ffmpeg)}"
VIDEO_ENCODER="${VIDEO_ENCODER:-libx264rgb}"
SOURCE_COPY_MODE="${SOURCE_COPY_MODE:-auto}"
CALIBRATE_CLOCK="${CALIBRATE_CLOCK:-auto}" # auto|1|0
CLOCK_CALIBRATION_REQUIRED="${CLOCK_CALIBRATION_REQUIRED:-0}"
REUSE_ACCEPTED_CLOCK_CALIBRATION="${REUSE_ACCEPTED_CLOCK_CALIBRATION:-1}"
CLOCK_CALIBRATION_ARM_WORKERS="${CLOCK_CALIBRATION_ARM_WORKERS:-2}"
if [[ ! "${CLOCK_CALIBRATION_ARM_WORKERS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "[error] CLOCK_CALIBRATION_ARM_WORKERS must be a positive integer" >&2
  exit 2
fi
if [[ -z "${PYTHON:-}" ]]; then
  if [[ -n "${CONDA_PREFIX:-}" && "$(basename "${CONDA_PREFIX}")" == "instaumi" && -x "${CONDA_PREFIX}/bin/python" ]]; then
    PYTHON="${CONDA_PREFIX}/bin/python"
  elif [[ -x "${HOME}/miniconda3/envs/instaumi/bin/python" ]]; then
    PYTHON="${HOME}/miniconda3/envs/instaumi/bin/python"
  elif command -v python >/dev/null 2>&1; then
    PYTHON="$(command -v python)"
  else
    PYTHON="$(command -v python3)"
  fi
fi

CLOCK_CALIBRATION_OUTPUT_DIR="${CLOCK_CALIBRATION_OUTPUT_DIR:-${BIARM_TMP_ROOT:-${TMP_ROOT}}/sync_calibration}"
CLOCK_CALIBRATION_REPORT="${CLOCK_CALIBRATION_OUTPUT_DIR}/calibration.json"
CLOCK_CALIBRATION_STATUS="not_run"

BIARM_SESSION="${BIARM_SESSION:-$(basename "${INPUT_DIR}")}"
BIARM_LAYOUT="${BIARM_LAYOUT:-grouped}"
if [[ "${BIARM_LAYOUT}" == "legacy" ]]; then
  BIARM_TMP_ROOT="${BIARM_TMP_ROOT:-${TMP_ROOT}}"
  LEFT_EPISODE="${LEFT_EPISODE:-${BIARM_SESSION}_left}"
  RIGHT_EPISODE="${RIGHT_EPISODE:-${BIARM_SESSION}_right}"
  STAGING_ROOT="${STAGING_ROOT:-${TMP_ROOT}/_01_raw_extract_biarm_inputs/${BIARM_SESSION}}"
else
  BIARM_TMP_ROOT="${BIARM_TMP_ROOT:-${TMP_ROOT}}"
  LEFT_EPISODE="${LEFT_EPISODE:-left}"
  RIGHT_EPISODE="${RIGHT_EPISODE:-right}"
  STAGING_ROOT="${STAGING_ROOT:-${BIARM_TMP_ROOT}/_01_raw_extract_biarm_inputs}"
fi
LEFT_RAW_MANIFEST="${BIARM_TMP_ROOT}/${LEFT_EPISODE}/01_raw_extract/manifest.json"
RIGHT_RAW_MANIFEST="${BIARM_TMP_ROOT}/${RIGHT_EPISODE}/01_raw_extract/manifest.json"

# Host-wide concurrency budget.  One libx264rgb task uses both filter and
# encoder worker pools; the dual side-view process has two encoder streams.
# Cap automatic settings at the i7-14700K's 28 logical CPUs and lower I/O
# concurrency on rotational input disks.  Explicit environment overrides are
# still honored for benchmarking.
HOST_LOGICAL_THREADS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc)"
if (( HOST_LOGICAL_THREADS > 28 )); then
  HOST_LOGICAL_THREADS=28
fi
PROCESSING_THREAD_BUDGET="${PROCESSING_THREAD_BUDGET:-${HOST_LOGICAL_THREADS}}"
if [[ ! "${PROCESSING_THREAD_BUDGET}" =~ ^[1-9][0-9]*$ ]]; then
  echo "[error] PROCESSING_THREAD_BUDGET must be a positive integer" >&2
  exit 2
fi

detect_input_rotational() {
  local source parent device rota
  source="$(findmnt -n -o SOURCE -T "${INPUT_DIR}" 2>/dev/null || true)"
  [[ "${source}" == /dev/* ]] || return 1
  parent="$(lsblk -ndo PKNAME "${source}" 2>/dev/null | head -n 1 || true)"
  device="${source}"
  if [[ -n "${parent}" ]]; then
    device="/dev/${parent}"
  fi
  rota="$(lsblk -ndo ROTA "${device}" 2>/dev/null | tr -d '[:space:]' || true)"
  [[ "${rota}" == "1" ]]
}

INPUT_ROTATIONAL="${INPUT_ROTATIONAL:-auto}"
if [[ "${INPUT_ROTATIONAL}" == "auto" ]]; then
  if detect_input_rotational; then
    INPUT_ROTATIONAL=1
  else
    INPUT_ROTATIONAL=0
  fi
fi
if [[ "${INPUT_ROTATIONAL}" != "0" && "${INPUT_ROTATIONAL}" != "1" ]]; then
  echo "[error] INPUT_ROTATIONAL must be auto, 0, or 1" >&2
  exit 2
fi
if [[ "${INPUT_ROTATIONAL}" == "1" ]]; then
  if [[ -z "${PARALLEL_ARMS_WAS_SET}" ]]; then
    PARALLEL_ARMS=0
  fi
  if [[ -z "${FRONT_SEGMENT_WORKERS_WAS_SET}" || "${FRONT_SEGMENT_WORKERS}" == "0" ]]; then
    FRONT_SEGMENT_WORKERS=1
  fi
elif [[ -z "${FRONT_SEGMENT_WORKERS_WAS_SET}" || "${FRONT_SEGMENT_WORKERS}" == "0" ]]; then
  FRONT_SEGMENT_WORKERS=2
fi
if [[ ! "${FRONT_SEGMENT_WORKERS}" =~ ^[0-9]+$ ]]; then
  echo "[error] FRONT_SEGMENT_WORKERS must be a non-negative integer" >&2
  exit 2
fi

arm_processes=1
if [[ "${PARALLEL_ARMS}" == "1" && "${RUN_LEFT:-1}" == "1" && "${RUN_RIGHT:-1}" == "1" ]]; then
  arm_processes=2
fi
front_processes=$((arm_processes * FRONT_SEGMENT_WORKERS))
# Approximate concurrent pools per FFmpeg process: decoder + filter + one
# encoder for front model videos, decoder + filter + two encoders for the
# single-decode side views.
front_thread_weight=$((3 * front_processes))
side_thread_weight=$((4 * arm_processes))
max_thread_weight="${front_thread_weight}"
if (( side_thread_weight > max_thread_weight )); then
  max_thread_weight="${side_thread_weight}"
fi
FFMPEG_THREADS="${FFMPEG_THREADS:-$((PROCESSING_THREAD_BUDGET / max_thread_weight))}"
if [[ ! "${FFMPEG_THREADS}" =~ ^[0-9]+$ ]]; then
  echo "[error] FFMPEG_THREADS must be a positive integer" >&2
  exit 2
fi
if (( FFMPEG_THREADS < 1 )); then
  FFMPEG_THREADS=1
fi
if (( FFMPEG_THREADS * max_thread_weight > PROCESSING_THREAD_BUDGET )); then
  echo "[warning] explicit concurrency may exceed PROCESSING_THREAD_BUDGET: estimated=$((FFMPEG_THREADS * max_thread_weight)) budget=${PROCESSING_THREAD_BUDGET}" >&2
fi


camera_dir_for_side() {
  local side="$1"
  local role_dir="${INPUT_DIR}/insta/insta_${side}"
  local legacy_dir=""
  if [[ "${side}" == "right" ]]; then
    legacy_dir="${INPUT_DIR}/insta/camera_1"
  else
    legacy_dir="${INPUT_DIR}/insta/camera_2"
  fi
  if [[ -d "${role_dir}" ]]; then
    printf '%s\n' "${role_dir}"
  elif [[ -d "${legacy_dir}" ]]; then
    printf '%s\n' "${legacy_dir}"
  else
    printf '%s\n' "${role_dir}"
  fi
}

LEFT_CAMERA_DIR="${LEFT_CAMERA_DIR:-$(camera_dir_for_side left)}"
RIGHT_CAMERA_DIR="${RIGHT_CAMERA_DIR:-$(camera_dir_for_side right)}"
LEFT_M4="${LEFT_M4:-${LEFT_CAMERA_DIR}/insta_left.mp4}"
RIGHT_M4="${RIGHT_M4:-${RIGHT_CAMERA_DIR}/insta_right.mp4}"
if [[ ! -f "${LEFT_M4}" && -f "${INPUT_DIR}/left.mp4" ]]; then
  LEFT_M4="${INPUT_DIR}/left.mp4"
fi
if [[ ! -f "${RIGHT_M4}" && -f "${INPUT_DIR}/right.mp4" ]]; then
  RIGHT_M4="${INPUT_DIR}/right.mp4"
fi

if [[ -n "${TRACKER_CSV:-}" ]]; then
  LEFT_TRACKER="${LEFT_TRACKER:-${TRACKER_CSV}}"
  RIGHT_TRACKER="${RIGHT_TRACKER:-${TRACKER_CSV}}"
else
  LEFT_TRACKER="${LEFT_TRACKER:-${INPUT_DIR}/htc_left.csv}"
  RIGHT_TRACKER="${RIGHT_TRACKER:-${INPUT_DIR}/htc_right.csv}"
fi
if [[ ! -f "${LEFT_TRACKER}" && -f "${INPUT_DIR}/tracker_data.csv" ]]; then
  LEFT_TRACKER="${INPUT_DIR}/tracker_data.csv"
fi
if [[ ! -f "${RIGHT_TRACKER}" && -f "${INPUT_DIR}/tracker_data.csv" ]]; then
  RIGHT_TRACKER="${INPUT_DIR}/tracker_data.csv"
fi
if [[ ! -f "${LEFT_TRACKER}" && -f "${INPUT_DIR}/left_elbow.csv" ]]; then
  LEFT_TRACKER="${INPUT_DIR}/left_elbow.csv"
fi
if [[ ! -f "${RIGHT_TRACKER}" && -f "${INPUT_DIR}/right_elbow.csv" ]]; then
  RIGHT_TRACKER="${INPUT_DIR}/right_elbow.csv"
fi
SELECTED_LEFT_TRACKER="${LEFT_TRACKER}"
SELECTED_RIGHT_TRACKER="${RIGHT_TRACKER}"
SELECTED_LEFT_TRACKER_ROLE="htc_left"
SELECTED_RIGHT_TRACKER_ROLE="htc_right"

TIMESTAMP_LOG="${TIMESTAMP_LOG:-${INPUT_DIR}/timestamp_log.jsonl}"
if [[ ! -f "${TIMESTAMP_LOG}" ]]; then
  first_log="$(find "${INPUT_DIR}" -maxdepth 1 -type f -name 'timestamp_log_*.jsonl' | sort | head -n 1 || true)"
  TIMESTAMP_LOG="${first_log}"
fi

require_file() {
  local path="$1"
  local label="$2"
  if [[ -z "${path}" || ! -f "${path}" ]]; then
    echo "[error] missing ${label}: ${path}" >&2
    exit 1
  fi
}

require_files() {
  local label="$1"
  shift
  if [[ "$#" -eq 0 ]]; then
    echo "[error] missing ${label}" >&2
    exit 1
  fi
  local path
  for path in "$@"; do
    require_file "${path}" "${label}"
  done
}

preserve_source_file() {
  local src="$1"
  local dst="$2"
  local method
  method="$("${PYTHON}" "${ARTIFACT_HELPER}" safe-copy \
    --source "${src}" \
    --destination "${dst}" \
    --mode "${SOURCE_COPY_MODE}")"
  echo "[source] ${method}: ${src} -> ${dst}"
}

discover_side_insvs() {
  local side="$1"
  local -n out_ref="$2"
  local canonical="${INPUT_DIR}/${side}.insv"
  local role_dir=""
  if [[ "${side}" == "right" ]]; then
    role_dir="${RIGHT_CAMERA_DIR}"
  else
    role_dir="${LEFT_CAMERA_DIR}"
  fi
  out_ref=()
  if [[ -d "${role_dir}" ]]; then
    mapfile -d "" -t out_ref < <(
      find "${role_dir}" -maxdepth 1 -type f -iname '*.insv' -print0 | sort -zV
    )
  fi
  if [[ "${#out_ref[@]}" -eq 0 && -f "${canonical}" ]]; then
    out_ref+=("${canonical}")
  elif [[ "${#out_ref[@]}" -eq 0 ]]; then
    mapfile -d "" -t out_ref < <(
      find "${INPUT_DIR}" -maxdepth 1 -type f -name "${side}_*.insv" -print0 | sort -zV
    )
  fi
  if [[ "${#out_ref[@]}" -eq 0 ]]; then
    echo "[error] missing insta_${side} INSV: expected ${role_dir}/*.insv, ${canonical}, or ${INPUT_DIR}/${side}_*.insv" >&2
    exit 1
  fi
}

json_array() {
  local item escaped first=1
  printf "["
  for item in "$@"; do
    escaped="${item//\\/\\\\}"
    escaped="${escaped//\"/\\\"}"
    if [[ "${first}" == "1" ]]; then
      first=0
    else
      printf ", "
    fi
    printf "\"%s\"" "${escaped}"
  done
  printf "]"
}

left_insvs=()
right_insvs=()
discover_side_insvs left left_insvs
discover_side_insvs right right_insvs
LEFT_INSV="${left_insvs[0]}"
RIGHT_INSV="${right_insvs[0]}"

# X5 chapters are independent until their ordered manifests are assembled.
# On SSD, parse them concurrently; rotational media stays serialized to avoid
# seek amplification.  Each arm is already a separate process, so this value
# is a per-arm cap.
if [[ -z "${TELEMETRY_CHAPTER_WORKERS:-}" ]]; then
  if [[ "${INPUT_ROTATIONAL}" == "1" ]]; then
    TELEMETRY_CHAPTER_WORKERS=1
  else
    max_arm_chapters="${#left_insvs[@]}"
    if (( ${#right_insvs[@]} > max_arm_chapters )); then
      max_arm_chapters="${#right_insvs[@]}"
    fi
    TELEMETRY_CHAPTER_WORKERS="${max_arm_chapters}"
    if (( TELEMETRY_CHAPTER_WORKERS > 3 )); then
      TELEMETRY_CHAPTER_WORKERS=3
    fi
  fi
fi
if [[ ! "${TELEMETRY_CHAPTER_WORKERS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "[error] TELEMETRY_CHAPTER_WORKERS must be a positive integer" >&2
  exit 2
fi

pano_readable() {
  local path="$1"
  shift
  local inputs=("$@")
  local command=(
    "${PYTHON}" "${ARTIFACT_HELPER}" pano-check
    --video "${path}"
    --width "${PANO_WIDTH}"
    --height "${PANO_HEIGHT}"
    --fps "${PANO_FPS}"
    --bitrate "${PANO_BITRATE}"
    --flowstate "${PANO_FLOWSTATE}"
    --stitch-type "${PANO_STITCH_TYPE}"
    --stitch-fusion "${PANO_STITCH_FUSION}"
    --direction-lock "${PANO_DIRECTION_LOCK}"
    --input-mode "${INSV_INPUT_MODE:-auto}"
  )
  local input
  for input in "${inputs[@]}"; do
    command+=(--input "${input}")
  done
  if [[ "${ADOPT_LEGACY_PANO}" == "1" && "${DRY_RUN:-0}" != "1" ]]; then
    command+=(--adopt-legacy)
  fi
  if [[ "${VERIFY_FULL_PANO_HASH:-0}" == "1" ]]; then
    command+=(--verify-full-output-hash)
  fi
  "${command[@]}"
}

existing_export_running() {
  pgrep -af "[i]nsv_to_pano" | grep -F -- "${LEFT_M4}" >/dev/null 2>&1 \
    || pgrep -af "[i]nsv_to_pano" | grep -F -- "${RIGHT_M4}" >/dev/null 2>&1
}

wait_existing_export() {
  if [[ "${WAIT_EXISTING_EXPORT}" != "1" ]]; then
    return
  fi
  while existing_export_running; do
    echo "[info] existing panorama export is still running; waiting 30s"
    sleep 30
  done
}

should_export_pano() {
  if [[ "${EXPORT_PANO}" == "0" ]]; then
    return 1
  fi
  if [[ "${EXPORT_PANO}" == "1" ]]; then
    return 0
  fi
  if ! pano_readable "${LEFT_M4}" "${left_insvs[@]}" \
      || ! pano_readable "${RIGHT_M4}" "${right_insvs[@]}"; then
    return 0
  fi
  [[ "${SKIP_EXISTING_PANO}" != "1" ]]
}

run_panorama_export() {
  echo "[stage] panorama export"
  echo "[info] pano_export_dir=${PANO_EXPORT_DIR}"
  echo "[info] fast_export=${FAST_EXPORT} fastest_export=${FASTEST_EXPORT} stitch_type=${STITCH_TYPE:-auto}"
  (
    cd "${PANO_EXPORT_DIR}"
    INPUT_DIR="${INPUT_DIR}" \
    TMP_ROOT="${TMP_ROOT}" \
    SEGMENT_WORK_DIR="${SEGMENT_WORK_DIR:-${TMP_ROOT}/.pano_segments}" \
    LEFT_INPUTS="${left_insvs[*]}" \
    RIGHT_INPUTS="${right_insvs[*]}" \
    LEFT_OUT="${LEFT_M4}" \
    RIGHT_OUT="${RIGHT_M4}" \
    WIDTH="${PANO_WIDTH}" \
    HEIGHT="${PANO_HEIGHT}" \
    FPS="${PANO_FPS}" \
    BITRATE="${PANO_BITRATE}" \
    FLOWSTATE="${PANO_FLOWSTATE}" \
    STITCH_TYPE="${PANO_STITCH_TYPE}" \
    STITCH_FUSION="${PANO_STITCH_FUSION}" \
    DIRECTION_LOCK="${PANO_DIRECTION_LOCK}" \
    SKIP_EXISTING_PANO="${SKIP_EXISTING_PANO}" \
    ADOPT_LEGACY_PANO="${ADOPT_LEGACY_PANO}" \
    FAST_EXPORT="${FAST_EXPORT}" \
    FASTEST_EXPORT="${FASTEST_EXPORT}" \
    INSV_INPUT_MODE="${INSV_INPUT_MODE:-auto}" \
    DRY_RUN="${DRY_RUN:-0}" \
    bash scripts/export_biarm.sh
  )
}

if should_export_pano; then
  wait_existing_export
  run_panorama_export
else
  echo "[skip] panorama export disabled or existing panoramas accepted"
fi

if [[ "${DRY_RUN:-0}" != "1" && "${DIRECT_IMAGE_MODE}" != "1" ]]; then
  if ! pano_readable "${LEFT_M4}" "${left_insvs[@]}"; then
    echo "[error] left panorama failed exact frame/provenance validation: ${LEFT_M4}" >&2
    exit 1
  fi
  if ! pano_readable "${RIGHT_M4}" "${right_insvs[@]}"; then
    echo "[error] right panorama failed exact frame/provenance validation: ${RIGHT_M4}" >&2
    exit 1
  fi
fi

prepare_side_input() {
  local side="$1"
  local mp4_path="$2"
  local tracker_path="$3"
  shift 3
  local insv_paths=("$@")
  local side_dir="${STAGING_ROOT}/${side}"

  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "[dry-run] prepare ${side} staging:"
    if [[ "${DIRECT_IMAGE_MODE}" != "1" ]]; then
      echo "          ${side}.mp4 -> ${mp4_path}"
    else
      echo "          panorama mp4 -> not used (OpenPI direct image mode)"
    fi
    printf "          insv -> %s\n" "${insv_paths[@]}"
    echo "          timestamp_log_${BIARM_SESSION}.jsonl -> ${TIMESTAMP_LOG}"
    echo "          htc_${side}.csv -> ${tracker_path}"
    return
  fi

  rm -rf "${side_dir}"
  mkdir -p "${side_dir}"
  if [[ "${DIRECT_IMAGE_MODE}" != "1" ]]; then
    ln -s "${mp4_path}" "${side_dir}/${side}.mp4"
  fi
  local insv_path
  for insv_path in "${insv_paths[@]}"; do
    ln -s "${insv_path}" "${side_dir}/$(basename "${insv_path}")"
  done

  # extract_raw_views_imu.py discovers timestamp logs with timestamp_log_*.jsonl.
  ln -s "${TIMESTAMP_LOG}" "${side_dir}/timestamp_log_${BIARM_SESSION}.jsonl"

  # Keep tracker CSV next to the staged inputs for traceability. The extractor
  # does not consume it in stage 01, but later stages can find the source path
  # from the generated bimanual manifest.
  ln -s "${tracker_path}" "${side_dir}/htc_${side}.csv"
}

run_side() {
  local side="$1"
  local episode="$2"
  local input_dir="${STAGING_ROOT}/${side}"
  local tracker_path="${RIGHT_TRACKER}"
  local camera_role="insta_right"
  local frame0_host_unix_time_sec="${RIGHT_VIDEO_FRAME0_HOST_UNIX_TIME_SEC:-}"
  if [[ "${side}" == "left" ]]; then
    tracker_path="${LEFT_TRACKER}"
    camera_role="insta_left"
    frame0_host_unix_time_sec="${LEFT_VIDEO_FRAME0_HOST_UNIX_TIME_SEC:-}"
  fi

  local cmd=(
    "${PYTHON}" "${SCRIPT_DIR}/extract_raw_views_imu.py"
    --input-dir "${input_dir}"
    --tmp-root "${BIARM_TMP_ROOT}"
    --episode "${episode}"
    --model-image-width "${MODEL_IMAGE_WIDTH}"
    --model-image-height "${MODEL_IMAGE_HEIGHT}"
    --ffmpeg-bin "${FFMPEG_BIN}"
    --ffmpeg-threads "${FFMPEG_THREADS}"
    --source-copy-mode "${SOURCE_COPY_MODE}"
    --camera-role "${camera_role}"
  )
  if [[ "${DIRECT_IMAGE_MODE}" == "1" ]]; then
    cmd+=(--direct-image-mode)
    if [[ "${CALIBRATE_CLOCK}" != "0" ]]; then
      cmd+=(--write-clock-timing-artifacts)
    fi
    if [[ "${MATERIALIZE_FRONT_VIDEO}" == "0" ]]; then
      cmd+=(--no-materialize-front-video)
    fi
  else
    cmd+=(
      --ffmpeg-preset "${FFMPEG_PRESET}"
      --video-encoder "${VIDEO_ENCODER}"
      --video-quality "${VIDEO_QUALITY}"
    )
  fi

  if [[ -n "${frame0_host_unix_time_sec}" ]]; then
    cmd+=(--video-frame0-host-unix-time-sec "${frame0_host_unix_time_sec}")
  fi

  # Direct production processing always keeps only the compact, provenance-
  # bound timing NPZ.  The legacy multi-million-row IMU JSON/CSV was never
  # consumed downstream and is no longer selectable from this wrapper.
  if [[ "${SKIP_IMU:-0}" == "1" || "${DIRECT_IMAGE_MODE}" == "1" ]]; then
    cmd+=(--skip-imu)
  fi
  cmd+=(--telemetry-workers "${TELEMETRY_CHAPTER_WORKERS}")
  if [[ "${ONLY_IMU:-0}" == "1" || "${SKIP_VIDEOS:-0}" == "1" ]]; then
    cmd+=(--skip-videos)
  fi
  if [[ "${REPAIR_FRAME_COUNTS:-0}" == "1" ]]; then
    cmd+=(--repair-frame-counts)
  fi
  if [[ "${ALLOW_FRAME_TRIM:-0}" == "1" ]]; then
    cmd+=(--allow-frame-trim)
  elif [[ "${ALLOW_FRAME_TRIM:-0}" != "0" ]]; then
    echo "[error] ALLOW_FRAME_TRIM must be 0 or 1" >&2
    return 2
  fi
  if [[ "${SKIP_EXISTING_VIDEOS:-0}" == "1" ]]; then
    cmd+=(--skip-existing-videos)
  fi
  if [[ "${PARALLEL_SIDE_VIEWS:-0}" == "1" ]]; then
    cmd+=(--parallel-side-views)
  fi
  if [[ "${PARALLEL_FRONT_SEGMENTS:-0}" == "1" ]]; then
    cmd+=(--parallel-front-segments)
  fi
  if [[ "${FRONT_SEGMENT_WORKERS:-0}" != "0" ]]; then
    cmd+=(--front-segment-workers "${FRONT_SEGMENT_WORKERS}")
  fi
  if [[ -n "${MAX_FRAMES:-}" ]]; then
    cmd+=(--max-frames "${MAX_FRAMES}")
  fi

  echo "[info] extracting ${side}: episode=${episode}"
  printf '+'
  printf ' %q' "${cmd[@]}"
  printf '\n'
  if [[ "${DRY_RUN:-0}" != "1" ]]; then
    "${cmd[@]}"

    local out_dir="${BIARM_TMP_ROOT}/${episode}/01_raw_extract"
    preserve_source_file "${tracker_path}" "${out_dir}/raw_sources/htc_${side}.csv"
  fi
}

write_biarm_manifest() {
  local manifest_dir="${BIARM_TMP_ROOT}/01_raw_extract_biarm"
  local manifest_path="${manifest_dir}/manifest.json"
  mkdir -p "${manifest_dir}"
  cat > "${manifest_path}" <<EOF
{
  "schema": "instaumi_biarm_raw_extract_v1",
  "session": "${BIARM_SESSION}",
  "image_pipeline": "$(if [[ "${DIRECT_IMAGE_MODE}" == "1" ]]; then printf openpi_224_direct; else printf legacy_model_video; fi)",
  "materialize_front_video": $(if [[ "${MATERIALIZE_FRONT_VIDEO}" == "1" ]]; then printf true; else printf false; fi),
  "input_dir": "${INPUT_DIR}",
  "timestamp_log": "${TIMESTAMP_LOG}",
  "clock_calibration": {
    "mode": "${CALIBRATE_CLOCK}",
    "status": "${CLOCK_CALIBRATION_STATUS}",
    "report": "${CLOCK_CALIBRATION_REPORT}",
    "left_timestamps_csv": "${CLOCK_CALIBRATION_OUTPUT_DIR}/left/calibrated_frame_timestamps.csv",
    "right_timestamps_csv": "${CLOCK_CALIBRATION_OUTPUT_DIR}/right/calibrated_frame_timestamps.csv"
  },
  "left": {
    "camera_role": "insta_left",
    "tracker_role": "${SELECTED_LEFT_TRACKER_ROLE}",
    "episode": "${LEFT_EPISODE}",
    "mp4": "${LEFT_M4}",
    "insv": "${LEFT_INSV}",
    "insv_files": $(json_array "${left_insvs[@]}"),
    "tracker_csv": "${SELECTED_LEFT_TRACKER}",
    "raw_extract_dir": "${BIARM_TMP_ROOT}/${LEFT_EPISODE}/01_raw_extract"
  },
  "right": {
    "camera_role": "insta_right",
    "tracker_role": "${SELECTED_RIGHT_TRACKER_ROLE}",
    "episode": "${RIGHT_EPISODE}",
    "mp4": "${RIGHT_M4}",
    "insv": "${RIGHT_INSV}",
    "insv_files": $(json_array "${right_insvs[@]}"),
    "tracker_csv": "${SELECTED_RIGHT_TRACKER}",
    "raw_extract_dir": "${BIARM_TMP_ROOT}/${RIGHT_EPISODE}/01_raw_extract"
  }
}
EOF
  echo "[info] bimanual manifest=${manifest_path}"
}

clock_calibration_is_current() {
  [[ "${REUSE_ACCEPTED_CLOCK_CALIBRATION}" == "1" ]] || return 1
  [[ -s "${CLOCK_CALIBRATION_REPORT}" ]] || return 1
  [[ -s "${CLOCK_CALIBRATION_OUTPUT_DIR}/left/calibrated_frame_timestamps.csv" ]] || return 1
  [[ -s "${CLOCK_CALIBRATION_OUTPUT_DIR}/right/calibrated_frame_timestamps.csv" ]] || return 1
  [[ "${CLOCK_CALIBRATION_REPORT}" -nt "${CLOCK_CALIBRATION_OUTPUT_DIR}/left/calibrated_frame_timestamps.csv" ]] || return 1
  [[ "${CLOCK_CALIBRATION_REPORT}" -nt "${CLOCK_CALIBRATION_OUTPUT_DIR}/right/calibrated_frame_timestamps.csv" ]] || return 1
  "${PYTHON}" -c '
import json, pathlib, sys
report_path = pathlib.Path(sys.argv[1]).resolve()
report = json.load(open(report_path, encoding="utf-8"))
arms = report.get("arms") or {}
ok = (
    report.get("status") == "accepted"
    and report.get("schema") == "instaumi-no-slam-clock-calibration-v2-monotonic-raw"
    and (report.get("clock_contract") or {}).get("domain") == "host_clock_monotonic_raw"
    and (report.get("clock_contract") or {}).get("alignment_policy") == "single_domain_only_fail_closed"
    and bool(report.get("calibration_id"))
    and pathlib.Path(report.get("session_dir", "")).resolve() == pathlib.Path(sys.argv[2]).resolve()
)

def identity_ok(identity, expected):
    if not isinstance(identity, dict):
        return False
    path = pathlib.Path(identity.get("path", "")).resolve()
    expected_path = pathlib.Path(expected).resolve()
    if path != expected_path or not path.is_file():
        return False
    stat = path.stat()
    return (
        int(identity.get("size_bytes", -1)) == stat.st_size
        and int(identity.get("mtime_ns", -1)) == stat.st_mtime_ns
        and report_path.stat().st_mtime_ns > stat.st_mtime_ns
    )

inputs = report.get("input_files") or {}
ok = ok and identity_ok(inputs.get("timestamp_log"), sys.argv[3])
transfer_manifest = pathlib.Path(sys.argv[2]) / "insta" / "transfer_manifest.json"
role_correction = pathlib.Path(sys.argv[2]) / "insta" / "camera_role_correction.json"
ok = ok and identity_ok(inputs.get("camera_transfer_manifest"), transfer_manifest)
if role_correction.is_file():
    ok = ok and identity_ok(inputs.get("camera_role_correction"), role_correction)
else:
    ok = ok and inputs.get("camera_role_correction") is None
expected_htc = {"left": sys.argv[4], "right": sys.argv[5]}
assignment = ((report.get("tracker_assignment") or {}).get("camera_to_tracker") or {})
expected_timeline = {"left": sys.argv[6], "right": sys.argv[7]}
expected_insv = {"left": json.loads(sys.argv[8]), "right": json.loads(sys.argv[9])}
expected_raw_manifest = {"left": sys.argv[10], "right": sys.argv[11]}
for name in ("left", "right"):
    arm = arms.get(name) or {}
    tracker_name = assignment.get(name)
    selected_htc = expected_htc.get(tracker_name, "")
    actual = (arm.get("frame_timeline") or {}).get("path")
    arm_inputs = ((inputs.get("arms") or {}).get(name) or {})
    ok = ok and tracker_name in ("left", "right")
    ok = ok and arm.get("tracker_arm") == tracker_name
    ok = ok and arm.get("tracker_role") == f"htc_{tracker_name}"
    ok = ok and arm.get("status") == "accepted" and arm.get("calibration_id") == report.get("calibration_id")
    ok = ok and identity_ok(arm_inputs.get("htc"), selected_htc)
    ok = ok and identity_ok((arm.get("htc") or {}).get("file_identity"), selected_htc)
    recorded_insv = arm_inputs.get("insv") or []
    ok = ok and identity_ok(arm_inputs.get("raw_manifest"), expected_raw_manifest[name])
    recorded_timing = arm_inputs.get("timing_artifacts") or []
    ok = ok and len(recorded_timing) == len(expected_insv[name])
    ok = ok and all(identity_ok(item, item.get("path", "")) for item in recorded_timing)
    ok = ok and {str(pathlib.Path(item.get("path", "")).resolve()) for item in recorded_insv} == {
        str(pathlib.Path(path).resolve()) for path in expected_insv[name]
    }
    ok = ok and all(identity_ok(item, item.get("path", "")) for item in recorded_insv)
    timeline = arm.get("frame_timeline") or {}
    ok = ok and timeline.get("clock_domain") == "host_clock_monotonic_raw"
    ok = ok and (arm.get("htc") or {}).get("time_column") == "hid_read_host_monotonic_raw_ns"
    ok = ok and actual is not None and identity_ok(timeline.get("file_identity"), expected_timeline[name])
    if actual is not None:
        ok = ok and pathlib.Path(actual).resolve() == pathlib.Path(expected_timeline[name]).resolve()
sys.exit(0 if ok else 1)
' "${CLOCK_CALIBRATION_REPORT}" \
    "${INPUT_DIR}" "${TIMESTAMP_LOG}" "${LEFT_TRACKER}" "${RIGHT_TRACKER}" \
    "${CLOCK_CALIBRATION_OUTPUT_DIR}/left/calibrated_frame_timestamps.csv" \
    "${CLOCK_CALIBRATION_OUTPUT_DIR}/right/calibrated_frame_timestamps.csv" \
    "$(json_array "${left_insvs[@]}")" "$(json_array "${right_insvs[@]}")" \
    "${LEFT_RAW_MANIFEST}" "${RIGHT_RAW_MANIFEST}" || return 1
  local source
  for source in "${left_insvs[@]}" "${right_insvs[@]}" \
    "${LEFT_TRACKER}" "${RIGHT_TRACKER}" "${TIMESTAMP_LOG}" \
    "${INPUT_DIR}/insta/transfer_manifest.json" \
    "${LEFT_RAW_MANIFEST}" "${RIGHT_RAW_MANIFEST}" \
    "${SCRIPT_DIR}/extract_raw_views_imu.py" "${SCRIPT_DIR}/calibrate_no_slam_clock.py"; do
    [[ "${CLOCK_CALIBRATION_REPORT}" -nt "${source}" ]] || return 1
  done
  if [[ -f "${INPUT_DIR}/insta/camera_role_correction.json" ]]; then
    [[ "${CLOCK_CALIBRATION_REPORT}" -nt "${INPUT_DIR}/insta/camera_role_correction.json" ]] || return 1
  fi
}

run_clock_calibration() {
  if [[ "${CALIBRATE_CLOCK}" == "0" ]]; then
    CLOCK_CALIBRATION_STATUS="disabled"
    echo "[skip] no-SLAM clock calibration disabled (CALIBRATE_CLOCK=0)"
    return 0
  fi
  if [[ "${CALIBRATE_CLOCK}" != "auto" && "${CALIBRATE_CLOCK}" != "1" ]]; then
    echo "[error] CALIBRATE_CLOCK must be auto, 1, or 0; got: ${CALIBRATE_CLOCK}" >&2
    return 2
  fi
  if clock_calibration_is_current; then
    CLOCK_CALIBRATION_STATUS="accepted"
    echo "[skip] accepted clock calibration is current: ${CLOCK_CALIBRATION_REPORT}"
    return 0
  fi

  local cmd=(
    "${PYTHON}" "${SCRIPT_DIR}/calibrate_no_slam_clock.py"
    --session-dir "${INPUT_DIR}"
    --output-dir "${CLOCK_CALIBRATION_OUTPUT_DIR}"
    --timestamp-log "${TIMESTAMP_LOG}"
    --htc-left "${LEFT_TRACKER}"
    --htc-right "${RIGHT_TRACKER}"
    --left-raw-manifest "${LEFT_RAW_MANIFEST}"
    --right-raw-manifest "${RIGHT_RAW_MANIFEST}"
    --arm-workers "${CLOCK_CALIBRATION_ARM_WORKERS}"
  )
  local source
  for source in "${left_insvs[@]}"; do
    cmd+=(--left-insv "${source}")
  done
  for source in "${right_insvs[@]}"; do
    cmd+=(--right-insv "${source}")
  done
  if [[ -n "${CLOCK_CALIBRATION_MAX_RESIDUAL_P95_MS:-}" ]]; then
    cmd+=(--max-residual-p95-ms "${CLOCK_CALIBRATION_MAX_RESIDUAL_P95_MS}")
  fi
  echo "[stage] no-SLAM X5/HTC clock calibration"
  printf ' +'; printf ' %q' "${cmd[@]}"; printf '\n'
  local calibration_rc=0
  "${cmd[@]}" || calibration_rc=$?
  if [[ ! -s "${CLOCK_CALIBRATION_REPORT}" ]]; then
    CLOCK_CALIBRATION_STATUS="missing_report"
    echo "[warning] clock calibration exited ${calibration_rc} without a report" >&2
    if [[ "${CLOCK_CALIBRATION_REQUIRED}" == "1" ]]; then
      return 1
    fi
    return 0
  fi
  CLOCK_CALIBRATION_STATUS="$("${PYTHON}" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("status", "unknown"))' "${CLOCK_CALIBRATION_REPORT}")"
  if [[ "${calibration_rc}" == "0" && "${CLOCK_CALIBRATION_STATUS}" == "accepted" ]]; then
    echo "[info] clock calibration accepted: ${CLOCK_CALIBRATION_REPORT}"
    return 0
  fi
  echo "[warning] clock calibration exit=${calibration_rc} status=${CLOCK_CALIBRATION_STATUS}; calibrated downstream conversion will refuse this session" >&2
  if [[ "${CLOCK_CALIBRATION_REQUIRED}" == "1" ]]; then
    return 1
  fi
  return 0
}

apply_clock_tracker_assignment() {
  [[ "${CLOCK_CALIBRATION_STATUS}" == "accepted" ]] || return 0
  local values=()
  mapfile -t values < <(
    "${PYTHON}" - "${CLOCK_CALIBRATION_REPORT}" <<'PY'
import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assignment = ((report.get("tracker_assignment") or {}).get("camera_to_tracker") or {})
arms = report.get("arms") or {}
for camera in ("left", "right"):
    tracker = assignment.get(camera)
    arm = arms.get(camera) or {}
    path = ((arm.get("htc") or {}).get("path") or "")
    role = arm.get("tracker_role") or (f"htc_{tracker}" if tracker else "")
    print(path)
    print(role)
PY
  )
  if [[ "${#values[@]}" != "4" || -z "${values[0]}" || -z "${values[1]}" || -z "${values[2]}" || -z "${values[3]}" ]]; then
    echo "[error] accepted clock report did not expose a complete camera-to-HTC assignment" >&2
    return 1
  fi
  SELECTED_LEFT_TRACKER="${values[0]}"
  SELECTED_LEFT_TRACKER_ROLE="${values[1]}"
  SELECTED_RIGHT_TRACKER="${values[2]}"
  SELECTED_RIGHT_TRACKER_ROLE="${values[3]}"
  echo "[info] audited tracker assignment: camera_left->${SELECTED_LEFT_TRACKER_ROLE} camera_right->${SELECTED_RIGHT_TRACKER_ROLE}"
}

if [[ "${DIRECT_IMAGE_MODE}" != "1" ]]; then
  require_file "${LEFT_M4}" "left mp4"
fi
require_files "left insv" "${left_insvs[@]}"
require_file "${LEFT_TRACKER}" "left tracker csv"
if [[ "${DIRECT_IMAGE_MODE}" != "1" ]]; then
  require_file "${RIGHT_M4}" "right mp4"
fi
require_files "right insv" "${right_insvs[@]}"
require_file "${RIGHT_TRACKER}" "right tracker csv"
require_file "${TIMESTAMP_LOG}" "timestamp log"

echo "[info] input_dir=${INPUT_DIR}"
echo "[info] tmp_root=${TMP_ROOT}"
echo "[info] biarm_tmp_root=${BIARM_TMP_ROOT}"
echo "[info] biarm_layout=${BIARM_LAYOUT}"
echo "[info] biarm_session=${BIARM_SESSION}"
echo "[info] left_episode=${LEFT_EPISODE}"
echo "[info] right_episode=${RIGHT_EPISODE}"
echo "[info] timestamp_log=${TIMESTAMP_LOG}"
echo "[info] staging_root=${STAGING_ROOT}"
echo "[info] python=${PYTHON}"
echo "[info] only_imu=${ONLY_IMU:-0}"
echo "[info] direct_image_mode=${DIRECT_IMAGE_MODE}"
echo "[info] materialize_front_video=${MATERIALIZE_FRONT_VIDEO}"
echo "[info] telemetry_chapter_workers_per_arm=${TELEMETRY_CHAPTER_WORKERS}"
echo "[info] repair_frame_counts=${REPAIR_FRAME_COUNTS:-0}"
echo "[info] skip_existing_videos=${SKIP_EXISTING_VIDEOS:-0}"
echo "[info] parallel_arms=${PARALLEL_ARMS}"
echo "[info] parallel_side_views=${PARALLEL_SIDE_VIEWS:-0}"
echo "[info] parallel_front_segments=${PARALLEL_FRONT_SEGMENTS:-0}"
echo "[info] front_segment_workers=${FRONT_SEGMENT_WORKERS}"
echo "[info] input_rotational=${INPUT_ROTATIONAL}"
echo "[info] processing_thread_budget=${PROCESSING_THREAD_BUDGET}"
echo "[info] ffmpeg_threads_per_task=${FFMPEG_THREADS}"
echo "[info] ffmpeg_bin=${FFMPEG_BIN}"
if [[ "${DIRECT_IMAGE_MODE}" == "1" ]]; then
  echo "[info] model_video_encoder=<unused; no model video is generated>"
else
  echo "[info] video_encoder=${VIDEO_ENCODER}"
  echo "[info] video_quality=${VIDEO_QUALITY}"
  echo "[info] ffmpeg_preset=${FFMPEG_PRESET}"
fi
echo "[info] source_copy_mode=${SOURCE_COPY_MODE}"
echo "[info] calibrate_clock=${CALIBRATE_CLOCK}"
echo "[info] clock_calibration_required=${CLOCK_CALIBRATION_REQUIRED}"
echo "[info] clock_calibration_arm_workers=${CLOCK_CALIBRATION_ARM_WORKERS}"
echo "[info] clock_calibration_output_dir=${CLOCK_CALIBRATION_OUTPUT_DIR}"
echo "[info] left_insvs=${left_insvs[*]}"
echo "[info] right_insvs=${right_insvs[*]}"
echo "[info] model_image_size=${MODEL_IMAGE_WIDTH}x${MODEL_IMAGE_HEIGHT}"

if [[ "${DRY_RUN:-0}" != "1" && "${SKIP_IMU:-0}" != "1" ]]; then
  if ! "${PYTHON}" -c 'import telemetry_parser' >/dev/null 2>&1; then
    echo "[error] ${PYTHON} cannot import telemetry_parser." >&2
    echo "        Activate the instaumi env or set PYTHON=/opt/anaconda3/envs/instaumi/bin/python." >&2
    echo "        Or run SKIP_IMU=1 if you intentionally do not need IMU." >&2
    exit 1
  fi
fi

if [[ "${DRY_RUN:-0}" != "1" ]]; then
  mkdir -p "${STAGING_ROOT}"
fi

prepare_side_input "left" "${LEFT_M4}" "${LEFT_TRACKER}" "${left_insvs[@]}"
prepare_side_input "right" "${RIGHT_M4}" "${RIGHT_TRACKER}" "${right_insvs[@]}"

if [[ "${PARALLEL_ARMS}" == "1" && "${RUN_LEFT:-1}" == "1" && "${RUN_RIGHT:-1}" == "1" ]]; then
  echo "[info] extracting left/right arms concurrently"
  run_side "left" "${LEFT_EPISODE}" &
  left_pid=$!
  run_side "right" "${RIGHT_EPISODE}" &
  right_pid=$!

  left_status=0
  right_status=0
  wait "${left_pid}" || left_status=$?
  wait "${right_pid}" || right_status=$?
  if [[ "${left_status}" != "0" ]]; then
    echo "[error] left raw extract failed" >&2
  fi
  if [[ "${right_status}" != "0" ]]; then
    echo "[error] right raw extract failed" >&2
  fi
  if [[ "${left_status}" != "0" || "${right_status}" != "0" ]]; then
    exit 1
  fi
else
  if [[ "${RUN_LEFT:-1}" == "1" ]]; then
    run_side "left" "${LEFT_EPISODE}"
  else
    echo "[skip] RUN_LEFT=0"
  fi

  if [[ "${RUN_RIGHT:-1}" == "1" ]]; then
    run_side "right" "${RIGHT_EPISODE}"
  else
    echo "[skip] RUN_RIGHT=0"
  fi
fi

if [[ "${DRY_RUN:-0}" != "1" ]]; then
  run_clock_calibration
  apply_clock_tracker_assignment
  write_biarm_manifest
else
  echo "[dry-run] clock calibration would run after extraction (CALIBRATE_CLOCK=${CALIBRATE_CLOCK})"
  echo "[dry-run] no files written"
fi

}
main "$@"
