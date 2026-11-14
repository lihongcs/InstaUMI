import Foundation
import INSCameraServiceSDK

enum MultiCameraTransport: String, Codable {
    case bluetooth
    case wifiSocket
    case usb
    case unknown
}

enum MultiCameraConnectionState: String, Codable, Equatable {
    case discovered
    case connecting
    case connected
    case failed
    case disconnected
}

enum MultiCameraCaptureMode: Equatable {
    case noCamera
    case singleX5
    case multiX5

    var displayName: String {
        switch self {
        case .noCamera:
            return "未连接 X5"
        case .singleX5:
            return "单台 X5"
        case .multiX5:
            return "多台 X5"
        }
    }

    var requiresClockSync: Bool {
        return self == .multiX5
    }
}

struct MultiCameraClockSample: Codable {
    let unixTimeMs: Int64
    let monotonicTime: TimeInterval
    let label: String
}

struct MultiCameraCaptureEvent: Codable {
    let eventID: String
    let cameraID: String
    let cameraName: String
    let captureMode: String
    let connectedCameraCount: Int
    let requiresClockSync: Bool
    let command: String
    let stage: String
    let localUnixTimeMs: Int64
    let monotonicTime: TimeInterval
    let cameraURI: String?
    let errorDescription: String?
}

struct MultiCameraDownloadedResource: Codable {
    let cameraID: String
    let cameraName: String
    let kind: String
    let remoteURI: String
    let localPath: String
    let downloadedAtUnixTimeMs: Int64
}

struct MultiCameraRemoteResource: Identifiable, Hashable {
    let id: String
    let kind: String
    let uri: String
    let fileName: String
    let fileSizeBytes: UInt64?
    let durationSeconds: UInt32?
    let isDownloaded: Bool

    var kindText: String {
        kind == "video" ? "视频" : "照片"
    }

    var fileSizeText: String {
        guard let fileSizeBytes = fileSizeBytes, fileSizeBytes > 0 else {
            return "--"
        }
        return ByteCountFormatter.string(fromByteCount: Int64(fileSizeBytes), countStyle: .file)
    }

    var durationText: String {
        guard let durationSeconds = durationSeconds, durationSeconds > 0 else {
            return "--"
        }
        let minutes = Int(durationSeconds) / 60
        let seconds = Int(durationSeconds) % 60
        return String(format: "%02d:%02d", minutes, seconds)
    }
}


struct MultiCameraGyroSampleSummary: Codable {
    let timestampMs: Int64
    let gravityX: Double
    let gravityY: Double
    let gravityZ: Double
    let rotationX: Double
    let rotationY: Double
    let rotationZ: Double
}

struct MultiCameraGyroSummary: Codable {
    let count: Int
    let timeOffsetMs: Double?
    let firstSample: MultiCameraGyroSampleSummary?
    let lastSample: MultiCameraGyroSampleSummary?
    let rawGyroPath: String?
    let rawSecondaryGyroPath: String?
    let imageAx: Double?
    let imageAy: Double?
    let imageAz: Double?
    let imageGx: Double?
    let imageGy: Double?
    let imageGz: Double?
}

struct MultiCameraMediaSidecar: Codable {
    let mediaPath: String
    let mediaKind: String
    let cameraType: String?
    let serialNumber: String?
    let firmwareVersion: String?
    let createUnixTimeMs: Int64?
    let firstFrameTimestampMs: Double?
    let firstGpsTimestampMs: Double?
    let gyroTimestampAdjustMs: Double?
    let rollingShutterTimeMs: Double?
    let fileTotalTimeMs: UInt32?
    let fileSize: UInt64?
    let width: Double?
    let height: Double?
    let gyro: MultiCameraGyroSummary?
}

struct MultiCameraInspectionItem: Identifiable {
    let id: String
    let mediaURL: URL
    let sidecarURL: URL?
    let panoramaURL: URL?
    let gyroURL: URL?
    let secondaryGyroURL: URL?
    let sidecar: MultiCameraMediaSidecar?
    let fileSizeBytes: UInt64
    let modifiedAt: Date?

    var fileName: String {
        mediaURL.lastPathComponent
    }

    var baseName: String {
        mediaURL.deletingPathExtension().lastPathComponent
    }

    var mediaKindText: String {
        switch mediaURL.pathExtension.lowercased() {
        case "insv", "mp4", "mov", "lrv":
            return "视频"
        case "insp", "jpg", "jpeg", "dng":
            return "照片"
        default:
            return "媒体"
        }
    }

    var durationText: String {
        guard let totalMs = sidecar?.fileTotalTimeMs else {
            return "--"
        }
        let totalSeconds = Int((Double(totalMs) / 1000.0).rounded())
        let minutes = totalSeconds / 60
        let seconds = totalSeconds % 60
        return String(format: "%02d:%02d", minutes, seconds)
    }

    var resolutionText: String {
        guard let width = sidecar?.width, let height = sidecar?.height else {
            return "--"
        }
        return "\(Int(width)) x \(Int(height))"
    }

    var fileSizeText: String {
        ByteCountFormatter.string(fromByteCount: Int64(fileSizeBytes), countStyle: .file)
    }

    var gyroCount: Int {
        sidecar?.gyro?.count ?? 0
    }

    var hasSidecar: Bool {
        sidecarURL != nil && sidecar != nil
    }

    var hasPanorama: Bool {
        panoramaURL != nil
    }

    var hasGyro: Bool {
        gyroCount > 0 || gyroURL != nil || secondaryGyroURL != nil
    }

    var relatedURLs: [URL] {
        var seen = Set<String>()
        return [mediaURL, sidecarURL, panoramaURL, gyroURL, secondaryGyroURL]
            .compactMap { $0 }
            .filter { seen.insert($0.path).inserted }
    }
}

final class MultiCameraDeviceRecord {
    let id: String
    var name: String
    var rssi: Int
    var transport: MultiCameraTransport
    var state: MultiCameraConnectionState
    var lastError: String?
    var lastPhotoURI: String?
    var lastVideoURI: String?
    var bluetoothDevice: INSBluetoothDevice?
    var commandManager: INSCameraBasicCommands?

    // Parameters for display
    var serialNumber: String?
    var batteryLevel: Int?
    var resolutionStr: String?
    var fps: Int?
    var lensModeStr: String?

    init(device: INSBluetoothDevice, rssi: Int) {
        self.id = device.identifierUUIDStringSafe
        self.name = device.name.isEmpty ? "影石相机" : device.name
        self.rssi = rssi
        self.transport = .bluetooth
        self.state = .discovered
        self.bluetoothDevice = device
    }

    var displayState: String {
        switch state {
        case .discovered:
            return "已发现"
        case .connecting:
            return "连接中"
        case .connected:
            return "已连接"
        case .failed:
            return "连接失败"
        case .disconnected:
            return "已断开"
        }
    }

    var summaryLine: String {
        var parts = ["\(name)", "\(displayState)", "信号 \(rssi)"]
        if let lastPhotoURI = lastPhotoURI {
            parts.append("照片 \(lastPhotoURI)")
        }
        if let lastVideoURI = lastVideoURI {
            parts.append("视频 \(lastVideoURI)")
        }
        if let lastError = lastError {
            parts.append("错误 \(lastError)")
        }
        return parts.joined(separator: " | ")
    }
}

enum MultiCameraClock {
    static func unixTimeMs() -> Int64 {
        return Int64(Date().timeIntervalSince1970 * 1000.0)
    }

    static func sample(_ label: String) -> MultiCameraClockSample {
        return MultiCameraClockSample(
            unixTimeMs: unixTimeMs(),
            monotonicTime: ProcessInfo.processInfo.systemUptime,
            label: label
        )
    }
}
