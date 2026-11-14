#!/usr/bin/env bash
set -euo pipefail

# Parse this complete multi-hour stage before starting any work.  Bash normally
# reads a top-level script incrementally; editing that file while a child
# process is running can otherwise leave the live shell reading a different
# version at the old byte offset.  Keeping the body in one function makes a
# syntax error fail immediately and freezes the parsed body for this run.
main() {

# Bimanual gripper estimation wrapper.
#
# This script does not change the single-arm run_03_gripper_estimation.sh. It
# runs the same estimator once for each arm episode produced by
# _01_raw_extrect/run_01_raw_extract_biarm.sh.

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
  latest_biarm_dir="$(
    find "${DATA_ROOT}" -maxdepth 1 -type d \( -name "pp_biarm_*" -o -name "biarm_pp_*" \) -print 2>/dev/null \
      | sort \
      | tail -n 1 \
      || true
  )"
  if [[ -n "${latest_biarm_dir}" ]]; then
    INPUT_DIR="${latest_biarm_dir}"
  else
    INPUT_DIR="${DATA_ROOT}/instaumi/biarm_pp_630"
  fi
fi
TMP_ROOT="${TMP_ROOT:-${INPUT_DIR}/tmp}"
GRIPPER_CALIBRATION_JSON="${GRIPPER_CALIBRATION_JSON:-${REPO_ROOT}/config/gripper_calibration.json}"
LEFT_GRIPPER_CALIBRATION_JSON="${LEFT_GRIPPER_CALIBRATION_JSON:-${GRIPPER_CALIBRATION_JSON}}"
RIGHT_GRIPPER_CALIBRATION_JSON="${RIGHT_GRIPPER_CALIBRATION_JSON:-${GRIPPER_CALIBRATION_JSON}}"
TIMESTAMP_LOG="${TIMESTAMP_LOG:-${INPUT_DIR}/timestamp_log.jsonl}"
ACTIVE_SEGMENTS="${ACTIVE_SEGMENTS:-1}"
if [[ "${ACTIVE_SEGMENTS}" != "0" && "${ACTIVE_SEGMENTS}" != "1" ]]; then
  echo "[error] ACTIVE_SEGMENTS must be 0 or 1" >&2
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

# The top-level pipeline already owns one adaptive controller for its complete
# systemd scope.  When this stage is invoked directly, install the same
# host-aware ceiling here so its multiprocessing workers cannot consume the
# desktop's final CPU headroom.
if [[ "${ADAPTIVE_CPU_LIMIT:-1}" == "1" \
      && "${INSTAUMI_CPU_CONTROLLER_ACTIVE:-0}" != "1" \
      && "${INSTAUMI_CPU_SCOPE_ENTERED:-0}" != "1" ]]; then
  current_cgroup="$(awk -F: '$1 == "0" {print $3}' "/proc/$$/cgroup")"
  current_unit="${current_cgroup##*/}"
  if [[ "$(systemctl --user show "${current_unit}" --property=LoadState --value 2>/dev/null || true)" != "loaded" ]]; then
    # Login session scopes are owned by the system manager and cannot be
    # throttled by an unprivileged user.  Re-enter this foreground command in
    # a transient user scope; systemd-run remains attached and returns the
    # stage's real exit status.
    exec systemd-run --user --scope --quiet \
      env INSTAUMI_CPU_SCOPE_ENTERED=1 \
      bash "$0" "$@"
  fi
fi

ADAPTIVE_CPU_CONTROLLER_PID=""
stop_adaptive_cpu_controller() {
  if [[ -n "${ADAPTIVE_CPU_CONTROLLER_PID}" ]] && kill -0 "${ADAPTIVE_CPU_CONTROLLER_PID}" 2>/dev/null; then
    kill -TERM "${ADAPTIVE_CPU_CONTROLLER_PID}" 2>/dev/null || true
    wait "${ADAPTIVE_CPU_CONTROLLER_PID}" 2>/dev/null || true
  fi
}
trap stop_adaptive_cpu_controller EXIT

if [[ "${ADAPTIVE_CPU_LIMIT:-1}" == "1" && "${INSTAUMI_CPU_CONTROLLER_ACTIVE:-0}" != "1" ]]; then
  "${PYTHON}" "${REPO_ROOT}/dataprocess/adaptive_cpu_quota.py" \
    --pid "$$" \
    --target-percent "${CPU_TARGET_PERCENT:-95}" \
    --interval-sec "${CPU_ADAPT_INTERVAL_SEC:-5}" \
    --minimum-cores "${CPU_MINIMUM_CORES:-0.25}" \
    --cpu-weight "${CPU_PIPELINE_WEIGHT:-50}" &
  ADAPTIVE_CPU_CONTROLLER_PID=$!
  sleep 0.2
  if ! kill -0 "${ADAPTIVE_CPU_CONTROLLER_PID}" 2>/dev/null; then
    wait "${ADAPTIVE_CPU_CONTROLLER_PID}" || true
    ADAPTIVE_CPU_CONTROLLER_PID=""
    echo "[warn] adaptive CPU controller unavailable; _03 will continue without a CPU quota" >&2
  else
    export INSTAUMI_CPU_CONTROLLER_ACTIVE=1
  fi
fi

BIARM_SESSION="${BIARM_SESSION:-$(basename "${INPUT_DIR}")}"
DEFAULT_BIARM_TMP_ROOT="${TMP_ROOT}"
if [[ -z "${BIARM_LAYOUT:-}" ]]; then
  if [[ -f "${TMP_ROOT}/left/01_raw_extract/manifest.json" ]]; then
    BIARM_LAYOUT="grouped"
  elif [[ -f "${TMP_ROOT}/${BIARM_SESSION}/left/01_raw_extract/manifest.json" ]]; then
    BIARM_LAYOUT="grouped"
    DEFAULT_BIARM_TMP_ROOT="${TMP_ROOT}/${BIARM_SESSION}"
  elif [[ -f "${TMP_ROOT}/${BIARM_SESSION}_left/01_raw_extract/manifest.json" ]]; then
    BIARM_LAYOUT="legacy"
  else
    BIARM_LAYOUT="grouped"
  fi
fi
if [[ "${BIARM_LAYOUT}" == "legacy" ]]; then
  BIARM_TMP_ROOT="${BIARM_TMP_ROOT:-${DEFAULT_BIARM_TMP_ROOT}}"
  LEFT_EPISODE="${LEFT_EPISODE:-${BIARM_SESSION}_left}"
  RIGHT_EPISODE="${RIGHT_EPISODE:-${BIARM_SESSION}_right}"
else
  BIARM_TMP_ROOT="${BIARM_TMP_ROOT:-${DEFAULT_BIARM_TMP_ROOT}}"
  LEFT_EPISODE="${LEFT_EPISODE:-left}"
  RIGHT_EPISODE="${RIGHT_EPISODE:-right}"
fi

require_file() {
  local path="$1"
  local label="$2"
  if [[ -z "${path}" || ! -f "${path}" ]]; then
    echo "[error] missing ${label}: ${path}" >&2
    exit 1
  fi
}

require_raw_manifest() {
  local episode="$1"
  require_file "${BIARM_TMP_ROOT}/${episode}/01_raw_extract/manifest.json" "${episode} raw manifest"
}

side_marker_value() {
  local side="$2"
  local which="$1"
  if [[ "${side}" == "left" ]]; then
    if [[ "${which}" == "a" ]]; then
      echo "${LEFT_MARKER_A:-0}"
    else
      echo "${LEFT_MARKER_B:-3}"
    fi
  else
    if [[ "${which}" == "a" ]]; then
      echo "${RIGHT_MARKER_A:-${MARKER_A:-1}}"
    else
      echo "${RIGHT_MARKER_B:-${MARKER_B:-2}}"
    fi
  fi
}

side_calibration_json() {
  local side="$1"
  if [[ "${side}" == "left" ]]; then
    echo "${LEFT_GRIPPER_CALIBRATION_JSON}"
  else
    echo "${RIGHT_GRIPPER_CALIBRATION_JSON}"
  fi
}

append_optional_args() {
  if [[ "${RAW_INSV_INPUT:-1}" == "1" ]]; then
    cmd+=(--raw-insv-input)
  else
    cmd+=(--no-raw-insv-input)
  fi
  if [[ "${CONSTANT_ZERO:-0}" == "1" ]]; then
    cmd+=(--constant-zero)
  fi
  if [[ "${WRITE_ANNOTATED:-0}" == "1" ]]; then
    cmd+=(--write-annotated)
  fi
  if [[ -n "${PX_TO_METER_SCALE:-}" ]]; then
    cmd+=(--px-to-meter-scale "${PX_TO_METER_SCALE}")
  fi
  if [[ -n "${CALIBRATION_DISTANCE_PX:-}" ]]; then
    cmd+=(--calibration-distance-px "${CALIBRATION_DISTANCE_PX}")
    cmd+=(--calibration-distance-m "${CALIBRATION_DISTANCE_M:-0.10}")
  fi
  if [[ -n "${MAX_FRAMES:-}" ]]; then
    cmd+=(--max-frames "${MAX_FRAMES}")
  fi
  if [[ -n "${PROGRESS_EVERY:-}" ]]; then
    cmd+=(--progress-every "${PROGRESS_EVERY}")
  fi
  if [[ -n "${FAST_CACHE_ROOT:-}" ]]; then
    cmd+=(--fast-cache-root "${FAST_CACHE_ROOT}")
  fi
  if [[ "${FRONT_OPENPI_CACHE:-0}" == "1" ]]; then
    cmd+=(
      --front-openpi-cache
      --front-cache-reserve-gb "${FRONT_CACHE_RESERVE_GB:-100}"
    )
  else
    cmd+=(--no-front-openpi-cache)
  fi
  if [[ "${SPLIT_SOURCE:-auto}" == "1" ]]; then
    cmd+=(--split-source)
  elif [[ "${SPLIT_SOURCE:-auto}" == "0" ]]; then
    cmd+=(--no-split-source)
  fi
  if [[ "${SKIP_EXISTING:-1}" != "1" ]]; then
    cmd+=(--no-resume)
  fi
}

run_side() {
  local side="$1"
  local episode="$2"
  local mode="${3:-run}"
  local raw_manifest="${BIARM_TMP_ROOT}/${episode}/01_raw_extract/manifest.json"
  local frame_timestamps=""
  if [[ "${side}" == "left" ]]; then
    frame_timestamps="${LEFT_FRAME_TIMESTAMPS:-${BIARM_TMP_ROOT}/sync_calibration/left/calibrated_frame_timestamps.csv}"
  else
    frame_timestamps="${RIGHT_FRAME_TIMESTAMPS:-${BIARM_TMP_ROOT}/sync_calibration/right/calibrated_frame_timestamps.csv}"
  fi

  local cmd=(
    "${PYTHON}" "${SCRIPT_DIR}/estimate_gripper_width.py"
    --episode "${episode}"
    --tmp-root "${BIARM_TMP_ROOT}"
    --raw-manifest "${raw_manifest}"
    --marker-a "$(side_marker_value a "${side}")"
    --marker-b "$(side_marker_value b "${side}")"
    --marker-width-m "${MARKER_WIDTH_M:-0.018}"
    --gripper-calibration-json "$(side_calibration_json "${side}")"
    --missing-policy "${MISSING_POLICY:-ffill}"
    --workers "${GRIPPER_WORKERS:-0}"
    --concurrent-estimators "${ESTIMATOR_CONCURRENCY:-1}"
    --decode-threads "${DECODE_THREADS_PER_WORKER:-1}"
    --chunk-retries "${GRIPPER_CHUNK_RETRIES:-2}"
    --rotational-total-workers "${GRIPPER_ROTATIONAL_TOTAL_WORKERS:-4}"
    --chunk-size-frames "${GRIPPER_CHUNK_SIZE_FRAMES:-7200}"
    --input-cache-policy "${GRIPPER_INPUT_CACHE_POLICY:-auto}"
  )
  if [[ "${ARUCO_ADAPTIVE_RETRY:-1}" == "1" ]]; then
    cmd+=(
      --aruco-adaptive-retry
      --aruco-adaptive-thresh-constant "${ARUCO_ADAPTIVE_THRESH_CONSTANT:-15}"
      --aruco-adaptive-min-distance-px "${ARUCO_ADAPTIVE_MIN_DISTANCE_PX:-100}"
      --aruco-adaptive-max-distance-px "${ARUCO_ADAPTIVE_MAX_DISTANCE_PX:-600}"
      --aruco-adaptive-peer-consistency-px "${ARUCO_ADAPTIVE_PEER_CONSISTENCY_PX:-2}"
      --aruco-adaptive-long-run-diagnostic-ms "${ARUCO_ADAPTIVE_LONG_RUN_DIAGNOSTIC_MS:-${ARUCO_ADAPTIVE_MAX_RUN_DURATION_MS:-250}}"
    )
  else
    cmd+=(--no-aruco-adaptive-retry)
  fi
  if [[ "${SINGLE_MARKER_RECOVERY:-1}" == "1" ]]; then
    cmd+=(
      --single-marker-recovery
      --single-marker-min-dual-frames "${SINGLE_MARKER_MIN_DUAL_FRAMES:-1000}"
      --single-marker-min-dual-segments "${SINGLE_MARKER_MIN_DUAL_SEGMENTS:-10}"
      --single-marker-cv-folds "${SINGLE_MARKER_CV_FOLDS:-0}"
      --single-marker-max-cv-p95-m "${SINGLE_MARKER_MAX_CV_P95_M:-0.004}"
      --single-marker-max-cv-p99-m "${SINGLE_MARKER_MAX_CV_P99_M:-0.005}"
      --single-marker-max-cv-error-m "${SINGLE_MARKER_MAX_CV_ERROR_M:-0.006}"
      --single-marker-max-cv-bias-m "${SINGLE_MARKER_MAX_CV_BIAS_M:-0.0015}"
      --single-marker-min-cv-coverage "${SINGLE_MARKER_MIN_CV_COVERAGE:-0.99}"
      --single-marker-min-cv-predictions-per-role "${SINGLE_MARKER_MIN_CV_PREDICTIONS_PER_ROLE:-1000}"
      --single-marker-min-cv-episodes-per-role "${SINGLE_MARKER_MIN_CV_EPISODES_PER_ROLE:-10}"
      --single-marker-max-episode-p95-m "${SINGLE_MARKER_MAX_EPISODE_P95_M:-0.006}"
      --single-marker-max-midpoint-p95-px "${SINGLE_MARKER_MAX_MIDPOINT_P95_PX:-8}"
      --single-marker-max-axis-p95-deg "${SINGLE_MARKER_MAX_AXIS_P95_DEG:-1}"
      --single-marker-min-distance-px "${SINGLE_MARKER_MIN_DISTANCE_PX:-100}"
      --single-marker-max-distance-px "${SINGLE_MARKER_MAX_DISTANCE_PX:-600}"
      --single-marker-max-axis-deviation-deg "${SINGLE_MARKER_MAX_AXIS_DEVIATION_DEG:-5}"
      --single-marker-roi-margin-px "${SINGLE_MARKER_ROI_MARGIN_PX:-5}"
      --single-marker-midpoint-min-gate-px "${SINGLE_MARKER_MIDPOINT_MIN_GATE_PX:-25}"
      --single-marker-continuity-base-m "${SINGLE_MARKER_CONTINUITY_BASE_M:-0.005}"
      --single-marker-continuity-slew-mps "${SINGLE_MARKER_CONTINUITY_SLEW_MPS:-0.1}"
      --single-marker-anchor-horizon-ms "${SINGLE_MARKER_ANCHOR_HORIZON_MS:-250}"
      --single-marker-max-timeline-step-ms "${SINGLE_MARKER_MAX_TIMELINE_STEP_MS:-50}"
    )
  else
    cmd+=(--no-single-marker-recovery)
  fi
  if [[ "${CONTINUOUS_GRIPPER_QUALITY_AUDIT:-0}" == "1" ]]; then
    cmd+=(
      --continuous-gripper-quality-audit
      --quality-min-detections-per-segment "${GRIPPER_QUALITY_MIN_DETECTIONS_PER_SEGMENT:-2}"
      --quality-max-internal-gap-ms "${GRIPPER_QUALITY_MAX_INTERNAL_GAP_MS:-500}"
      --quality-max-edge-extrapolation-ms "${GRIPPER_QUALITY_MAX_EDGE_EXTRAPOLATION_MS:-250}"
      --quality-min-width-m "${GRIPPER_QUALITY_MIN_WIDTH_M:-0}"
      --quality-max-width-m "${GRIPPER_QUALITY_MAX_WIDTH_M:-0.11}"
    )
  else
    cmd+=(--no-continuous-gripper-quality-audit)
  fi
  if [[ -n "${GRIPPER_ZERO_BELOW_M:-}" ]]; then
    cmd+=(--gripper-zero-below-m "${GRIPPER_ZERO_BELOW_M}")
  fi
  if [[ "${ACTIVE_SEGMENTS}" == "1" ]]; then
    cmd+=(
      --frame-timestamps "${frame_timestamps}"
      --timestamp-log "${TIMESTAMP_LOG}"
      --segment-time-offset-sec "${SEGMENT_TIME_OFFSET_SEC:-0}"
      --min-segment-frames "${MIN_SEGMENT_FRAMES:-2}"
    )
  fi
  append_optional_args
  if [[ "${mode}" == "prepare-cache" ]]; then
    cmd+=(--prepare-cache-only)
  fi

  echo "[info] ${mode} ${side}: episode=${episode}"
  printf '+'
  printf ' %q' "${cmd[@]}"
  printf '\n'

  if [[ "${DRY_RUN:-0}" != "1" ]]; then
    "${cmd[@]}"
  fi
}

write_biarm_manifest() {
  local manifest_dir="${BIARM_TMP_ROOT}/03_gripper_estimation_biarm"
  local manifest_path="${manifest_dir}/manifest.json"
  local manifest_tmp="${manifest_path}.tmp.$$"
  mkdir -p "${manifest_dir}"
  cat > "${manifest_tmp}" <<EOF
{
  "schema": "instaumi_biarm_gripper_estimation_v1",
  "session": "${BIARM_SESSION}",
  "input_dir": "${INPUT_DIR}",
  "tmp_root": "${BIARM_TMP_ROOT}",
  "calibration_json": "${GRIPPER_CALIBRATION_JSON}",
  "calibration_json_by_side": {
    "left": "${LEFT_GRIPPER_CALIBRATION_JSON}",
    "right": "${RIGHT_GRIPPER_CALIBRATION_JSON}"
  },
  "missing_policy": "${MISSING_POLICY:-ffill}",
  "active_segments": ${ACTIVE_SEGMENTS},
  "front_openpi_cache": ${FRONT_OPENPI_CACHE:-0},
  "timestamp_log": "${TIMESTAMP_LOG}",
  "left": {
    "episode": "${LEFT_EPISODE}",
    "calibration_json": "${LEFT_GRIPPER_CALIBRATION_JSON}",
    "raw_manifest": "${BIARM_TMP_ROOT}/${LEFT_EPISODE}/01_raw_extract/manifest.json",
    "gripper_dir": "${BIARM_TMP_ROOT}/${LEFT_EPISODE}/03_gripper_estimation",
    "gripper_csv": "${BIARM_TMP_ROOT}/${LEFT_EPISODE}/03_gripper_estimation/gripper_width.csv"
  },
  "right": {
    "episode": "${RIGHT_EPISODE}",
    "calibration_json": "${RIGHT_GRIPPER_CALIBRATION_JSON}",
    "raw_manifest": "${BIARM_TMP_ROOT}/${RIGHT_EPISODE}/01_raw_extract/manifest.json",
    "gripper_dir": "${BIARM_TMP_ROOT}/${RIGHT_EPISODE}/03_gripper_estimation",
    "gripper_csv": "${BIARM_TMP_ROOT}/${RIGHT_EPISODE}/03_gripper_estimation/gripper_width.csv"
  }
}
EOF
  mv -f "${manifest_tmp}" "${manifest_path}"
  echo "[info] bimanual manifest=${manifest_path}"
}

if [[ ! -d "${INPUT_DIR}" ]]; then
  echo "[error] INPUT_DIR does not exist: ${INPUT_DIR}" >&2
  exit 1
fi
require_file "${LEFT_GRIPPER_CALIBRATION_JSON}" "left gripper calibration JSON"
require_file "${RIGHT_GRIPPER_CALIBRATION_JSON}" "right gripper calibration JSON"
require_raw_manifest "${LEFT_EPISODE}"
require_raw_manifest "${RIGHT_EPISODE}"
if [[ "${ACTIVE_SEGMENTS}" == "1" ]]; then
  require_file "${TIMESTAMP_LOG}" "timestamp log"
  require_file "${LEFT_FRAME_TIMESTAMPS:-${BIARM_TMP_ROOT}/sync_calibration/left/calibrated_frame_timestamps.csv}" "left calibrated frame timestamps"
  require_file "${RIGHT_FRAME_TIMESTAMPS:-${BIARM_TMP_ROOT}/sync_calibration/right/calibrated_frame_timestamps.csv}" "right calibrated frame timestamps"
fi

echo "[info] input_dir=${INPUT_DIR}"
echo "[info] tmp_root=${TMP_ROOT}"
echo "[info] biarm_tmp_root=${BIARM_TMP_ROOT}"
echo "[info] biarm_layout=${BIARM_LAYOUT}"
echo "[info] biarm_session=${BIARM_SESSION}"
echo "[info] left_episode=${LEFT_EPISODE}"
echo "[info] right_episode=${RIGHT_EPISODE}"
echo "[info] python=${PYTHON}"
echo "[info] adaptive_cpu_target_percent=${CPU_TARGET_PERCENT:-95} minimum_cores=${CPU_MINIMUM_CORES:-0.25}"
echo "[info] calibration_json_legacy_fallback=${GRIPPER_CALIBRATION_JSON}"
echo "[info] calibration_json_left=${LEFT_GRIPPER_CALIBRATION_JSON}"
echo "[info] calibration_json_right=${RIGHT_GRIPPER_CALIBRATION_JSON}"
echo "[info] markers_left=$(side_marker_value a left),$(side_marker_value b left)"
echo "[info] markers_right=$(side_marker_value a right),$(side_marker_value b right)"
echo "[info] missing_policy=${MISSING_POLICY:-ffill}"
echo "[info] aruco_adaptive_retry=${ARUCO_ADAPTIVE_RETRY:-1} adaptive_thresh_constant=${ARUCO_ADAPTIVE_THRESH_CONSTANT:-15} distance_px=[${ARUCO_ADAPTIVE_MIN_DISTANCE_PX:-100},${ARUCO_ADAPTIVE_MAX_DISTANCE_PX:-600}] peer_consistency_px=${ARUCO_ADAPTIVE_PEER_CONSISTENCY_PX:-2} long_run_diagnostic_ms=${ARUCO_ADAPTIVE_LONG_RUN_DIAGNOSTIC_MS:-${ARUCO_ADAPTIVE_MAX_RUN_DURATION_MS:-250}} long_run_is_not_rejection=1"
echo "[info] single_marker_recovery=${SINGLE_MARKER_RECOVERY:-1} validation=episode-held-out geometry=arm-specific-midpoint"
echo "[info] continuous_gripper_quality_audit=${CONTINUOUS_GRIPPER_QUALITY_AUDIT:-0} min_detections=${GRIPPER_QUALITY_MIN_DETECTIONS_PER_SEGMENT:-2} internal_gap_ms=${GRIPPER_QUALITY_MAX_INTERNAL_GAP_MS:-500} edge_extrapolation_ms=${GRIPPER_QUALITY_MAX_EDGE_EXTRAPOLATION_MS:-250} width_m=[${GRIPPER_QUALITY_MIN_WIDTH_M:-0},${GRIPPER_QUALITY_MAX_WIDTH_M:-0.11}]"
echo "[info] gripper_zero_below_m=${GRIPPER_ZERO_BELOW_M:-<disabled>}"
echo "[info] active_segments=${ACTIVE_SEGMENTS} timestamp_log=${TIMESTAMP_LOG}"
echo "[info] skip_existing=${SKIP_EXISTING:-1}"
echo "[info] workers_per_arm=${GRIPPER_WORKERS:-0} decode_threads=${DECODE_THREADS_PER_WORKER:-1}"
echo "[info] chunk_retries=${GRIPPER_CHUNK_RETRIES:-2} rotational_total_workers=${GRIPPER_ROTATIONAL_TOTAL_WORKERS:-4}"
echo "[info] chunk_size_frames=${GRIPPER_CHUNK_SIZE_FRAMES:-7200} split_source=${SPLIT_SOURCE:-auto} fast_cache_root=${FAST_CACHE_ROOT:-disabled}"
echo "[info] input_cache_policy=${GRIPPER_INPUT_CACHE_POLICY:-auto} (auto bypasses same-physical-device copies)"
echo "[info] raw_insv_input=${RAW_INSV_INPUT:-1} source_stream=0:v:0 native_resolution_before_aruco=1"
echo "[info] front_openpi_cache=${FRONT_OPENPI_CACHE:-0} decode_fanout=aruco+openpi224"
echo "[info] front_cache_reserve_gb=${FRONT_CACHE_RESERVE_GB:-100}"

if [[ "${DRY_RUN:-0}" != "1" ]]; then
  if ! "${PYTHON}" - <<'PY'
import sys
try:
    import av
    import cv2
    from cv2 import aruco
except Exception as exc:
    print(f"[error] Python executable cannot import PyAV and cv2/aruco: {sys.executable}", file=sys.stderr)
    print(f"[error] {exc}", file=sys.stderr)
    print("[hint] Install OpenCV with ArUco into this exact Python environment:", file=sys.stderr)
    print(f"[hint] {sys.executable} -m pip install opencv-contrib-python", file=sys.stderr)
    raise SystemExit(1)
print(f"[info] cv2={cv2.__version__} python={sys.executable}")
print(f"[info] pyav={av.__version__} decoder=PTS-indexed 0:v:0 BGR24 ITU-601")
PY
  then
    exit 1
  fi
fi

if [[ -n "${FAST_CACHE_ROOT:-}" && "${CONSTANT_ZERO:-0}" != "1" && "${WRITE_ANNOTATED:-0}" != "1" && "${DRY_RUN:-0}" != "1" ]]; then
  echo "[info] preparing source access sequentially (same-device direct or cross-device verified cache)"
  ESTIMATOR_CONCURRENCY=1
  if [[ "${RUN_LEFT:-1}" == "1" ]]; then
    run_side "left" "${LEFT_EPISODE}" "prepare-cache"
  fi
  if [[ "${RUN_RIGHT:-1}" == "1" ]]; then
    run_side "right" "${RIGHT_EPISODE}" "prepare-cache"
  fi
fi

if [[ "${PARALLEL_ARMS:-1}" == "1" && "${RUN_LEFT:-1}" == "1" && "${RUN_RIGHT:-1}" == "1" ]]; then
  echo "[info] parallel_arms=1"
  ESTIMATOR_CONCURRENCY=2
  run_side "left" "${LEFT_EPISODE}" &
  left_pid=$!
  run_side "right" "${RIGHT_EPISODE}" &
  right_pid=$!

  left_status=0
  right_status=0
  wait "${left_pid}" || left_status=$?
  wait "${right_pid}" || right_status=$?
  if [[ "${left_status}" != "0" || "${right_status}" != "0" ]]; then
    echo "[error] gripper estimation failed: left=${left_status}, right=${right_status}" >&2
    exit 1
  fi
else
  echo "[info] parallel_arms=0"
  ESTIMATOR_CONCURRENCY=1
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
  write_biarm_manifest
else
  echo "[dry-run] no files written"
fi

}
main "$@"
