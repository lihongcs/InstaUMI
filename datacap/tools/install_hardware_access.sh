#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATACAP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
RULE_SOURCE="${DATACAP_DIR}/udev/99-instaumi-datacap.rules"
RULE_TARGET="/etc/udev/rules.d/99-instaumi-datacap.rules"

sudo install -m 0644 "${RULE_SOURCE}" "${RULE_TARGET}"
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=input --action=change
sudo udevadm trigger --subsystem-match=hidraw --action=change
sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=0bb4 --attr-match=idProduct=0350 --action=change

echo "Installed ${RULE_TARGET}"
echo "Reconnect the pedal/HTC dongle if their current device permissions did not change."
