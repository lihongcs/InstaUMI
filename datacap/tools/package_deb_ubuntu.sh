#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DATACAP_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
VERSION="${VERSION:-0.3.9}"
ARCH="${ARCH:-$(dpkg --print-architecture)}"
PACKAGE_NAME="instadatacap"
APP_DIR="/opt/InstaDataCap"
BUILD_ROOT="$DATACAP_DIR/build/deb"
PACKAGE_ROOT="$BUILD_ROOT/${PACKAGE_NAME}_${VERSION}_${ARCH}"
OUTPUT_DIR="$DATACAP_DIR/dist"
OUTPUT_DEB="$OUTPUT_DIR/InstaDataCap_${VERSION}_${ARCH}.deb"

for command in dpkg-deb rsync python3 g++ pkg-config; do
  command -v "$command" >/dev/null 2>&1 || {
    printf 'Required command not found: %s\n' "$command" >&2
    exit 1
  }
done

# Compiles only; the UVC worker does not enumerate or open a camera until the
# installed application receives an explicit preview/capture action.
bash "$DATACAP_DIR/camera_usb/build_worker.sh"

rm -rf -- "$PACKAGE_ROOT"
mkdir -p \
  "$PACKAGE_ROOT$APP_DIR/datacap" \
  "$PACKAGE_ROOT/DEBIAN" \
  "$PACKAGE_ROOT/usr/bin" \
  "$PACKAGE_ROOT/usr/lib/systemd/user" \
  "$PACKAGE_ROOT/usr/lib/udev/rules.d" \
  "$PACKAGE_ROOT/usr/share/applications" \
  "$PACKAGE_ROOT/usr/share/icons/hicolor/scalable/apps" \
  "$PACKAGE_ROOT/usr/share/instadatacap" \
  "$OUTPUT_DIR"

for runtime_file in \
  __init__.py \
  gripper_binary_preview.py \
  gripper_episode_estimation.py \
  htc_pose_transform.py \
  insta_adapter.py \
  mvs_adapter.py \
  insta_ble_direct_worker.py \
  offline_annotation.py \
  pedal.py \
  session.py \
  tracker_adapter.py \
  tracker_recorder.py \
  webapp.py; do
  install -m 0644 "$DATACAP_DIR/$runtime_file" "$PACKAGE_ROOT$APP_DIR/datacap/$runtime_file"
done
rsync -a "$DATACAP_DIR/static/" "$PACKAGE_ROOT$APP_DIR/datacap/static/"
install -D -m 0644 "$DATACAP_DIR/THIRD_PARTY_NOTICES.md" \
  "$PACKAGE_ROOT/usr/share/doc/$PACKAGE_NAME/THIRD_PARTY_NOTICES.md"
install -m 0644 "$DATACAP_DIR/../LICENSE" "$PACKAGE_ROOT/usr/share/doc/$PACKAGE_NAME/LICENSE"
install -m 0644 "$DATACAP_DIR/../NOTICE" "$PACKAGE_ROOT/usr/share/doc/$PACKAGE_NAME/NOTICE"
install -D -m 0755 "$DATACAP_DIR/camera_usb/bin/insta_uvc_worker" \
  "$PACKAGE_ROOT$APP_DIR/datacap/camera_usb/bin/insta_uvc_worker"
install -D -m 0755 "$DATACAP_DIR/camera_mvs/mvs_worker.py" \
  "$PACKAGE_ROOT$APP_DIR/datacap/camera_mvs/mvs_worker.py"
python3 - "$DATACAP_DIR/config.usb_webcam.example.json" \
  "$PACKAGE_ROOT/usr/share/instadatacap/config.usb_webcam.example.json" <<'PY'
import json
import sys
from pathlib import Path

source, destination = map(Path, sys.argv[1:3])
config = json.loads(source.read_text(encoding="utf-8"))
config["output_root"] = "~/InstaDataCap"
config["insta"]["usb_webcam_worker"] = (
    "/opt/InstaDataCap/datacap/camera_usb/bin/insta_uvc_worker"
)
config["tracker"].update({
    "python": "/usr/bin/python3",
    "worker": "/opt/InstaDataCap/datacap/tracker_recorder.py",
    "role_config_path": "~/.config/htc/roles.json",
})
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(
    json.dumps(config, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
)
PY
CONFIG_SOURCE="$DATACAP_DIR/config.example.json"
python3 - "$CONFIG_SOURCE" "$PACKAGE_ROOT/usr/share/instadatacap/config.template.json" <<'PY'
import json
import sys
from pathlib import Path

source, destination = map(Path, sys.argv[1:3])
config = json.loads(source.read_text(encoding="utf-8"))
config["task_name"] = "Untitled task"
config["output_root"] = "@DATA_DIR@"
config.setdefault("web", {}).update({"host": "127.0.0.1", "port": 8788})
config.setdefault("insta", {}).update({
    "connect_on_start": False,
    "python": "/usr/bin/python3",
    "worker": "/opt/InstaDataCap/datacap/insta_ble_direct_worker.py",
    "camera_sdk_worker": "/opt/InstaDataCap/datacap/bin/insta_camera_worker",
    "sdk_lib_dir": "/opt/InstaDataCap/datacap/.sdk/CAMERA_SDK/lib",
    "usb_webcam_worker": "/opt/InstaDataCap/datacap/camera_usb/bin/insta_uvc_worker",
    "mvs_worker": "/opt/InstaDataCap/datacap/camera_mvs/mvs_worker.py",
})
config.setdefault("tracker", {}).update({
    "python": "/usr/bin/python3",
    "worker": "/opt/InstaDataCap/datacap/tracker_recorder.py",
})
destination.write_text(json.dumps(config, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
PY

install -m 0644 "$DATACAP_DIR/assets/instadatacap.svg" \
  "$PACKAGE_ROOT/usr/share/icons/hicolor/scalable/apps/instadatacap.svg"
install -m 0644 "$DATACAP_DIR/assets/firefox-user.js" \
  "$PACKAGE_ROOT/usr/share/instadatacap/firefox-user.js"
install -m 0644 "$DATACAP_DIR/udev/99-instaumi-datacap.rules" \
  "$PACKAGE_ROOT/usr/lib/udev/rules.d/99-instadatacap.rules"

cat >"$PACKAGE_ROOT/usr/bin/instadatacap" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/InstaDataCap"
HOST="${INSTADATACAP_HOST:-127.0.0.1}"
PORT="${INSTADATACAP_PORT:-8788}"
STATE_DIR="${INSTADATACAP_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/instadatacap}"
DATA_DIR="${INSTADATACAP_DATA_DIR:-$HOME/InstaDataCap}"
CONFIG_FILE="${INSTADATACAP_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/instadatacap/config.json}"

mkdir -p -- "$STATE_DIR" "$DATA_DIR" "$(dirname -- "$CONFIG_FILE")"
if [[ ! -f "$CONFIG_FILE" ]]; then
  install -m 0600 /usr/share/instadatacap/config.template.json "$CONFIG_FILE"
  sed -i "s|@DATA_DIR@|$DATA_DIR|g" "$CONFIG_FILE"
fi

export INSTADATACAP_STATE_DIR="$STATE_DIR"
export INSTADATACAP_DATA_DIR="$DATA_DIR"
export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"
cd -- "$APP_DIR"
exec /usr/bin/python3 -m datacap.webapp \
  --config "$CONFIG_FILE" \
  --host "$HOST" \
  --port "$PORT"
EOF

cat >"$PACKAGE_ROOT/usr/bin/instadatacap-open" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

HOST="${INSTADATACAP_HOST:-127.0.0.1}"
PORT="${INSTADATACAP_PORT:-8788}"
URL="http://${HOST}:${PORT}/"
STATE_DIR="${INSTADATACAP_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/instadatacap}"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
LIFETIME_MARKER="$RUNTIME_DIR/instadatacap-launch-active"
FIREFOX_PROFILE=""
FIREFOX_PID=""

cleanup_app() {
  status=$?
  trap - EXIT HUP INT TERM
  if [[ -n "$FIREFOX_PID" ]] && kill -0 "$FIREFOX_PID" >/dev/null 2>&1; then
    kill "$FIREFOX_PID" >/dev/null 2>&1 || true
    wait "$FIREFOX_PID" 2>/dev/null || true
  fi
  # SIGINT lets the service safely stop an active capture before all camera,
  # Tracker, and pedal workers are released.
  systemctl --user stop instadatacap.service >/dev/null 2>&1 || true
  rm -f -- "$LIFETIME_MARKER"
  if [[ -n "$FIREFOX_PROFILE" && -d "$FIREFOX_PROFILE" ]]; then
    rm -rf -- "$FIREFOX_PROFILE"
  fi
  exit "$status"
}

# The dedicated Firefox process owns the whole application lifetime. Closing
# its final window stops the service and releases every device worker.
trap cleanup_app EXIT HUP INT TERM

install -m 0600 /dev/null "$LIFETIME_MARKER"
systemctl --user daemon-reload
systemctl --user start instadatacap.service
for _ in $(seq 1 60); do
  if curl --silent --fail --max-time 1 "${URL}api/status" >/dev/null; then
    if command -v firefox >/dev/null 2>&1; then
      # Ubuntu's Firefox is commonly a strictly confined Snap. Such builds
      # cannot use arbitrary hidden paths such as ~/.local/state as profiles.
      if [[ -x /snap/bin/firefox ]]; then
        FIREFOX_PROFILE_BASE="$HOME/snap/firefox/common/instadatacap-profiles"
      else
        FIREFOX_PROFILE_BASE="$STATE_DIR/firefox-profiles"
      fi
      mkdir -p -- "$FIREFOX_PROFILE_BASE"
      FIREFOX_PROFILE="$(mktemp -d "$FIREFOX_PROFILE_BASE/run.XXXXXX")"
      install -m 0600 /usr/share/instadatacap/firefox-user.js "$FIREFOX_PROFILE/user.js"
      firefox --no-remote --profile "$FIREFOX_PROFILE" --new-window "$URL" &
      FIREFOX_PID="$!"
      # Closing the dedicated window stops the service. Conversely, the
      # page's Exit App action stops the service, so close only the Firefox
      # process started by this launcher when the service itself exits. A
      # hardware action may briefly delay /api/status while remaining healthy;
      # one curl timeout must never be interpreted as an application exit.
      while kill -0 "$FIREFOX_PID" >/dev/null 2>&1; do
        if ! curl --silent --fail --max-time 1 "${URL}api/status" >/dev/null; then
          if ! systemctl --user is-active --quiet instadatacap.service; then
            kill "$FIREFOX_PID" >/dev/null 2>&1 || true
            break
          fi
        fi
        sleep 0.25
      done
      wait "$FIREFOX_PID" 2>/dev/null || true
      FIREFOX_PID=""
      exit 0
    fi
    printf 'InstaDataCap requires Firefox so closing its window can safely stop all services.\n' >&2
    command -v notify-send >/dev/null 2>&1 && \
      notify-send "InstaDataCap" "Firefox is required for managed application lifetime."
    exit 1
  fi
  sleep 0.25
done

printf 'InstaDataCap did not become ready. Run: journalctl --user -u instadatacap -n 100\n' >&2
command -v notify-send >/dev/null 2>&1 && \
  notify-send "InstaDataCap" "Service failed to start. Check the user service log."
exit 1
EOF

cat >"$PACKAGE_ROOT/usr/lib/systemd/user/instadatacap.service" <<'EOF'
[Unit]
Description=InstaDataCap local capture service
After=graphical-session.target
ConditionPathExists=%t/instadatacap-launch-active
StartLimitIntervalSec=30
StartLimitBurst=10

[Service]
Type=simple
ExecStart=/usr/bin/instadatacap
Restart=on-failure
RestartSec=2
# USB STOP may need to drain and fsync two long MJPEG recordings. Keep this
# longer than usb_stop_timeout_sec so systemd never turns a clean Exit App into
# a forced kill while capture artifacts are being committed.
TimeoutStopSec=180
KillSignal=SIGINT
Environment=PYTHONUNBUFFERED=1
UMask=0022

EOF

cat >"$PACKAGE_ROOT/usr/share/applications/instadatacap.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=InstaDataCap
Comment=Insta360 and HTC tracker data capture
Exec=instadatacap-open
Icon=instadatacap
Terminal=false
Categories=Utility;Development;
StartupNotify=true
EOF

cat >"$PACKAGE_ROOT/DEBIAN/control" <<EOF
Package: $PACKAGE_NAME
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Maintainer: InstaDataCap Team <support@localhost>
Depends: python3 (>= 3.10), python3-dbus, python3-gi, python3-hid, python3-numpy, bluez, curl, ffmpeg, xdg-utils, libhidapi-hidraw0, libhidapi-libusb0, libavcodec60, libavutil58, libswscale7
Description: Local Insta360 and HTC tracker capture application
 InstaDataCap packages the existing low-latency web interface as a native
 Ubuntu desktop entry backed by a persistent per-user service.
EOF

cat >"$PACKAGE_ROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
udevadm control --reload-rules >/dev/null 2>&1 || true
udevadm trigger --subsystem-match=input >/dev/null 2>&1 || true
udevadm trigger --subsystem-match=hidraw >/dev/null 2>&1 || true
udevadm trigger --subsystem-match=usb >/dev/null 2>&1 || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true

# Register the user unit without enabling login-time auto-start. The desktop
# launcher starts it and owns its lifetime; package upgrades never interrupt
# an active recording.
if [ "$1" = "configure" ] || [ "$1" = "abort-upgrade" ] || \
   [ "$1" = "abort-deconfigure" ] || [ "$1" = "abort-remove" ]; then
  if [ -z "${DPKG_ROOT:-}" ] && command -v deb-systemd-helper >/dev/null 2>&1; then
    deb-systemd-helper --user unmask instadatacap.service >/dev/null || true
    deb-systemd-helper --user update-state instadatacap.service >/dev/null || true
  fi
fi
exit 0
EOF

cat >"$PACKAGE_ROOT/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
  udevadm control --reload-rules >/dev/null 2>&1 || true
fi
if [ "$1" = "purge" ] && [ -z "${DPKG_ROOT:-}" ] && \
   command -v deb-systemd-helper >/dev/null 2>&1; then
  deb-systemd-helper --user purge instadatacap.service >/dev/null || true
fi
exit 0
EOF

find "$PACKAGE_ROOT" -type d -exec chmod 0755 {} +
find "$PACKAGE_ROOT" -type f -exec chmod 0644 {} +
find "$PACKAGE_ROOT$APP_DIR" -type f -name '*.sh' -exec chmod 0755 {} +
if [[ -f "$PACKAGE_ROOT$APP_DIR/datacap/bin/insta_camera_worker" ]]; then
  chmod 0755 "$PACKAGE_ROOT$APP_DIR/datacap/bin/insta_camera_worker"
fi
chmod 0755 "$PACKAGE_ROOT$APP_DIR/datacap/camera_usb/bin/insta_uvc_worker"
chmod 0755 \
  "$PACKAGE_ROOT/usr/bin/instadatacap" \
  "$PACKAGE_ROOT/usr/bin/instadatacap-open" \
  "$PACKAGE_ROOT/DEBIAN/postinst" \
  "$PACKAGE_ROOT/DEBIAN/postrm"

python3 - "$PACKAGE_ROOT$APP_DIR" <<'PY'
import ast
import sys
from pathlib import Path

root = Path(sys.argv[1])
for path in root.rglob("*.py"):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
PY
dpkg-deb --root-owner-group --build "$PACKAGE_ROOT" "$OUTPUT_DEB"
printf 'Built %s\n' "$OUTPUT_DEB"
