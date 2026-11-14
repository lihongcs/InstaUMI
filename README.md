# InstaUMI

InstaUMI is the capture and processing software for [GenUMI](https://lihongcs.github.io/genumi/). Choose a camera profile on Ubuntu, record demonstrations with DataCap, and process the saved session offline.

| Camera | Capture profile | Processing output |
| --- | --- | --- |
| Insta360 X3 | Wired USB Webcam. DataCap previews and records the native 1920 × 1080, 30 Hz dual-fisheye MJPEG stream on the Ubuntu host. | Validated packet stream, host-clock alignment with the trackers and a native MP4 remux for inspection. |
| Insta360 X5 | Bluetooth Control. DataCap marks episodes and controls continuous recording to the cameras. Transfer original INSV chapters after capture. | Camera–tracker clock calibration, gripper-width estimation and bimanual LeRobot v2.1 export. |

The X3 Webcam path ends at stage 01. Its image layout and clock contract require their own gripper calibration and downstream export validation. The documented LeRobot path uses X5 INSV input.

## Quick start

On a computer with Python 3, preview the DataCap interface and record a simulated session:

```sh
python3 -m datacap.webapp --dry-run --host 127.0.0.1 --port 8788
```

Open <http://127.0.0.1:8788/>. The simulation writes tracker and episode logs.

- [Ubuntu capture guide](datacap/README.md) covers hardware setup and both camera profiles.
- [Post-processing guide](dataprocess/README.md) covers the X3 inspection workflow and the X5 dataset pipeline.
- [Measured X5 benchmark](https://lihongcs.github.io/genumi/guide.html#processing) shows the native INSV input, per-stage timings, resource use, and LeRobot output.

Hardware tracker capture requires a compatible local VIVE backend. The open-source recorder and adapter specify its interface in the Ubuntu guide. The repository ships launch examples, reference calibration curves, and code only.

## License

First-party code is [Apache-2.0](LICENSE). See the [notices](NOTICE) for third-party attribution.
