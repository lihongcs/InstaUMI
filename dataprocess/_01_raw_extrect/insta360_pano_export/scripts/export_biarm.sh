#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
RAW_EXTRACT_DIR="$(cd "${PROJECT_DIR}/.." && pwd)"
ARTIFACT_HELPER="${RAW_EXTRACT_DIR}/artifact_io.py"

INPUT_DIR="${INPUT_DIR:?Set INPUT_DIR to the completed capture directory}"
LEFT_OUT="${LEFT_OUT:-${INPUT_DIR}/left.mp4}"
RIGHT_OUT="${RIGHT_OUT:-${INPUT_DIR}/right.mp4}"
INSV_INPUT_MODE="${INSV_INPUT_MODE:-auto}" # auto|segments|group
TMP_ROOT="${TMP_ROOT:-${INPUT_DIR}/tmp}"
SEGMENT_WORK_DIR="${SEGMENT_WORK_DIR:-${TMP_ROOT}/.pano_segments}"
SKIP_EXISTING_PARTS="${SKIP_EXISTING_PARTS:-1}"
SKIP_EXISTING_PANO="${SKIP_EXISTING_PANO:-1}"
ADOPT_LEGACY_PANO="${ADOPT_LEGACY_PANO:-0}"
WIDTH="${WIDTH:-3840}"
HEIGHT="${HEIGHT:-1920}"
FPS="${FPS:-60000/1001}"
BITRATE="${BITRATE:-67M}"
FLOWSTATE="${FLOWSTATE:-off}"
STITCH_FUSION="${STITCH_FUSION:-off}"
DIRECTION_LOCK="${DIRECTION_LOCK:-off}"
PYTHON_BIN="${PYTHON:-python3}"
if [[ -z "${STITCH_TYPE:-}" ]]; then
  if [[ "${FASTEST_EXPORT:-0}" == "1" ]]; then
    STITCH_TYPE="template"
  elif [[ "${FAST_EXPORT:-0}" == "1" ]]; then
    STITCH_TYPE="dynamic"
  else
    STITCH_TYPE="optflow"
  fi
fi
export WIDTH HEIGHT FPS BITRATE FLOWSTATE STITCH_TYPE STITCH_FUSION DIRECTION_LOCK

pano_readable() {
  local path="$1"
  shift
  local inputs=("$@")
  local command=(
    "${PYTHON_BIN}" "${ARTIFACT_HELPER}" pano-check
    --video "${path}"
    --width "${WIDTH}"
    --height "${HEIGHT}"
    --fps "${FPS}"
    --bitrate "${BITRATE}"
    --flowstate "${FLOWSTATE}"
    --stitch-type "${STITCH_TYPE}"
    --stitch-fusion "${STITCH_FUSION}"
    --direction-lock "${DIRECTION_LOCK}"
    --input-mode "${INSV_INPUT_MODE}"
  )
  local input
  for input in "${inputs[@]}"; do
    command+=(--input "${input}")
  done
  if [[ "${ADOPT_LEGACY_PANO}" == "1" ]]; then
    command+=(--adopt-legacy)
  fi
  if [[ "${VERIFY_FULL_PANO_HASH:-0}" == "1" ]]; then
    command+=(--verify-full-output-hash)
  fi
  "${command[@]}"
}

write_pano_plan() {
  local plan_path="$1"
  shift
  local command=(
    "${PYTHON_BIN}" "${ARTIFACT_HELPER}" pano-plan
    --plan "${plan_path}"
    --width "${WIDTH}"
    --height "${HEIGHT}"
    --fps "${FPS}"
    --bitrate "${BITRATE}"
    --flowstate "${FLOWSTATE}"
    --stitch-type "${STITCH_TYPE}"
    --stitch-fusion "${STITCH_FUSION}"
    --direction-lock "${DIRECTION_LOCK}"
    --input-mode "${INSV_INPUT_MODE}"
  )
  local input
  for input in "$@"; do
    command+=(--input "${input}")
  done
  "${command[@]}"
}

video_stream_count() {
  ffprobe -v error -select_streams v -show_entries stream=index -of csv=p=0 "$1" 2>/dev/null | wc -l
}

discover_side_inputs() {
  local side="$1"
  local env_name="${side^^}_INSV"
  local inputs_env_name="${side^^}_INPUTS"
  local explicit_input="${!env_name:-}"
  local explicit_inputs="${!inputs_env_name:-}"
  local -n out_ref="$2"

  out_ref=()
  if [[ -n "${explicit_inputs}" ]]; then
    read -r -a out_ref <<<"${explicit_inputs}"
  elif [[ -n "${explicit_input}" ]]; then
    out_ref+=("${explicit_input}")
  elif [[ -f "${INPUT_DIR}/${side}.insv" ]]; then
    out_ref+=("${INPUT_DIR}/${side}.insv")
  else
    mapfile -d "" -t out_ref < <(
      find "${INPUT_DIR}" -maxdepth 1 -type f -name "${side}_*.insv" -print0 | sort -zV
    )
  fi

  if [[ "${#out_ref[@]}" -eq 0 ]]; then
    echo "[error] missing ${side} insv: expected ${INPUT_DIR}/${side}.insv or ${INPUT_DIR}/${side}_*.insv" >&2
    exit 1
  fi
  for input in "${out_ref[@]}"; do
    if [[ ! -f "${input}" ]]; then
      echo "[error] missing ${side} insv: ${input}" >&2
      exit 1
    fi
  done
}

side_mode() {
  local mode="${INSV_INPUT_MODE}"
  shift
  local inputs=("$@")
  if [[ "${mode}" != "auto" ]]; then
    echo "${mode}"
    return
  fi
  if [[ "${#inputs[@]}" -le 1 ]]; then
    echo "group"
    return
  fi

  local input count
  for input in "${inputs[@]}"; do
    count="$(video_stream_count "${input}")"
    if [[ "${count}" -lt 2 ]]; then
      echo "group"
      return
    fi
  done
  echo "segments"
}

write_concat_list() {
  local list_path="$1"
  shift
  local part
  : >"${list_path}"
  for part in "$@"; do
    if [[ "${part}" == *" "* ]]; then
      echo "[error] concat path contains space, unsupported: ${part}" >&2
      exit 1
    fi
    printf "file %s\n" "${part}" >>"${list_path}"
  done
}

export_side_group() {
  local side="$1"
  local output="$2"
  local log="$3"
  shift 3
  local inputs=("$@")

  echo "[info] ${side}: group export -> ${output}" >"${log}"
  "${SCRIPT_DIR}/export_one.sh" "${inputs[@]}" "${output}" >>"${log}" 2>&1
}

export_side_segments() {
  local side="$1"
  local output="$2"
  local log="$3"
  shift 3
  local inputs=("$@")
  local side_work_dir="${SEGMENT_WORK_DIR}/${side}"
  local concat_list="${side_work_dir}/concat.txt"
  local tmp_output="${output%.mp4}.partial.mp4"
  local plan_path="${tmp_output}.plan.json"
  local parts=()
  local input stem part

  mkdir -p "${side_work_dir}"
  echo "[info] ${side}: segment export + concat -> ${output}" >"${log}"
  echo "[info] segment_work_dir=${side_work_dir}" >>"${log}"

  for input in "${inputs[@]}"; do
    stem="$(basename "${input%.*}")"
    part="${side_work_dir}/${stem}.mp4"
    parts+=("${part}")
    if [[ "${SKIP_EXISTING_PARTS}" == "1" ]] && pano_readable "${part}" "${input}"; then
      echo "[skip] existing segment panorama: ${part}" >>"${log}"
      continue
    fi
    echo "[info] exporting segment: ${input} -> ${part}" >>"${log}"
    "${SCRIPT_DIR}/export_one.sh" "${input}" "${part}" >>"${log}" 2>&1
  done

  write_concat_list "${concat_list}" "${parts[@]}"
  rm -f "${tmp_output}"
  write_pano_plan "${plan_path}" "${inputs[@]}"
  echo "[info] concat segments -> ${output}" >>"${log}"
  ffmpeg -hide_banner -y -f concat -safe 0 -i "${concat_list}" \
    -map 0:v:0 -an -c:v copy -f mp4 "${tmp_output}" >>"${log}" 2>&1
  "${PYTHON_BIN}" "${ARTIFACT_HELPER}" pano-publish \
    --partial "${tmp_output}" \
    --output "${output}" \
    --plan "${plan_path}" >>"${log}" 2>&1
}

export_side() {
  local side="$1"
  local output="$2"
  local log="$3"
  local mode="$4"
  shift 4
  local inputs=("$@")

  if [[ "${SKIP_EXISTING_PANO}" == "1" ]] && pano_readable "${output}" "${inputs[@]}" >>"${log}" 2>&1; then
    echo "[skip] complete ${side} panorama: ${output}" >>"${log}"
    return 0
  fi

  if [[ "${mode}" == "segments" ]]; then
    export_side_segments "${side}" "${output}" "${log}" "${inputs[@]}"
  elif [[ "${mode}" == "group" ]]; then
    export_side_group "${side}" "${output}" "${log}" "${inputs[@]}"
  else
    echo "[error] invalid INSV input mode for ${side}: ${mode}" >&2
    return 1
  fi
}

left_inputs=()
right_inputs=()
discover_side_inputs left left_inputs
discover_side_inputs right right_inputs
left_mode="$(side_mode left "${left_inputs[@]}")"
right_mode="$(side_mode right "${right_inputs[@]}")"

LEFT_LOG="${LEFT_LOG:-${LEFT_OUT}.export.log}"
RIGHT_LOG="${RIGHT_LOG:-${RIGHT_OUT}.export.log}"

echo "[info] input_dir=${INPUT_DIR}"
echo "[info] insv_input_mode=${INSV_INPUT_MODE}"
echo "[info] left mode: ${left_mode}"
echo "[info] left inputs:"
printf "       %s\n" "${left_inputs[@]}"
echo "[info] left output:  ${LEFT_OUT}"
echo "[info] right mode: ${right_mode}"
echo "[info] right inputs:"
printf "       %s\n" "${right_inputs[@]}"
echo "[info] right output: ${RIGHT_OUT}"

echo "[info] left_log=${LEFT_LOG}"
echo "[info] right_log=${RIGHT_LOG}"

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo "[dry-run] no export started"
  exit 0
fi

echo "[info] exporting left/right panoramas concurrently"
export_side left "${LEFT_OUT}" "${LEFT_LOG}" "${left_mode}" "${left_inputs[@]}" &
left_pid=$!
export_side right "${RIGHT_OUT}" "${RIGHT_LOG}" "${right_mode}" "${right_inputs[@]}" &
right_pid=$!

left_status=0
right_status=0
wait "${left_pid}" || left_status=$?
wait "${right_pid}" || right_status=$?

if [[ "${left_status}" != "0" ]]; then
  echo "[error] left export failed; see ${LEFT_LOG}" >&2
fi
if [[ "${right_status}" != "0" ]]; then
  echo "[error] right export failed; see ${RIGHT_LOG}" >&2
fi
if [[ "${left_status}" != "0" || "${right_status}" != "0" ]]; then
  exit 1
fi

echo "[done] left/right panorama export finished"
