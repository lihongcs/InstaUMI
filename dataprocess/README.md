# InstaUMI data processing

The standard export takes a bimanual X5 + VIVE Ultimate Tracker session from
DataCap and writes LeRobot v2.1 episodes. A separate USB/UVC entry validates and
remuxes completed webcam sessions. Run the commands below from the root of the
extracted InstaUMI Ubuntu bundle.

## Set up the INSV exporter

Use Ubuntu x86-64 with Bash 4+, FFmpeg/ffprobe, a C++17 toolchain, CMake and an
NVIDIA GPU/driver stack compatible with Insta360 MediaSDK. The exporter uses
the official MediaSDK 3.1.1 API for optical-flow stitching. Obtain the SDK
from Insta360, then install it or point the build at the extracted SDK root.

```sh
python3 -m venv .venv-processing
source .venv-processing/bin/activate
python -m pip install -r dataprocess/requirements.txt
export PYTHON="$(command -v python)"

MEDIASDK_ROOT=/path/to/MediaSDK \
  bash dataprocess/_01_raw_extrect/insta360_pano_export/scripts/build_ubuntu.sh
python -c 'import av; print(av.filter.Filter("v360").name)'
```

The INSV path uses the `v360` filter in PyAV's FFmpeg libraries. If MediaSDK
requires model assets from its vendor bundle, set
`DIRECT_IMAGE_MODEL_DIR=/path/to/MediaSDK/models` in the processing
environment. Run `$PYTHON dataprocess/pipeline_preflight.py --profile ordinary`
to check the syntax of the pipeline entry points.

## Prepare a recording

1. Verify the left and right roles on the Linux capture machine before
   recording. Hold each X5 and its tracker as a rigid pair.
2. After total start, before total end and every 10–15 minutes, rotate each
   pair for 5–10 seconds around several axes using irregular motion. The
   clock calibration needs corresponding motion in the camera gyro and
   tracker stream, including in automatically split camera chapters.
3. Use DataCap segment start/end events to mark each demonstration. Finish
   the session, then copy all original INSV chapters into
   `insta/insta_right/` and `insta/insta_left/` as instructed by the
   generated `insta/transfer_manifest.json`. Keep the surrounding capture
   directory and logs together.

```text
capture/
  manifest.json
  timestamp_log.jsonl
  timestamp_audit_log.jsonl
  htc_right.csv
  htc_left.csv
  tracker_meta.json
  insta_ble_events.jsonl
  insta/
    transfer_manifest.json
    insta_right/*.insv
    insta_left/*.insv
```

Before copying media, the raw-session audit can report `waiting_for_insv`.
After transfer, add `--require-media` to require the original videos.

```sh
python dataprocess/inspect_capture_session.py /path/to/capture --require-media
```

## Export LeRobot v2.1

The two-arm export uses one pixel-distance-to-width calibration per arm.
The bundle includes a compatibility curve, `config/gripper_calibration.json`,
and a reference curve measured on the project's X5 rig,
`config/gripper_calibration_x5_reference.json`. For a new assembly, measure
the relationship between the ArUco marker center distance and the physical
gripper opening. Each calibration JSON holds ordered points with `px`
(center distance in pixels) and `m` (opening in metres). Set the two
environment variables below to use your own curves.

```sh
export LEFT_GRIPPER_CALIBRATION_JSON=/path/to/left.json
export RIGHT_GRIPPER_CALIBRATION_JSON=/path/to/right.json
bash dataprocess/run_bimanual_htc.sh /path/to/capture 'Pick up the block.'
```

The launcher checks the media, runs three stages and audits the exported
dataset:

| Stage | Work | Output |
| --- | --- | --- |
| `_01` | Index INSV chapters, recover frame timestamps and align camera gyro with tracker angular velocity | `capture/tmp/sync_calibration/calibration.json` and per-arm calibrated timelines |
| `_03` | Detect ArUco markers on each original front-fisheye stream and apply the gripper curves | `capture/tmp/{left,right}/03_gripper_estimation/gripper_width.csv` |
| `_05` | Pair camera frames and interpolate tracker pose and gripper width, split demonstrations and export episodes | `capture/lerobot/` and its embedded `sync_report.json` |

The default dataset aligns on the 60 Hz source timeline and exports at
30 Hz. Each sample contains six 224×224 RGB views, 20-element bimanual state
and action vectors, and source-alignment fields. Each action targets the
next retained state in the same episode. Inspect the accepted clock report
and run the exported dataset audit independently with:

```sh
python dataprocess/audit_lerobot_sync.py /path/to/capture/lerobot \
  --require-embedded-calibration
```

Keep adequate free storage for the panorama cache and the resulting dataset.
The launcher supports absolute `OUTPUT_ROOT`, per-arm calibration paths,
`PYTHON`, and `DIRECT_IMAGE_MODEL_DIR` through the environment. See the stage
scripts for options when inspecting or rerunning an individual stage.

## Inspect an X3 USB recording

For a completed DataCap X3 Wired USB capture, keep `uvc_capture_manifest.json`,
`uvc_capture_complete.json`, and the original `uvc/` packet and timestamp
files alongside the tracker and event logs. The stage selects its profile
from the recorded manifests. It validates MJPEG packets and the host clock
mapping, then writes the native 1920 × 1080, 30 Hz MP4 remux and timing
report under `capture/tmp/`:

- `{left,right}/01_raw_extract/videos/native_flat_master.mp4`
- `sync_calibration/{left,right}/calibrated_frame_timestamps.csv`
- `sync_calibration/calibration.json`
- `uvc_stage01_complete.json`

```sh
python3 dataprocess/pipeline_preflight.py --profile uvc
INPUT_DIR=/path/to/completed_usb_capture \
  bash dataprocess/_01_raw_extrect/run_01_raw_extract_biarm.sh
```

The USB MP4 is an inspection output. This bundle's LeRobot exporter uses
the bimanual INSV/HTC path above.
