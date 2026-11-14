#!/usr/bin/env bash
set -euo pipefail

# Offline-only stage-01 entry for a completed DataCap USB/UVC session.  This
# script never enumerates or opens /dev/video*; the completion marker and
# recorded packet files are its only camera inputs.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INPUT_DIR="${INPUT_DIR:-${1:-}}"
if [[ -z "${INPUT_DIR}" || ! -d "${INPUT_DIR}" ]]; then
  echo "Usage: INPUT_DIR=/path/to/completed_uvc_session bash $(basename "$0")" >&2
  exit 2
fi
if [[ ! -f "${INPUT_DIR}/uvc_capture_complete.json" ]]; then
  echo "[error] missing completed UVC marker: ${INPUT_DIR}/uvc_capture_complete.json" >&2
  exit 1
fi

if [[ -z "${PYTHON:-}" ]]; then
  if [[ -n "${CONDA_PREFIX:-}" && -x "${CONDA_PREFIX}/bin/python" ]]; then
    PYTHON="${CONDA_PREFIX}/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
  else
    echo "[error] no Python 3 executable found" >&2
    exit 1
  fi
fi

cmd=(
  "${PYTHON}"
  "${SCRIPT_DIR}/extract_uvc_capture.py"
  --session-dir "${INPUT_DIR}"
  --tmp-root "${TMP_ROOT:-${INPUT_DIR}/tmp}"
  --ffmpeg-bin "${FFMPEG_BIN:-/usr/bin/ffmpeg}"
  --ffprobe-bin "${FFPROBE_BIN:-/usr/bin/ffprobe}"
  --ffmpeg-threads "${FFMPEG_THREADS:-2}"
  --max-raw-clock-error-ns "${UVC_MAX_RAW_CLOCK_ERROR_NS:-1000000}"
)
for side in right left; do
  upper="${side^^}"
  for axis in yaw pitch roll; do
    axis_upper="${axis^^}"
    variable="UVC_${upper}_${axis_upper}_DEG"
    if [[ -n "${!variable:-}" ]]; then
      cmd+=("--${side}-${axis}-deg" "${!variable}")
    fi
  done
done

echo "[info] source_family=usb_uvc input_dir=${INPUT_DIR}"
echo "[info] native_source=recorded MJPEG; profile is validated from the capture manifests"
echo "[info] clock=V4L2_SOE_to_MONOTONIC_RAW no_gyro=1 synthetic_60fps=0"
printf '+'; printf ' %q' "${cmd[@]}"; printf '\n'
exec "${cmd[@]}"
