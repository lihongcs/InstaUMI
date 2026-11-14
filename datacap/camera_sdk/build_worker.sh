#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATACAP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTAUMI_DIR="$(cd "${DATACAP_DIR}/.." && pwd)"
ARCHIVE="${1:-${INSTAUMI_DIR}/Linux_CameraSDK-2.1.1_MediaSDK-3.1.1/CameraSDK-2.1.1-Linux.tar.gz}"
SDK_PARENT="${DATACAP_DIR}/.sdk"
BIN_DIR="${DATACAP_DIR}/bin"

if [[ ! -f "${ARCHIVE}" ]]; then
  echo "CameraSDK archive not found: ${ARCHIVE}" >&2
  exit 1
fi

mkdir -p "${SDK_PARENT}" "${BIN_DIR}"
tar -xzf "${ARCHIVE}" -C "${SDK_PARENT}"
SDK_ROOT="$(find "${SDK_PARENT}" -mindepth 1 -maxdepth 1 -type d -name 'CameraSDK*-Linux' | head -n 1)"
if [[ -z "${SDK_ROOT}" ]]; then
  echo "Could not find extracted CameraSDK directory under ${SDK_PARENT}" >&2
  exit 1
fi

g++ "${SCRIPT_DIR}/insta_camera_worker.cc" \
  -std=c++17 -O2 -pthread \
  -I"${SDK_ROOT}/include" \
  -L"${SDK_ROOT}/lib" -lCameraSDK \
  -o "${BIN_DIR}/insta_camera_worker"

echo "Built: ${BIN_DIR}/insta_camera_worker"
echo "SDK lib: ${SDK_ROOT}/lib"
echo "Use both paths in datacap/config.json."
