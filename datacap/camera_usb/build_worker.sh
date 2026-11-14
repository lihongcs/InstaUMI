#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_file="${script_dir}/insta_uvc_worker.cpp"
output_dir="${script_dir}/bin"
output_file="${output_dir}/insta_uvc_worker"

mkdir -p "${output_dir}"
temporary_output="$(mktemp "${output_dir}/.insta_uvc_worker.build.XXXXXX")"
trap 'rm -f -- "${temporary_output}"' EXIT

multiarch="$(g++ -print-multiarch)"
system_pkgconfig_dirs=()
for candidate in \
  "/usr/lib/${multiarch}/pkgconfig" \
  "/usr/lib/pkgconfig" \
  "/usr/share/pkgconfig"; do
  if [[ -d "${candidate}" ]]; then
    system_pkgconfig_dirs+=("${candidate}")
  fi
done
if [[ ${#system_pkgconfig_dirs[@]} -eq 0 ]]; then
  echo "Cannot locate the system pkg-config directories" >&2
  exit 2
fi

# This worker is packaged for the host Ubuntu installation.  Do not allow a
# stale /usr/local *.pc/header installation to be combined with the system
# shared libraries: AVCodecContext and AVFrame are not ABI-compatible across
# FFmpeg major versions.  PKG_CONFIG_LIBDIR replaces pkg-config's built-in
# search path, while the empty PKG_CONFIG_PATH prevents caller contamination.
system_pkgconfig_libdir="$(IFS=:; echo "${system_pkgconfig_dirs[*]}")"
export PKG_CONFIG_PATH=
export PKG_CONFIG_LIBDIR="${system_pkgconfig_libdir}"

if ! pkg-config --exists libavcodec libavutil libswscale; then
  echo "Missing FFmpeg development packages: libavcodec, libavutil, libswscale" >&2
  exit 2
fi

echo "Building with system FFmpeg: libavcodec=$(pkg-config --modversion libavcodec) libavutil=$(pkg-config --modversion libavutil) libswscale=$(pkg-config --modversion libswscale)"

g++ \
  -std=c++17 \
  -O2 \
  -Wall \
  -Wextra \
  -Wpedantic \
  -pthread \
  -isystem "/usr/include/${multiarch}" \
  "${source_file}" \
  -o "${temporary_output}" \
  $(pkg-config --cflags --libs libavcodec libavutil libswscale)

chmod 0755 "${temporary_output}"
"${temporary_output}" --ffmpeg-abi-check >/dev/null
"${temporary_output}" --offline-startup-timing-self-test >/dev/null
"${temporary_output}" --offline-profile-self-test >/dev/null
"${temporary_output}" --help >/dev/null
mv -fT -- "${temporary_output}" "${output_file}"
trap - EXIT

echo "Built ${output_file}"
