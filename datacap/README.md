# InstaUMI capture on Ubuntu

DataCap is a local browser interface for recording demonstrations with wrist-mounted cameras and VIVE Ultimate Trackers. Start these commands from the repository root.

## Try the interface

```sh
python3 -m datacap.webapp --dry-run --host 127.0.0.1 --port 8788
```

Open <http://127.0.0.1:8788/>. Choose a task and save folder, start capture, mark an episode, and end capture. This mode creates simulated tracker and event logs. Capture folders default to `~/InstaDataCap`.

## Prepare the capture computer

Install the system requirements and device-specific udev rules on Ubuntu 24.04:

```sh
sudo apt install python3-venv libhidapi-hidraw0 libhidapi-libusb0 ffmpeg
python3 -m venv datacap/.venv
source datacap/.venv/bin/activate
bash datacap/tools/install_hardware_access.sh
```

For real tracker capture, install a compatible local VIVE tracker backend in that environment and set `tracker.pyvut_root` in your camera profile to its checkout directory, the directory containing `pyvut/__init__.py`. Set `tracker.python` if the backend uses another Python environment. The backend must expose `pyvut.UltimateTrackerAPI` with `mode`, `wifi_info_path`, `debug`, `independent_slam` and `enable_pairing_on_start` constructor keywords; `start()`, `stop()` and `add_pose_callback()` methods; and `polling_alive` and `poll_error` properties. Tracker poses for offline synchronization must include the host `CLOCK_MONOTONIC_RAW` timestamp sampled at HID receipt. See [the recorder](tracker_recorder.py) for the pose fields and CSV schema. The public upstream [pyvut](https://github.com/nijkah/pyvut) describes the VIVE setup. DataCap requires the extended interface above.

Pair the trackers and make sure they have a usable map. Initial mapping may require VIVE Streaming Hub and SteamVR on Windows. If your backend needs Wi-Fi settings, create a private local copy of [the example](../examples/tracker-wifi.example.json):

```sh
mkdir -p ~/.config/instadatacap
install -m 600 examples/tracker-wifi.example.json ~/.config/instadatacap/wifi_info.json
```

Fill in your own settings and assign the physical trackers to `htc_right` and `htc_left` under `tracker.role_by_mac`. Use `DONGLE_USB` with an RF dongle or `TRACKER_USB` for direct USB as supported by the backend.

The optional pedal rule matches USB VID:PID `0483:5750`. With the pedal connected, three short taps within 1.2 seconds start capture, short taps mark episode boundaries, and a two-second hold ends capture.

## Insta360 X3 · Wired USB

Set each camera to Webcam mode. Build the Ubuntu UVC worker and make a camera profile:

```sh
sudo apt install g++ pkg-config libavcodec-dev libavutil-dev libswscale-dev
bash datacap/camera_usb/build_worker.sh
cp datacap/config.usb_webcam.example.json datacap/config.usb_webcam.json
ls -l /dev/v4l/by-path/*video-index0
```

Set `insta.usb_devices.insta_right` and `insta.usb_devices.insta_left` to stable `video-index0` nodes for two separate X3 cameras. `video-index1` is the metadata node. This profile requires the X3 native **Flat View**, a 1920 × 1080 MJPEG stream at 30 Hz with one fisheye above the other.

```sh
python -m datacap.webapp --config datacap/config.usb_webcam.json
```

1. Check **Wired USB**, choose the arm mode and task destination, then click **Start preview**. Confirm the previews and live tracker roles.
2. Click **Start capture** and mark each demonstration with **Start episode** and **End episode**. Frame exposure timestamps and tracker receive timestamps share the host clock.
3. Click **End capture**. Keep the session tree with `uvc_capture_complete.json`, both `uvc/insta_*/` packet streams and frame indexes, tracker CSVs, and the event log.

[Post-process the completed X3 capture](../dataprocess/README.md#inspect-an-x3-usb-recording). This path produces an aligned native MP4 for each camera. Webcam mode does not supply gyro data or FlowState.

## Insta360 X5 · Bluetooth and INSV

```sh
sudo apt install bluez python3-dbus python3-gi
cp datacap/config.example.json datacap/config.json
```

Set `insta.addresses` and `insta.role_by_address` so the cameras map to `insta_right` and `insta_left`. The X5 profile scans for names beginning with `X5 `.

```sh
python -m datacap.webapp --config datacap/config.json
```

1. Check **Bluetooth Control**, choose **Single arm · Right side** or **Dual arm · 2 Trackers**, and set the task destination. Click **Scan Bluetooth** and **Connect cameras**, then verify both roles and live poses.
2. Click **Start capture**. Rotate each rigid X5–tracker assembly around several axes with irregular motion for 5–10 seconds. Repeat every 10–15 minutes and just before ending capture so the offline clock calibration can align the signals.
3. Select **Episode capture** and mark each demonstration. Click **End capture** when finished.
4. Transfer every original INSV chapter and sidecar to the corresponding `insta/insta_right/` and `insta/insta_left/` directories from the session's transfer manifest. Keep the tracker CSVs and event log together with the media.

[Post-process a bimanual X5 session](../dataprocess/README.md#export-lerobot-v21).

## Build the Ubuntu package

On Ubuntu 24.04, install `rsync` along with the UVC build dependencies above:

```sh
bash datacap/tools/package_deb_ubuntu.sh
sudo apt install ./datacap/dist/InstaDataCap_0.3.9_amd64.deb
instadatacap-open
```

The launcher creates `~/.config/instadatacap/config.json` and opens the loopback interface. To start with X3 settings, copy the installed profile at `/usr/share/instadatacap/config.usb_webcam.example.json` to the user config before launching, then set the camera paths. Configure the tracker backend separately and restart the user service after editing, outside an active capture.

See [third-party notices](THIRD_PARTY_NOTICES.md).
