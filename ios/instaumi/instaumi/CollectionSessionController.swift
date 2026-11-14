import Foundation
import Combine

enum CollectionTimestampEventType: String, Codable {
    case totalStart = "total_start"
    case totalEnd = "total_end"
    case clockSyncStart = "clock_sync_start"
    case clockSyncEnd = "clock_sync_end"
    case slamTimestamp = "slam_timestamp"
    case segmentStart = "segment_start"
    case segmentEnd = "segment_end"
    case windowsClockSync = "windows_clock_sync"
    case windowsControlStart = "windows_control_start"
    case windowsControlStop = "windows_control_stop"
    case cameraCommandSent = "camera_command_sent"
    case cameraCommandCompleted = "camera_command_completed"
}

struct CollectionTimestampEvent: Identifiable, Codable {
    let id: String
    let sessionID: String
    let type: CollectionTimestampEventType
    let title: String
    let unixTimeMs: Int64
    let isoTime: String
    let monotonicTime: TimeInterval
    let connectedCameraCount: Int
    let segmentIndex: Int?
    let note: String?
    let externalSystem: String?
    let externalClockOffsetMs: Double?
    let externalRoundTripMs: Double?
    let externalUnixTimeMs: Int64?
    let inputSource: String?
    let inputHandlerUnixTimeMs: Int64?
    let inputLatencyMs: Double?
}

struct CollectionActionResult {
    let success: Bool
    let message: String
}

struct CollectionInputTimestamp {
    let unixTimeMs: Int64
    let monotonicTime: TimeInterval
    let source: String
    let handlerUnixTimeMs: Int64
    let latencyMs: Double

    static func now() -> CollectionInputTimestamp {
        let nowUnixTimeMs = Int64(Date().timeIntervalSince1970 * 1000)
        return CollectionInputTimestamp(
            unixTimeMs: nowUnixTimeMs,
            monotonicTime: ProcessInfo.processInfo.systemUptime,
            source: "swift_now",
            handlerUnixTimeMs: nowUnixTimeMs,
            latencyMs: 0
        )
    }

    static func touchBegan(touchTimestamp: TimeInterval) -> CollectionInputTimestamp {
        let handlerUnixTimeMs = Int64(Date().timeIntervalSince1970 * 1000)
        let handlerUptime = ProcessInfo.processInfo.systemUptime
        let latencySec = max(handlerUptime - touchTimestamp, 0)
        let touchUnixTimeMs = handlerUnixTimeMs - Int64((latencySec * 1000).rounded())
        return CollectionInputTimestamp(
            unixTimeMs: touchUnixTimeMs,
            monotonicTime: touchTimestamp,
            source: "ui_touch_began",
            handlerUnixTimeMs: handlerUnixTimeMs,
            latencyMs: latencySec * 1000
        )
    }

    static func hardwareKeyDown(pressTimestamp: TimeInterval) -> CollectionInputTimestamp {
        let handlerUnixTimeMs = Int64(Date().timeIntervalSince1970 * 1000)
        let handlerUptime = ProcessInfo.processInfo.systemUptime
        let latencySec = max(handlerUptime - pressTimestamp, 0)
        let pressUnixTimeMs = handlerUnixTimeMs - Int64((latencySec * 1000).rounded())
        return CollectionInputTimestamp(
            unixTimeMs: pressUnixTimeMs,
            monotonicTime: pressTimestamp,
            source: "hardware_key_down",
            handlerUnixTimeMs: handlerUnixTimeMs,
            latencyMs: latencySec * 1000
        )
    }
}

final class CollectionSessionController: ObservableObject {
    @Published private(set) var isTotalRunning = false
    @Published private(set) var isClockSyncRunning = false
    @Published private(set) var isSegmentRunning = false
    @Published private(set) var currentSegmentIndex = 0
    @Published private(set) var completedSegmentCount = 0
    @Published private(set) var totalStartedAt: Date?
    @Published private(set) var totalEndedAt: Date?
    @Published private(set) var lastTotalDuration: TimeInterval = 0
    @Published private(set) var sessionID = UUID().uuidString
    @Published private(set) var logURL: URL?
    @Published private(set) var events: [CollectionTimestampEvent] = []
    @Published private(set) var lastMessage = "等待开始总采集"
    @Published var currentTaskName = "默认任务"

    private let isoFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    var slamTimestampEvents: [CollectionTimestampEvent] {
        events.filter { $0.type == .slamTimestamp }
    }

    var slamTimestampCount: Int {
        slamTimestampEvents.count
    }

    func elapsed(at date: Date = Date()) -> TimeInterval {
        guard isTotalRunning, let totalStartedAt else {
            return lastTotalDuration
        }

        return max(date.timeIntervalSince(totalStartedAt), 0)
    }

    func startTotal(
        connectedCameraCount: Int,
        inputTimestamp: CollectionInputTimestamp? = nil
    ) -> CollectionActionResult {
        guard connectedCameraCount > 0 else {
            return fail("未连接相机，不能启动总采集")
        }

        guard isTotalRunning == false else {
            return fail("总采集已经在运行")
        }

        beginNewSession()
        isTotalRunning = true
        totalStartedAt = Date()
        totalEndedAt = nil
        lastTotalDuration = 0

        appendEvent(
            type: .totalStart,
            title: "总采集启动",
            connectedCameraCount: connectedCameraCount,
            segmentIndex: nil,
            note: inputTimestamp == nil ? "开始连续拍摄" : "触摸按下瞬间记录；随后发送相机 startCapture",
            inputTimestamp: inputTimestamp
        )

        return succeed("总采集已启动")
    }

    func endTotal(
        connectedCameraCount: Int,
        inputTimestamp: CollectionInputTimestamp? = nil
    ) -> CollectionActionResult {
        guard isTotalRunning else {
            return fail("总采集尚未启动")
        }

        if isClockSyncRunning {
            appendEvent(
                type: .clockSyncEnd,
                title: "时钟对齐结束",
                connectedCameraCount: connectedCameraCount,
                segmentIndex: nil,
                note: "总采集结束时自动闭合"
            )
            isClockSyncRunning = false
        }

        if isSegmentRunning {
            appendEvent(
                type: .segmentEnd,
                title: "分段结束",
                connectedCameraCount: connectedCameraCount,
                segmentIndex: currentSegmentIndex,
                note: "总采集结束时自动闭合"
            )
            completedSegmentCount += 1
            isSegmentRunning = false
        }

        let endedAt = Date()
        if let totalStartedAt {
            lastTotalDuration = max(endedAt.timeIntervalSince(totalStartedAt), 0)
        }
        totalEndedAt = endedAt

        appendEvent(
            type: .totalEnd,
            title: "总采集结束",
            connectedCameraCount: connectedCameraCount,
            segmentIndex: nil,
            note: inputTimestamp == nil ? "停止连续拍摄" : "触摸按下瞬间记录；随后发送相机 stopCapture",
            inputTimestamp: inputTimestamp
        )

        isTotalRunning = false
        return succeed("总采集已结束")
    }

    func startClockSync(connectedCameraCount: Int) -> CollectionActionResult {
        guard isTotalRunning else {
            return fail("请先启动总采集，再开始时钟对齐")
        }

        guard isClockSyncRunning == false else {
            return fail("时钟对齐已经开始")
        }

        isClockSyncRunning = true
        appendEvent(
            type: .clockSyncStart,
            title: "时钟对齐启动",
            connectedCameraCount: connectedCameraCount,
            segmentIndex: nil,
            note: "只记录时间戳，不暂停总拍摄"
        )

        return succeed("时钟对齐已启动")
    }

    func endClockSync(connectedCameraCount: Int) -> CollectionActionResult {
        guard isClockSyncRunning else {
            return fail("时钟对齐尚未启动")
        }

        isClockSyncRunning = false
        appendEvent(
            type: .clockSyncEnd,
            title: "时钟对齐结束",
            connectedCameraCount: connectedCameraCount,
            segmentIndex: nil,
            note: "只记录时间戳，不暂停总拍摄"
        )

        return succeed("时钟对齐已结束")
    }

    func addSlamTimestamp(
        connectedCameraCount: Int,
        inputTimestamp: CollectionInputTimestamp? = nil
    ) -> CollectionActionResult {
        guard isTotalRunning else {
            return fail("请先启动总采集，再添加 SLAM 时间戳")
        }

        guard slamTimestampCount < 2 else {
            return fail("SLAM 时间戳已记录 2 个，请先删除再添加")
        }

        let timestampIndex = slamTimestampCount + 1
        appendEvent(
            type: .slamTimestamp,
            title: "SLAM 时刻 \(timestampIndex)",
            connectedCameraCount: connectedCameraCount,
            segmentIndex: nil,
            note: inputTimestamp == nil ? "手动添加当前 SLAM 时间戳" : "脚踏按下瞬间记录 SLAM 时间戳",
            inputTimestamp: inputTimestamp
        )

        return succeed("已添加 SLAM 时刻 \(timestampIndex)")
    }

    func deleteSlamTimestamp() -> CollectionActionResult {
        guard let eventIndex = events.lastIndex(where: { $0.type == .slamTimestamp }) else {
            return fail("当前没有可删除的 SLAM 时间戳")
        }

        let removedEvent = events.remove(at: eventIndex)
        rewriteLog()
        return succeed("已删除 \(removedEvent.title)")
    }

    func startSegment(
        connectedCameraCount: Int,
        inputTimestamp: CollectionInputTimestamp? = nil
    ) -> CollectionActionResult {
        guard isTotalRunning else {
            return fail("请先启动总采集，再开始分段")
        }

        guard isSegmentRunning == false else {
            return fail("当前分段尚未结束")
        }

        currentSegmentIndex += 1
        isSegmentRunning = true
        appendEvent(
            type: .segmentStart,
            title: "分段启动",
            connectedCameraCount: connectedCameraCount,
            segmentIndex: currentSegmentIndex,
            note: inputTimestamp == nil ? "只记录时间戳，不暂停总拍摄" : "触摸按下瞬间记录，不暂停总拍摄",
            inputTimestamp: inputTimestamp
        )

        return succeed("第 \(currentSegmentIndex) 段已启动")
    }

    func endSegment(
        connectedCameraCount: Int,
        inputTimestamp: CollectionInputTimestamp? = nil
    ) -> CollectionActionResult {
        guard isSegmentRunning else {
            return fail("当前没有正在运行的分段")
        }

        appendEvent(
            type: .segmentEnd,
            title: "分段结束",
            connectedCameraCount: connectedCameraCount,
            segmentIndex: currentSegmentIndex,
            note: inputTimestamp == nil ? "只记录时间戳，不暂停总拍摄" : "触摸按下瞬间记录，不暂停总拍摄",
            inputTimestamp: inputTimestamp
        )

        completedSegmentCount += 1
        isSegmentRunning = false
        return succeed("第 \(currentSegmentIndex) 段已结束")
    }

    func recordWindowsClockSync(
        offsetMs: Double,
        roundTripMs: Double,
        serverTimeMs: Int64?,
        connectedCameraCount: Int,
        note: String?
    ) {
        appendEvent(
            type: .windowsClockSync,
            title: "Windows 时钟差",
            connectedCameraCount: connectedCameraCount,
            segmentIndex: nil,
            note: note,
            externalSystem: "windows_htc",
            externalClockOffsetMs: offsetMs,
            externalRoundTripMs: roundTripMs,
            externalUnixTimeMs: serverTimeMs
        )
    }

    func recordWindowsControlStart(
        pid: Int?,
        connectedCameraCount: Int,
        note: String?
    ) {
        let pidNote = [note, pid.map { "PID \($0)" }]
            .compactMap { $0 }
            .joined(separator: "；")

        appendEvent(
            type: .windowsControlStart,
            title: "Windows HTC 启动",
            connectedCameraCount: connectedCameraCount,
            segmentIndex: nil,
            note: pidNote.isEmpty ? nil : pidNote,
            externalSystem: "windows_htc",
            externalClockOffsetMs: nil,
            externalRoundTripMs: nil,
            externalUnixTimeMs: nil
        )
    }

    func recordWindowsControlStop(connectedCameraCount: Int, note: String?) {
        appendEvent(
            type: .windowsControlStop,
            title: "Windows HTC 停止",
            connectedCameraCount: connectedCameraCount,
            segmentIndex: nil,
            note: note,
            externalSystem: "windows_htc",
            externalClockOffsetMs: nil,
            externalRoundTripMs: nil,
            externalUnixTimeMs: nil
        )
    }

    func recordCameraCommandEvent(_ commandEvent: MultiCameraCaptureEvent) {
        guard commandEvent.command == "startCapture"
            || commandEvent.command == "stopCapture"
            || commandEvent.command == "syncTimeMsToCamera" else {
            return
        }

        let eventType: CollectionTimestampEventType = commandEvent.stage == "completed"
            ? .cameraCommandCompleted
            : .cameraCommandSent
        let cameraDeltaMs = Self.parseCameraDeltaMs(from: commandEvent.cameraURI)
        let title = "\(commandEvent.cameraName) \(commandEvent.command) \(commandEvent.stage)"
        let noteParts = [
            "cameraID=\(commandEvent.cameraID)",
            "command=\(commandEvent.command)",
            "stage=\(commandEvent.stage)",
            commandEvent.cameraURI.map { "uri=\($0)" },
            commandEvent.errorDescription.map { "error=\($0)" },
        ].compactMap { $0 }

        appendEvent(
            type: eventType,
            title: title,
            connectedCameraCount: commandEvent.connectedCameraCount,
            segmentIndex: nil,
            note: noteParts.joined(separator: "；"),
            externalSystem: "insta360_camera",
            externalClockOffsetMs: cameraDeltaMs,
            externalRoundTripMs: nil,
            externalUnixTimeMs: nil,
            unixTimeMs: commandEvent.localUnixTimeMs,
            monotonicTime: commandEvent.monotonicTime
        )
    }

    private static func parseCameraDeltaMs(from uri: String?) -> Double? {
        guard let uri, uri.hasPrefix("deltaMs=") else {
            return nil
        }
        return Double(uri.replacingOccurrences(of: "deltaMs=", with: ""))
    }

    private func beginNewSession() {
        sessionID = UUID().uuidString
        events = []
        currentSegmentIndex = 0
        completedSegmentCount = 0
        isClockSyncRunning = false
        isSegmentRunning = false

        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd_HHmmss"
        let fileName = "timestamp_log_\(formatter.string(from: Date())).jsonl"
        logURL = timestampLogDirectory().appendingPathComponent(fileName)
        try? FileManager.default.createDirectory(
            at: timestampLogDirectory(),
            withIntermediateDirectories: true
        )
        FileManager.default.createFile(atPath: logURL?.path ?? "", contents: nil)
    }

    private func appendEvent(
        type: CollectionTimestampEventType,
        title: String,
        connectedCameraCount: Int,
        segmentIndex: Int?,
        note: String?,
        externalSystem: String? = nil,
        externalClockOffsetMs: Double? = nil,
        externalRoundTripMs: Double? = nil,
        externalUnixTimeMs: Int64? = nil,
        unixTimeMs: Int64? = nil,
        monotonicTime: TimeInterval? = nil,
        inputTimestamp: CollectionInputTimestamp? = nil
    ) {
        let now = Date()
        let eventUnixTimeMs = inputTimestamp?.unixTimeMs ?? unixTimeMs ?? Int64(now.timeIntervalSince1970 * 1000)
        let event = CollectionTimestampEvent(
            id: UUID().uuidString,
            sessionID: sessionID,
            type: type,
            title: title,
            unixTimeMs: eventUnixTimeMs,
            isoTime: isoFormatter.string(from: Date(timeIntervalSince1970: TimeInterval(eventUnixTimeMs) / 1000.0)),
            monotonicTime: inputTimestamp?.monotonicTime ?? monotonicTime ?? ProcessInfo.processInfo.systemUptime,
            connectedCameraCount: connectedCameraCount,
            segmentIndex: segmentIndex,
            note: note,
            externalSystem: externalSystem,
            externalClockOffsetMs: externalClockOffsetMs,
            externalRoundTripMs: externalRoundTripMs,
            externalUnixTimeMs: externalUnixTimeMs,
            inputSource: inputTimestamp?.source,
            inputHandlerUnixTimeMs: inputTimestamp?.handlerUnixTimeMs,
            inputLatencyMs: inputTimestamp?.latencyMs
        )

        events.append(event)
        writeEvent(event)
        lastMessage = "\(title)：\(event.isoTime)"
    }

    private func writeEvent(_ event: CollectionTimestampEvent) {
        guard let logURL else {
            return
        }

        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            let data = try encoder.encode(event)
            var line = Data()
            line.append(data)
            line.append(0x0A)

            if FileManager.default.fileExists(atPath: logURL.path) == false {
                FileManager.default.createFile(atPath: logURL.path, contents: nil)
            }

            let handle = try FileHandle(forWritingTo: logURL)
            try handle.seekToEnd()
            try handle.write(contentsOf: line)
            try handle.close()
        } catch {
            lastMessage = "时间戳日志写入失败：\(error.localizedDescription)"
        }
    }

    private func rewriteLog() {
        guard let logURL else {
            return
        }

        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]

            var fileData = Data()
            for event in events {
                let data = try encoder.encode(event)
                fileData.append(data)
                fileData.append(0x0A)
            }

            try FileManager.default.createDirectory(
                at: logURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            try fileData.write(to: logURL, options: .atomic)
        } catch {
            lastMessage = "时间戳日志更新失败：\(error.localizedDescription)"
        }
    }

    private func timestampLogDirectory() -> URL {
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first!
        return documents
            .appendingPathComponent("instaumi", isDirectory: true)
            .appendingPathComponent("timestamp_logs", isDirectory: true)
    }

    private func succeed(_ message: String) -> CollectionActionResult {
        lastMessage = message
        return CollectionActionResult(success: true, message: message)
    }

    private func fail(_ message: String) -> CollectionActionResult {
        lastMessage = message
        return CollectionActionResult(success: false, message: message)
    }
}
