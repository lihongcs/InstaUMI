import Foundation
import INSCoreMedia

enum MultiCameraMediaParser {
    static func parseAndWriteSidecar(for mediaURL: URL, outputDirectory: URL) throws -> URL {
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)

        let sidecar = try parse(mediaURL: mediaURL, outputDirectory: outputDirectory)
        let sidecarURL = outputDirectory
            .appendingPathComponent(mediaURL.deletingPathExtension().lastPathComponent)
            .appendingPathExtension("sync.json")

        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(sidecar)
        try data.write(to: sidecarURL, options: .atomic)
        return sidecarURL
    }

    static func parse(mediaURL: URL, outputDirectory: URL) throws -> MultiCameraMediaSidecar {
        let ext = mediaURL.pathExtension.lowercased()
        if ["insv", "mp4", "mov", "lrv"].contains(ext) {
            return try parseVideo(mediaURL: mediaURL, outputDirectory: outputDirectory)
        }
        return try parseImage(mediaURL: mediaURL, outputDirectory: outputDirectory)
    }

    private static func parseVideo(mediaURL: URL, outputDirectory: URL) throws -> MultiCameraMediaSidecar {
        let parser = INSVideoInfoParser(url: mediaURL)
        guard parser.open() else {
            throw NSError(domain: "MultiCameraMediaParser", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "Failed to open video parser for \(mediaURL.lastPathComponent)"
            ])
        }
        defer {
            parser.close()
        }

        let extraInfo = parser.extraInfo
        let metadata = extraInfo?.metadata
        let gyroSummary = try summarizeVideoGyro(
            extraInfo?.gyroData,
            mediaURL: mediaURL,
            outputDirectory: outputDirectory
        )

        return MultiCameraMediaSidecar(
            mediaPath: mediaURL.path,
            mediaKind: "video",
            cameraType: metadata?.cameraType,
            serialNumber: metadata?.serialNumber,
            firmwareVersion: metadata?.fwVersion,
            createUnixTimeMs: unixMs(metadata?.createDate),
            firstFrameTimestampMs: metadata?.firstFrameTimestamp,
            firstGpsTimestampMs: metadata?.firstGpsTimestamp,
            gyroTimestampAdjustMs: metadata?.gyroTimestampAdjust,
            rollingShutterTimeMs: metadata?.rollingShutterTime,
            fileTotalTimeMs: metadata?.fileTotalTimeMs,
            fileSize: metadata?.fileSize,
            width: metadata.map { Double($0.fileDimension.width) },
            height: metadata.map { Double($0.fileDimension.height) },
            gyro: gyroSummary
        )
    }

    private static func parseImage(mediaURL: URL, outputDirectory: URL) throws -> MultiCameraMediaSidecar {
        let parser = INSImageInfoParser(url: mediaURL)
        guard parser.open() else {
            throw NSError(domain: "MultiCameraMediaParser", code: 2, userInfo: [
                NSLocalizedDescriptionKey: "Failed to open image parser for \(mediaURL.lastPathComponent)"
            ])
        }

        let extraInfo = parser.extraInfo
        let metadata = extraInfo?.metadata
        let imageGyro = parser.gyroData
        let gyroSummary = summarizeImageGyro(imageGyro)

        return MultiCameraMediaSidecar(
            mediaPath: mediaURL.path,
            mediaKind: "image",
            cameraType: metadata?.cameraType,
            serialNumber: metadata?.serialNumber,
            firmwareVersion: metadata?.fwVersion,
            createUnixTimeMs: unixMs(metadata?.createDate),
            firstFrameTimestampMs: metadata?.firstFrameTimestamp,
            firstGpsTimestampMs: metadata?.firstGpsTimestamp,
            gyroTimestampAdjustMs: metadata?.gyroTimestampAdjust,
            rollingShutterTimeMs: metadata?.rollingShutterTime,
            fileTotalTimeMs: metadata?.fileTotalTimeMs,
            fileSize: metadata?.fileSize,
            width: metadata.map { Double($0.fileDimension.width) } ?? Double(parser.width),
            height: metadata.map { Double($0.fileDimension.height) } ?? Double(parser.height),
            gyro: gyroSummary
        )
    }

    private static func summarizeVideoGyro(
        _ gyroData: INSExtraGyroData?,
        mediaURL: URL,
        outputDirectory: URL
    ) throws -> MultiCameraGyroSummary? {
        guard let gyroData = gyroData else {
            return nil
        }

        var rawGyroPath: String?
        if let raw = gyroData.gyroData {
            let rawURL = outputDirectory
                .appendingPathComponent(mediaURL.deletingPathExtension().lastPathComponent)
                .appendingPathExtension("gyro.bin")
            try raw.write(to: rawURL, options: .atomic)
            rawGyroPath = rawURL.path
        }

        var rawSecondaryGyroPath: String?
        if let raw = gyroData.gyroDataExt {
            let rawURL = outputDirectory
                .appendingPathComponent(mediaURL.deletingPathExtension().lastPathComponent)
                .appendingPathExtension("gyro2.bin")
            try raw.write(to: rawURL, options: .atomic)
            rawSecondaryGyroPath = rawURL.path
        }

        var first = ins_gyro_info()
        var last = ins_gyro_info()
        let firstSummary = gyroData.firstGyroDataInfo(&first) ? summarize(first) : nil
        let lastSummary = gyroData.last(&last) ? summarize(last) : nil

        return MultiCameraGyroSummary(
            count: Int(gyroData.count),
            timeOffsetMs: gyroData.timeOffset,
            firstSample: firstSummary,
            lastSample: lastSummary,
            rawGyroPath: rawGyroPath,
            rawSecondaryGyroPath: rawSecondaryGyroPath,
            imageAx: nil,
            imageAy: nil,
            imageAz: nil,
            imageGx: nil,
            imageGy: nil,
            imageGz: nil
        )
    }

    private static func summarizeImageGyro(_ gyro: INSMediaGyro?) -> MultiCameraGyroSummary? {
        guard let gyro = gyro else {
            return nil
        }

        return MultiCameraGyroSummary(
            count: 1,
            timeOffsetMs: nil,
            firstSample: nil,
            lastSample: nil,
            rawGyroPath: nil,
            rawSecondaryGyroPath: nil,
            imageAx: gyro.ax,
            imageAy: gyro.ay,
            imageAz: gyro.az,
            imageGx: gyro.gx,
            imageGy: gyro.gy,
            imageGz: gyro.gz
        )
    }

    private static func summarize(_ sample: ins_gyro_info) -> MultiCameraGyroSampleSummary {
        return MultiCameraGyroSampleSummary(
            timestampMs: sample.timestamp,
            gravityX: sample.gravity_x,
            gravityY: sample.gravity_y,
            gravityZ: sample.gravity_z,
            rotationX: sample.rotation_x,
            rotationY: sample.rotation_y,
            rotationZ: sample.rotation_z
        )
    }

    private static func unixMs(_ date: Date?) -> Int64? {
        guard let date = date else {
            return nil
        }
        return Int64(date.timeIntervalSince1970 * 1000.0)
    }
}
