#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
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

DEB_PATH="${DEB_PATH:-${SDK_BUNDLE_DIR}/libMediaSDK-dev-3.1.1.0-20250922_191110-amd64/libMediaSDK-dev-3.1.1.0-20250922_191110-amd64.deb}"

if [[ ! -f "${DEB_PATH}" ]]; then
  echo "[error] missing MediaSDK deb: ${DEB_PATH}" >&2
  echo "        Set DEB_PATH=/path/to/libMediaSDK-dev-*.deb" >&2
  exit 1
fi

echo "[install] apt prerequisites"
sudo apt-get update
sudo apt-get install -y \
  build-essential cmake pkg-config ffmpeg python3 \
  libtiff5 libegl1 libxext6 libvulkan1 freeglut3-dev vulkan-tools libdc1394-25

echo "[install] MediaSDK deb=${DEB_PATH}"
sudo apt-get install -y "${DEB_PATH}"

echo "[check] MediaSDK files"
test -f /usr/include/ins_stitcher.h
test -f /usr/lib/libMediaSDK.so
echo "[done] MediaSDK installed"
