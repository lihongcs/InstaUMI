# GenUMI iOS capture utility

This optional Xcode project controls one or more Insta360 X5 cameras, downloads media, extracts timestamp and gyro sidecars, and can export panoramas. Ubuntu DataCap is the capture controller for the documented GenUMI Tracker workflow.

## Build from source

1. Obtain compatible iOS copies of `INSCameraSDK.xcframework`, `INSCameraServiceSDK.xcframework`, `INSCoreMedia.xcframework`, and `SSZipArchive.xcframework` from their providers. The project expects these directories directly inside `instaumi/Frameworks/`.
2. Open `instaumi/instaumi.xcodeproj` in Xcode, select your development team under **Signing & Capabilities**, then choose a bundle identifier you can sign.
3. Build and run on a physical iPhone or iPad. Enable the requested Bluetooth and local network permissions when connecting to cameras.

## Record and download

For one X5, tap **Scan BLE**, select the camera, then tap **Start Rec** and **Stop Rec**. For multiple X5s, use **Connect X5s**, then **Sync Multi** before capture. Sync Multi exchanges clock information over the camera command path. For final timing calibration, retain both the media sidecars and the command logs.

Enable camera Wi-Fi if necessary. Connect the phone to the camera, choose **Wi-Fi Mode**, and tap **Download Latest**. Repeat for each camera. Use **Parse Sidecars** to extract timestamp and gyro information and **Export Pano** to write a panorama when needed.

The app saves media under `Documents/instaumi/downloads/`, sidecars under `Documents/instaumi/sidecars/`, panorama outputs under `Documents/instaumi/panoramas/`, and command/event logs under `sessions/` and `timestamp_logs/` in the same app directory.
