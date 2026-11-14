#!/usr/bin/env bash
set -euo pipefail

# Parse and freeze the complete conversion wrapper before the expensive Python
# conversion starts, so source-tree updates cannot alter a live run.
main() {

# Dedicated bimanual HTC conversion. It does not invoke the bimanual SLAM
# shell; only the Python dataset algorithm is shared.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DATA_ROOT="${DATA_ROOT:-$(cd "${REPO_ROOT}/.." && pwd)/data}"
INPUT_DIR="${1:-${INPUT_DIR:-${HTC_INPUT_DIR:-}}}"
if [[ -z "${INPUT_DIR}" || ! -d "${INPUT_DIR}" ]]; then
  echo "Usage: bash $(basename "$0") /path/to/bimanual_session" >&2
  exit 2
fi

SESSION="${BIARM_SESSION:-$(basename "${INPUT_DIR}")}"
TMP_ROOT="${TMP_ROOT:-${INPUT_DIR}/tmp}"
if [[ -f "${TMP_ROOT}/right/01_raw_extract/manifest.json" ]]; then
  SESSION_TMP_ROOT="${BIARM_TMP_ROOT:-${TMP_ROOT}}"
  RIGHT_EPISODE="${RIGHT_EPISODE:-right}"
  LEFT_EPISODE="${LEFT_EPISODE:-left}"
elif [[ -f "${TMP_ROOT}/${SESSION}/right/01_raw_extract/manifest.json" ]]; then
  SESSION_TMP_ROOT="${BIARM_TMP_ROOT:-${TMP_ROOT}/${SESSION}}"
  RIGHT_EPISODE="${RIGHT_EPISODE:-right}"
  LEFT_EPISODE="${LEFT_EPISODE:-left}"
elif [[ -f "${TMP_ROOT}/${SESSION}_right/01_raw_extract/manifest.json" ]]; then
  SESSION_TMP_ROOT="${BIARM_TMP_ROOT:-${TMP_ROOT}}"
  RIGHT_EPISODE="${RIGHT_EPISODE:-${SESSION}_right}"
  LEFT_EPISODE="${LEFT_EPISODE:-${SESSION}_left}"
else
  SESSION_TMP_ROOT="${BIARM_TMP_ROOT:-${TMP_ROOT}}"
  RIGHT_EPISODE="${RIGHT_EPISODE:-right}"
  LEFT_EPISODE="${LEFT_EPISODE:-left}"
fi

if [[ -n "${TRACKER_CSV:-}" ]]; then
  RIGHT_HTC_CSV="${RIGHT_HTC_CSV:-${TRACKER_CSV}}"
  LEFT_HTC_CSV="${LEFT_HTC_CSV:-${TRACKER_CSV}}"
else
  RIGHT_HTC_CSV="${RIGHT_HTC_CSV:-${INPUT_DIR}/htc_right.csv}"
  LEFT_HTC_CSV="${LEFT_HTC_CSV:-${INPUT_DIR}/htc_left.csv}"
fi
if [[ ! -f "${RIGHT_HTC_CSV}" && -f "${INPUT_DIR}/tracker_data.csv" ]]; then
  RIGHT_HTC_CSV="${INPUT_DIR}/tracker_data.csv"
fi
if [[ ! -f "${LEFT_HTC_CSV}" && -f "${INPUT_DIR}/tracker_data.csv" ]]; then
  LEFT_HTC_CSV="${INPUT_DIR}/tracker_data.csv"
fi
PYTHON="${PYTHON:-python3}"
DIRECT_OPENPI_IMAGES="${DIRECT_OPENPI_IMAGES:-0}"
if [[ "${DIRECT_OPENPI_IMAGES}" != "0" && "${DIRECT_OPENPI_IMAGES}" != "1" ]]; then
  echo "[error] DIRECT_OPENPI_IMAGES must be 0 or 1" >&2
  exit 2
fi
PANORAMA_NUMERIC_ONLY="${PANORAMA_NUMERIC_ONLY:-0}"
if [[ "${PANORAMA_NUMERIC_ONLY}" != "0" && "${PANORAMA_NUMERIC_ONLY}" != "1" ]]; then
  echo "[error] PANORAMA_NUMERIC_ONLY must be 0 or 1" >&2
  exit 2
fi
if [[ "${PANORAMA_NUMERIC_ONLY}" == "1" && "${DIRECT_OPENPI_IMAGES}" != "1" ]]; then
  echo "[error] PANORAMA_NUMERIC_ONLY=1 requires DIRECT_OPENPI_IMAGES=1" >&2
  exit 2
fi
DIRECT_IMAGE_PARALLEL_ARMS="${DIRECT_IMAGE_PARALLEL_ARMS:-1}"
if [[ "${DIRECT_IMAGE_PARALLEL_ARMS}" != "0" && "${DIRECT_IMAGE_PARALLEL_ARMS}" != "1" ]]; then
  echo "[error] DIRECT_IMAGE_PARALLEL_ARMS must be 0 or 1" >&2
  exit 2
fi
OUTPUT_ROOT="${OUTPUT_ROOT:-${INPUT_DIR}/lerobot}"
FAST_WORK_ROOT="${FAST_WORK_ROOT:-${INPUT_DIR}/instaumi-dataprocess-cache}"
TASK_PROMPT="${TASK_PROMPT:-bimanual_instaumi_task}"
if [[ "${DIRECT_OPENPI_IMAGES}" == "1" && "${PREVIEW_EVERY_N:-0}" != "0" ]]; then
  echo "[error] direct OpenPI image mode requires PREVIEW_EVERY_N=0" >&2
  exit 2
fi
TIMESTAMP_LOG="${TIMESTAMP_LOG:-${INPUT_DIR}/timestamp_log.jsonl}"
# Clock calibration and gripper calibration are different artifacts.  Never
# inherit the generic CALIBRATION_REPORT variable here: production wrappers
# use that name for the shared gripper-width calibration report.
SYNC_CALIBRATION_REPORT="${SYNC_CALIBRATION_REPORT:-${SESSION_TMP_ROOT}/sync_calibration/calibration.json}"
ALLOW_UNCALIBRATED_TIMELINE="${ALLOW_UNCALIBRATED_TIMELINE:-${ALLOW_UNCALIBRATED_SYNC:-0}}"
if [[ "${ALLOW_UNCALIBRATED_TIMELINE}" == "1" ]]; then
  REQUIRE_CALIBRATED_TIMESTAMPS=0
else
  REQUIRE_CALIBRATED_TIMESTAMPS="${REQUIRE_CALIBRATED_TIMESTAMPS:-1}"
fi
RIGHT_FRAME_TIMESTAMPS="${RIGHT_FRAME_TIMESTAMPS:-}"
LEFT_FRAME_TIMESTAMPS="${LEFT_FRAME_TIMESTAMPS:-}"

validate_output_root() {
  local output_abs tmp_abs input_abs output_parent output_name
  output_abs="$(realpath -m "${OUTPUT_ROOT}")"
  tmp_abs="$(realpath -m "${TMP_ROOT}")"
  input_abs="$(realpath -m "${INPUT_DIR}")"
  if [[ "${output_abs}" == "/" || "${output_abs}" == "${tmp_abs}" || "${output_abs}" == "${tmp_abs}/"* || "${tmp_abs}" == "${output_abs}/"* ]]; then
    echo "[error] OUTPUT_ROOT must not replace, contain, or live inside TMP_ROOT: ${OUTPUT_ROOT}" >&2
    exit 2
  fi
  if [[ "${output_abs}" == "${input_abs}" || "${input_abs}" == "${output_abs}/"* ]]; then
    echo "[error] OUTPUT_ROOT must not replace or contain the capture session: ${INPUT_DIR}" >&2
    exit 2
  fi
  if [[ "${output_abs}" == "${input_abs}/"* ]]; then
    output_parent="$(dirname "${output_abs}")"
    output_name="$(basename "${output_abs}")"
    if [[ "${output_parent}" != "${input_abs}" ]] || { [[ "${output_name}" != "lerobot" ]] && [[ "${output_name}" != *_lerobot* ]]; }; then
      echo "[error] A dataset inside INPUT_DIR must be the top-level lerobot or *_lerobot* directory: ${OUTPUT_ROOT}" >&2
      exit 2
    fi
  fi
}
validate_output_root

if [[ -e "${OUTPUT_ROOT}" && "${OVERWRITE:-0}" != "1" && "${RESUME_CONVERSION:-1}" != "1" ]]; then
  echo "[error] output already exists; it is not assumed complete or current: ${OUTPUT_ROOT}" >&2
  echo "[hint] use a new OUTPUT_ROOT, or set OVERWRITE=1 explicitly to rebuild it" >&2
  exit 2
fi

if [[ -n "${RIGHT_FRAME_TIMESTAMPS}" && -z "${LEFT_FRAME_TIMESTAMPS}" ]] || \
   [[ -z "${RIGHT_FRAME_TIMESTAMPS}" && -n "${LEFT_FRAME_TIMESTAMPS}" ]]; then
  echo "[error] RIGHT_FRAME_TIMESTAMPS and LEFT_FRAME_TIMESTAMPS must be provided together" >&2
  exit 2
fi

if [[ -z "${RIGHT_FRAME_TIMESTAMPS}" && -f "${SYNC_CALIBRATION_REPORT}" ]]; then
  mapfile -t calibration_values < <(
    "${PYTHON}" - "${SYNC_CALIBRATION_REPORT}" <<'PY'
import json
import sys
from pathlib import Path

report_path = Path(sys.argv[1]).expanduser().resolve()
report = json.loads(report_path.read_text(encoding="utf-8"))
contract = report.get("clock_contract") or {}
assignment_record = report.get("tracker_assignment") or {}
assignment = assignment_record.get("camera_to_tracker") or {}
strict_report = (
    report.get("status") == "accepted"
    and report.get("schema") == "instaumi-no-slam-clock-calibration-v2-monotonic-raw"
    and contract.get("domain") == "host_clock_monotonic_raw"
    and contract.get("alignment_policy") == "single_domain_only_fail_closed"
    and assignment_record.get("status") == "accepted"
    and set(assignment) == {"left", "right"}
    and set(assignment.values()) == {"left", "right"}
)

def timeline_path(arm: str) -> str:
    arm_report = (report.get("arms") or {}).get(arm) or {}
    timeline = arm_report.get("frame_timeline") or {}
    value = (
        timeline.get("path")
        or arm_report.get("calibrated_frame_timestamps_csv")
        or arm_report.get("calibrated_timestamps_csv")
        or ""
    )
    if not value:
        return ""
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = report_path.parent / path
    return str(path.resolve())

def tracker_value(arm: str, key: str) -> str:
    arm_report = (report.get("arms") or {}).get(arm) or {}
    if key == "path":
        value = (arm_report.get("htc") or {}).get("path") or ""
    else:
        value = arm_report.get("tracker_role") or ""
    if not value:
        return ""
    if key == "path":
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = report_path.parent / path
        return str(path.resolve())
    return str(value)

print("accepted" if strict_report else "invalid_strict_clock_contract")
print(timeline_path("right"))
print(timeline_path("left"))
print(tracker_value("right", "path"))
print(tracker_value("right", "role"))
print(tracker_value("left", "path"))
print(tracker_value("left", "role"))
PY
  )
  calibration_status="${calibration_values[0]:-}"
  if [[ "${calibration_status}" == "accepted" ]]; then
    RIGHT_FRAME_TIMESTAMPS="${calibration_values[1]:-}"
    LEFT_FRAME_TIMESTAMPS="${calibration_values[2]:-}"
    RIGHT_HTC_CSV="${calibration_values[3]:-}"
    RIGHT_HTC_ROLE="${calibration_values[4]:-}"
    LEFT_HTC_CSV="${calibration_values[5]:-}"
    LEFT_HTC_ROLE="${calibration_values[6]:-}"
    if [[ -z "${RIGHT_HTC_CSV}" || -z "${RIGHT_HTC_ROLE}" || -z "${LEFT_HTC_CSV}" || -z "${LEFT_HTC_ROLE}" ]]; then
      echo "[error] accepted sync calibration report has no complete audited tracker assignment" >&2
      exit 2
    fi
    echo "[info] using audited tracker assignment from calibration: right_camera->${RIGHT_HTC_ROLE} left_camera->${LEFT_HTC_ROLE}"
  elif [[ "${REQUIRE_CALIBRATED_TIMESTAMPS}" == "1" ]]; then
    echo "[error] sync calibration report is not accepted: ${SYNC_CALIBRATION_REPORT}" >&2
    echo "[hint] rerun calibration, or deliberately set ALLOW_UNCALIBRATED_TIMELINE=1" >&2
    exit 2
  fi
fi

if [[ "${REQUIRE_CALIBRATED_TIMESTAMPS}" == "1" ]]; then
  if [[ ! -f "${SYNC_CALIBRATION_REPORT}" ]]; then
    echo "[error] accepted sync calibration report is required: ${SYNC_CALIBRATION_REPORT}" >&2
    echo "[hint] run calibrate_no_slam_clock.py first, or deliberately set ALLOW_UNCALIBRATED_TIMELINE=1" >&2
    exit 2
  fi
  if [[ -z "${RIGHT_FRAME_TIMESTAMPS}" || -z "${LEFT_FRAME_TIMESTAMPS}" ]]; then
    echo "[error] accepted report did not resolve both calibrated frame timelines" >&2
    exit 2
  fi
fi

if [[ -n "${RIGHT_FRAME_TIMESTAMPS}" ]]; then
  if [[ ! -f "${RIGHT_FRAME_TIMESTAMPS}" || ! -f "${LEFT_FRAME_TIMESTAMPS}" ]]; then
    echo "[error] calibrated frame timestamp CSV is missing" >&2
    echo "        right=${RIGHT_FRAME_TIMESTAMPS}" >&2
    echo "        left=${LEFT_FRAME_TIMESTAMPS}" >&2
    exit 2
  fi
fi

for required in \
  "${SESSION_TMP_ROOT}/${RIGHT_EPISODE}/01_raw_extract/manifest.json" \
  "${SESSION_TMP_ROOT}/${LEFT_EPISODE}/01_raw_extract/manifest.json" \
  "${SESSION_TMP_ROOT}/${RIGHT_EPISODE}/03_gripper_estimation/gripper_width.csv" \
  "${SESSION_TMP_ROOT}/${LEFT_EPISODE}/03_gripper_estimation/gripper_width.csv" \
  "${RIGHT_HTC_CSV}" "${LEFT_HTC_CSV}" "${TIMESTAMP_LOG}"; do
  if [[ ! -f "${required}" ]]; then
    echo "[error] missing bimanual HTC input: ${required}" >&2
    exit 1
  fi
done

export MPLCONFIGDIR="${MPLCONFIGDIR:-${SESSION_TMP_ROOT}/.matplotlib_cache}"
export KMP_DUPLICATE_LIB_OK="${KMP_DUPLICATE_LIB_OK:-TRUE}"
export LEROBOT_VIDEO_ENCODING_WORKERS="${LEROBOT_VIDEO_ENCODING_WORKERS:-6}"
mkdir -p "${MPLCONFIGDIR}"

cmd=(
  "${PYTHON}" "${SCRIPT_DIR}/convert_bimanual_to_lerobot.py"
  --direct-from-raw
  --session-dir "${INPUT_DIR}"
  --timestamp-log "${TIMESTAMP_LOG}"
  --tmp-root "${SESSION_TMP_ROOT}"
  --right-episode "${RIGHT_EPISODE}"
  --left-episode "${LEFT_EPISODE}"
  --right-htc-csv "${RIGHT_HTC_CSV}"
  --left-htc-csv "${LEFT_HTC_CSV}"
  --right-htc-role "${RIGHT_HTC_ROLE:-htc_right}"
  --left-htc-role "${LEFT_HTC_ROLE:-htc_left}"
  --right-pose-sensor htc
  --left-pose-sensor htc
  --output-root "${OUTPUT_ROOT}"
  --fast-workspace "${FAST_WORKSPACE:-auto}"
  --fast-work-root "${FAST_WORK_ROOT}"
  --fast-work-reserve-gb "${FAST_WORK_RESERVE_GB:-100}"
  --output-reserve-gb "${OUTPUT_RESERVE_GB:-20}"
  --repo-id "${REPO_ID:-local/${SESSION}_bimanual_htc}"
  --task "${TASK_PROMPT}"
  --lerobot-env "${SCRIPT_DIR}"
  --time-align-mode "${TIME_ALIGN_MODE:-global_time}"
  --camera-pairing-mode "${CAMERA_PAIRING_MODE:-right_anchor_nearest}"
  --max-camera-time-diff-ms "${MAX_CAMERA_TIME_DIFF_MS:-10}"
  --hard-camera-time-diff-ms "${HARD_CAMERA_TIME_DIFF_MS:-20}"
  --max-htc-nearest-gap-ms "${MAX_HTC_NEAREST_GAP_MS:-10}"
  --max-htc-bracket-gap-ms "${MAX_HTC_BRACKET_GAP_MS:-20}"
  --max-host-clock-offset-step-ms "${MAX_HOST_CLOCK_OFFSET_STEP_MS:-5}"
  --hard-gap-reject-ms "${HARD_GAP_REJECT_MS:-50}"
  --htc-time-source "${HTC_TIME_SOURCE:-auto}"
  --segment-time-offset-sec "${SEGMENT_TIME_OFFSET_SEC:-0}"
  --min-segment-frames "${MIN_SEGMENT_FRAMES:-2}"
  --missing-gripper "${MISSING_GRIPPER:-skip}"
  --gripper-column "${GRIPPER_COLUMN:-auto}"
  --fps "${FPS:-60}"
  --down-sample-steps "${DOWN_SAMPLE_STEPS:-2}"
  --preview-every-n "${PREVIEW_EVERY_N:-0}"
  --preview-stride "${PREVIEW_STRIDE:-4}"
  --progress-every "${CONVERT_PROGRESS_EVERY:-500}"
  --batch-encoding-size "${BATCH_ENCODING_SIZE:-1}"
  --image-writer-processes "${IMAGE_WRITER_PROCESSES:-0}"
  --image-writer-threads "${IMAGE_WRITER_THREADS:-16}"
  --stats-image-threads "${STATS_IMAGE_THREADS:-8}"
  --png-compress-level "${PNG_COMPRESS_LEVEL:-1}"
  --episode-write-batch-frames "${EPISODE_WRITE_BATCH_FRAMES:-256}"
  --video-seek-threshold-frames "${VIDEO_SEEK_THRESHOLD_FRAMES:-300}"
  --split-by-segments
)
if [[ -n "${GRIPPER_ZERO_BELOW_M:-}" ]]; then
  cmd+=(--gripper-zero-below-m "${GRIPPER_ZERO_BELOW_M}")
fi
if [[ -n "${GRIPPER_BINARY_THRESHOLD_M:-}" ]]; then
  cmd+=(--gripper-binary-threshold-m "${GRIPPER_BINARY_THRESHOLD_M}")
fi
if [[ "${DIRECT_OPENPI_IMAGES}" == "1" ]]; then
  cmd+=(
    --direct-openpi-images
    --direct-image-export-workers "${DIRECT_IMAGE_EXPORT_WORKERS:-4}"
    --direct-image-panorama-export-workers "${DIRECT_IMAGE_PANORAMA_EXPORT_WORKERS:-12}"
    --direct-image-panorama-bitrate "${DIRECT_IMAGE_PANORAMA_BITRATE:-67000000}"
    --direct-image-panorama-timeout-sec "${DIRECT_IMAGE_PANORAMA_TIMEOUT_SEC:-7200}"
    --direct-image-gpu-max-workers "${DIRECT_IMAGE_GPU_MAX_WORKERS:-0}"
    --direct-image-gpu-startup-workers "${DIRECT_IMAGE_GPU_STARTUP_WORKERS:-0}"
    --direct-image-gpu-min-workers "${DIRECT_IMAGE_GPU_MIN_WORKERS:-0}"
    --direct-image-gpu-startup-progress "${DIRECT_IMAGE_GPU_STARTUP_PROGRESS:-10}"
    --direct-image-gpu-oom-retries "${DIRECT_IMAGE_GPU_OOM_RETRIES:-6}"
    --direct-image-batch-frames "${DIRECT_IMAGE_BATCH_FRAMES:-512}"
    --direct-image-temp-margin-gb "${DIRECT_IMAGE_TEMP_MARGIN_GB:-0.5}"
    --direct-image-work-root "${DIRECT_IMAGE_WORK_ROOT:-/dev/shm/instaumi-direct-openpi}"
    --direct-image-panorama-width "${DIRECT_IMAGE_PANORAMA_WIDTH:-2048}"
    --direct-image-panorama-height "${DIRECT_IMAGE_PANORAMA_HEIGHT:-1024}"
    --direct-image-stitch-type "${DIRECT_IMAGE_STITCH_TYPE:-optflow}"
    --direct-image-timeout-sec "${DIRECT_IMAGE_TIMEOUT_SEC:-600}"
    --direct-image-decode-threads "${DIRECT_IMAGE_DECODE_THREADS:-1}"
    --direct-image-resize-workers "${DIRECT_IMAGE_RESIZE_WORKERS:-4}"
    --direct-image-v360-filter-threads "${DIRECT_IMAGE_V360_FILTER_THREADS:-2}"
    --direct-image-repeat-cache-frames "${DIRECT_IMAGE_REPEAT_CACHE_FRAMES:-8}"
    --direct-image-front-cache "${DIRECT_IMAGE_FRONT_CACHE:-auto}"
    --episode-shards "${EPISODE_SHARDS:-auto}"
    --episode-shard-workers "${EPISODE_SHARD_WORKERS:-0}"
    --ffprobe-bin "${FFPROBE_BIN:-ffprobe}"
  )
  if [[ "${PANORAMA_NUMERIC_ONLY}" == "1" ]]; then
    cmd+=(--panorama-numeric-only)
  fi
  if [[ "${DIRECT_IMAGE_PARALLEL_ARMS}" == "1" ]]; then
    cmd+=(--direct-image-parallel-arms)
  else
    cmd+=(--no-direct-image-parallel-arms)
  fi
  if [[ "${DIRECT_IMAGE_CUDA:-1}" == "1" ]]; then
    cmd+=(--direct-image-cuda)
  else
    cmd+=(--no-direct-image-cuda)
  fi
  if [[ "${DIRECT_IMAGE_FLOWSTATE:-0}" == "1" ]]; then
    cmd+=(--direct-image-flowstate)
  else
    cmd+=(--no-direct-image-flowstate)
  fi
  if [[ "${DIRECT_IMAGE_DIRECTION_LOCK:-0}" == "1" ]]; then
    cmd+=(--direct-image-direction-lock)
  else
    cmd+=(--no-direct-image-direction-lock)
  fi
  if [[ "${DIRECT_IMAGE_STITCH_FUSION:-0}" == "1" ]]; then
    cmd+=(--direct-image-stitch-fusion)
  else
    cmd+=(--no-direct-image-stitch-fusion)
  fi
  if [[ -n "${DIRECT_IMAGE_MEDIASDK_EXPORTER:-}" ]]; then
    cmd+=(--direct-image-mediasdk-exporter "${DIRECT_IMAGE_MEDIASDK_EXPORTER}")
  fi
  if [[ -n "${DIRECT_IMAGE_PANORAMA_EXPORTER:-}" ]]; then
    cmd+=(--direct-image-panorama-exporter "${DIRECT_IMAGE_PANORAMA_EXPORTER}")
  fi
  if [[ -n "${DIRECT_IMAGE_MODEL_DIR:-}" ]]; then
    cmd+=(--direct-image-model-dir "${DIRECT_IMAGE_MODEL_DIR}")
  fi
  if [[ -n "${DIRECT_IMAGE_PROJECTION_SIZE:-}" ]]; then
    cmd+=(--direct-image-projection-size "${DIRECT_IMAGE_PROJECTION_SIZE}")
  fi
fi
if [[ "${STREAMING_DATASET_WRITE:-1}" == "1" ]]; then
  cmd+=(--streaming-dataset-write)
else
  cmd+=(--no-streaming-dataset-write)
fi
if [[ "${EMBED_IMAGES_IN_MEMORY:-1}" == "1" ]]; then
  cmd+=(--embed-images-in-memory)
else
  cmd+=(--no-embed-images-in-memory)
fi
if [[ "${PERSISTENT_VIDEO_READERS:-1}" == "1" ]]; then
  cmd+=(--persistent-video-readers)
else
  cmd+=(--no-persistent-video-readers)
fi
if [[ "${VERIFY_FAST_CACHE_FULL_HASH:-0}" == "1" ]]; then
  cmd+=(--verify-fast-cache-full-hash)
else
  cmd+=(--no-verify-fast-cache-full-hash)
fi
case "${ALLOW_HOME_FAST_WORK_ROOT:-0}" in
  0) ;;
  1) cmd+=(--allow-home-fast-work-root) ;;
  *)
    echo "[error] ALLOW_HOME_FAST_WORK_ROOT must be 0 or 1" >&2
    exit 2
    ;;
esac
if [[ "${KEEP_FAST_WORK_OUTPUT:-0}" == "1" ]]; then
  cmd+=(--keep-fast-work-output)
fi
if [[ "${RESUME_CONVERSION:-1}" == "1" ]]; then
  cmd+=(--resume)
else
  cmd+=(--no-resume)
fi
if [[ "${VERIFY_RESUME_FULL_HASH:-0}" == "1" ]]; then
  cmd+=(--verify-resume-full-hash)
fi
if [[ -n "${RIGHT_FRAME_TIMESTAMPS}" ]]; then
  cmd+=(
    --right-frame-timestamps "${RIGHT_FRAME_TIMESTAMPS}"
    --left-frame-timestamps "${LEFT_FRAME_TIMESTAMPS}"
  )
fi
if [[ -f "${SYNC_CALIBRATION_REPORT}" ]]; then
  cmd+=(--sync-calibration-report "${SYNC_CALIBRATION_REPORT}")
fi
if [[ "${REQUIRE_CALIBRATED_TIMESTAMPS}" == "1" ]]; then
  cmd+=(--require-calibrated-timestamps)
fi
if [[ "${ALLOW_PARTIAL_INPUTS:-0}" == "1" ]]; then
  cmd+=(--allow-partial-inputs)
fi
if [[ "${OVERWRITE:-0}" == "1" ]]; then
  cmd+=(--overwrite)
fi
if [[ "${DISCARD_OVERWRITE_BACKUP:-0}" == "1" ]]; then
  cmd+=(--discard-overwrite-backup)
fi
if [[ "${DRY_RUN:-0}" == "1" ]]; then
  cmd+=(--dry-run)
fi

echo "[info] mode=bimanual_htc"
echo "[info] input_dir=${INPUT_DIR}"
echo "[info] tmp_root=${TMP_ROOT}"
echo "[info] session_tmp_root=${SESSION_TMP_ROOT}"
echo "[info] output_root=${OUTPUT_ROOT}"
echo "[info] task_prompt=${TASK_PROMPT}"
echo "[info] timestamp_log=${TIMESTAMP_LOG}"
echo "[info] segment_time_offset_sec=${SEGMENT_TIME_OFFSET_SEC:-0}"
echo "[info] sync_calibration_report=${SYNC_CALIBRATION_REPORT}"
echo "[info] require_calibrated_timestamps=${REQUIRE_CALIBRATED_TIMESTAMPS}"
echo "[info] allow_uncalibrated_timeline=${ALLOW_UNCALIBRATED_TIMELINE}"
echo "[info] right_frame_timestamps=${RIGHT_FRAME_TIMESTAMPS:-<legacy manifest>}"
echo "[info] left_frame_timestamps=${LEFT_FRAME_TIMESTAMPS:-<legacy manifest>}"
echo "[info] htc_time_source=${HTC_TIME_SOURCE:-auto}"
echo "[info] max_htc_nearest_gap_ms=${MAX_HTC_NEAREST_GAP_MS:-10}"
echo "[info] max_htc_bracket_gap_ms=${MAX_HTC_BRACKET_GAP_MS:-20}"
echo "[info] max_host_clock_offset_step_ms=${MAX_HOST_CLOCK_OFFSET_STEP_MS:-5}"
echo "[info] hard_gap_reject_ms=${HARD_GAP_REJECT_MS:-50}"
echo "[info] allow_partial_inputs=${ALLOW_PARTIAL_INPUTS:-0}"
echo "[info] gripper_zero_below_m=${GRIPPER_ZERO_BELOW_M:-<disabled>}"
echo "[info] allow_home_fast_work_root=${ALLOW_HOME_FAST_WORK_ROOT:-0}"
echo "[info] direct_openpi_images=${DIRECT_OPENPI_IMAGES}"
if [[ "${DIRECT_OPENPI_IMAGES}" == "1" ]]; then
  echo "[info] direct_image_export_workers=${DIRECT_IMAGE_EXPORT_WORKERS:-4}"
  echo "[info] direct_image_panorama_export_workers=${DIRECT_IMAGE_PANORAMA_EXPORT_WORKERS:-12}"
  echo "[info] direct_image_panorama_bitrate=${DIRECT_IMAGE_PANORAMA_BITRATE:-67000000}"
  echo "[info] direct_image_gpu_max_workers=${DIRECT_IMAGE_GPU_MAX_WORKERS:-0} startup_workers=${DIRECT_IMAGE_GPU_STARTUP_WORKERS:-0} min_workers=${DIRECT_IMAGE_GPU_MIN_WORKERS:-0}"
  echo "[info] direct_image_gpu_startup_progress=${DIRECT_IMAGE_GPU_STARTUP_PROGRESS:-10} oom_retries=${DIRECT_IMAGE_GPU_OOM_RETRIES:-6}"
  echo "[info] direct_image_parallel_arms=${DIRECT_IMAGE_PARALLEL_ARMS}"
  echo "[info] direct_image_batch_frames=${DIRECT_IMAGE_BATCH_FRAMES:-512}"
  echo "[info] episode_shards=${EPISODE_SHARDS:-auto} episode_shard_workers=${EPISODE_SHARD_WORKERS:-0}"
  echo "[info] direct_image_front_cache=${DIRECT_IMAGE_FRONT_CACHE:-auto} source=_03_native_decode_fanout"
  echo "[info] direct_image_work_root=${DIRECT_IMAGE_WORK_ROOT:-/dev/shm/instaumi-direct-openpi}"
  echo "[info] direct_image_panorama=${DIRECT_IMAGE_PANORAMA_WIDTH:-2048}x${DIRECT_IMAGE_PANORAMA_HEIGHT:-1024} stitch=${DIRECT_IMAGE_STITCH_TYPE:-optflow}"
  echo "[info] direct_image_pipeline=all_chapters_parallel_mediasdk_h265+active_frame_v360"
fi
printf '+'; printf ' %q' "${cmd[@]}"; printf '\n'
"${cmd[@]}"

}
main "$@"
