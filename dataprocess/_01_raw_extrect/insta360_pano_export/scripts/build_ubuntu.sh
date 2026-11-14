#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUILD_DIR="${BUILD_DIR:-${PROJECT_DIR}/build}"
MEDIASDK_ROOT="${MEDIASDK_ROOT:-}"

if [[ -z "${MEDIASDK_ROOT}" ]]; then
  echo "[info] MEDIASDK_ROOT is empty; CMake will search /usr/include and /usr/lib."
  echo "       If you extracted the SDK manually, rerun with:"
  echo "       MEDIASDK_ROOT=/path/to/MediaSDK ${BASH_SOURCE[0]}"
fi

mkdir -p "${BUILD_DIR}"

cmake_args=(
  -S "${PROJECT_DIR}"
  -B "${BUILD_DIR}"
  -DCMAKE_BUILD_TYPE=Release
)
if [[ -n "${MEDIASDK_ROOT}" ]]; then
  cmake_args+=(-DINSTA360_MEDIASDK_ROOT="${MEDIASDK_ROOT}")
fi

echo "[build] configure"
printf '+'
printf ' %q' cmake "${cmake_args[@]}"
printf '\n'
cmake "${cmake_args[@]}"

echo "[build] compile"
cmake --build "${BUILD_DIR}" -j"$(nproc)"

echo "[done] ${BUILD_DIR}/insv_to_pano"
