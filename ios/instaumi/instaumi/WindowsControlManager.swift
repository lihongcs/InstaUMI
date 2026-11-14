import Foundation
import Combine
import Network

struct WindowsControlResult {
    let success: Bool
    let message: String
    let clockOffsetMs: Double?
    let roundTripMs: Double?
    let serverUnixTimeMs: Int64?
    let pid: Int?
    let isRemoteRunning: Bool
}

private struct WindowsControlResponse: Decodable {
    let requestId: String?
    let ok: Bool?
    let message: String?
    let serverUnixTimeMs: Int64?
    let serverReceiveUnixTimeMs: Int64?
    let serverSendUnixTimeMs: Int64?
    let pid: Int?
    let running: Bool?
}

private struct PendingUSBRequest {
    let localStartMs: Double
    let completion: ((WindowsControlResult) -> Void)?
}

final class WindowsControlManager: ObservableObject {
    private static let bonjourServiceName = "instaumi-htc"
    private static let bonjourServiceType = "_instaumi-htc._tcp"

    @Published var isEnabled: Bool {
        didSet {
            UserDefaults.standard.set(isEnabled, forKey: "windows_control_enabled")
            if isEnabled {
                startUSBListenerIfNeeded()
            } else {
                stopUSBListener(resetStatus: true)
            }
        }
    }

    @Published var usbPort: String {
        didSet {
            UserDefaults.standard.set(usbPort, forKey: "windows_control_usb_port")
            if isEnabled, oldValue != usbPort {
                restartUSBListener()
            }
        }
    }

    @Published var capID: String {
        didSet { UserDefaults.standard.set(capID, forKey: "windows_control_cap_id") }
    }

    @Published var startCollectingOnLaunch: Bool {
        didSet { UserDefaults.standard.set(startCollectingOnLaunch, forKey: "windows_control_collect_on_start") }
    }

    @Published private(set) var isConnected = false
    @Published private(set) var isBusy = false
    @Published private(set) var isRemoteRunning = false
    @Published private(set) var lastStatusMessage = "未连接 Windows 控制 bridge"
    @Published private(set) var lastClockOffsetMs: Double?
    @Published private(set) var lastRoundTripMs: Double?
    @Published private(set) var lastServerUnixTimeMs: Int64?
    @Published private(set) var lastPID: Int?

    private let usbQueue = DispatchQueue(label: "instaumi.windows-control.usb")
    private var usbListener: NWListener?
    private var usbConnection: NWConnection?
    private var usbConnectionReady = false
    private var usbReceiveBuffer = Data()
    private var pendingUSBRequests: [String: PendingUSBRequest] = [:]

    init() {
        self.isEnabled = UserDefaults.standard.bool(forKey: "windows_control_enabled")
        self.usbPort = UserDefaults.standard.string(forKey: "windows_control_usb_port") ?? "8766"
        self.capID = UserDefaults.standard.string(forKey: "windows_control_cap_id") ?? "112501"

        if UserDefaults.standard.object(forKey: "windows_control_collect_on_start") == nil {
            self.startCollectingOnLaunch = true
        } else {
            self.startCollectingOnLaunch = UserDefaults.standard.bool(forKey: "windows_control_collect_on_start")
        }
    }

    func prepareIfNeeded() {
        if isEnabled {
            startUSBListenerIfNeeded()
        }
    }

    func checkConnection(completion: ((WindowsControlResult) -> Void)? = nil) {
        sendUSBCommand(command: "ping", body: ["ipad_unix_time_ms": unixTimeMs()], completion: completion)
    }

    func startHTC(sessionID: String, taskName: String, completion: @escaping (WindowsControlResult) -> Void) {
        let capIDValue = Int(capID.trimmingCharacters(in: .whitespacesAndNewlines)) ?? 112501
        let body: [String: Any] = [
            "cap_id": capIDValue,
            "collect_on_start": startCollectingOnLaunch,
            "ipad_unix_time_ms": unixTimeMs(),
            "session_id": sessionID,
            "task_name": taskName
        ]
        sendUSBCommand(command: "start", body: body, completion: completion)
    }

    func stopHTC(completion: ((WindowsControlResult) -> Void)? = nil) {
        let body: [String: Any] = [
            "ipad_unix_time_ms": unixTimeMs()
        ]
        sendUSBCommand(command: "stop", body: body, completion: completion)
    }

    private func startUSBListenerIfNeeded() {
        guard usbListener == nil else {
            return
        }

        let trimmedPort = usbPort.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let portValue = UInt16(trimmedPort),
              let port = NWEndpoint.Port(rawValue: portValue) else {
            lastStatusMessage = "USB/LAN 端口无效"
            isConnected = false
            return
        }

        do {
            let listener = try NWListener(using: .tcp, on: port)
            listener.service = NWListener.Service(
                name: Self.bonjourServiceName,
                type: Self.bonjourServiceType
            )
            listener.stateUpdateHandler = { [weak self] state in
                DispatchQueue.main.async {
                    guard let self else { return }
                    switch state {
                    case .ready:
                        self.lastStatusMessage = "监听端口 \(portValue)，已广播 Bonjour，等待 Windows bridge 连接"
                    case .failed(let error):
                        self.lastStatusMessage = "端口监听失败：\(error.localizedDescription)"
                        self.isConnected = false
                        self.usbListener?.cancel()
                        self.usbListener = nil
                    case .cancelled:
                        break
                    default:
                        break
                    }
                }
            }
            listener.newConnectionHandler = { [weak self] connection in
                self?.acceptUSBConnection(connection)
            }
            listener.start(queue: usbQueue)
            usbListener = listener
            lastStatusMessage = "正在启动 USB/LAN 监听..."
        } catch {
            lastStatusMessage = "USB/LAN 监听启动失败：\(error.localizedDescription)"
            isConnected = false
        }
    }

    private func restartUSBListener() {
        stopUSBListener(resetStatus: false)
        startUSBListenerIfNeeded()
    }

    private func stopUSBListener(resetStatus: Bool) {
        usbQueue.async { [weak self] in
            guard let self else { return }
            self.usbListener?.cancel()
            self.usbConnection?.cancel()
            DispatchQueue.main.async {
                self.usbListener = nil
                self.usbConnection = nil
                self.usbConnectionReady = false
                self.usbReceiveBuffer = Data()
                self.pendingUSBRequests.removeAll()
                if resetStatus {
                    self.isConnected = false
                    self.isRemoteRunning = false
                }
            }
        }
    }

    private func acceptUSBConnection(_ connection: NWConnection) {
        usbQueue.async { [weak self] in
            guard let self else { return }
            self.usbConnection?.cancel()
            self.usbConnection = connection
            self.usbReceiveBuffer = Data()

            connection.stateUpdateHandler = { [weak self, weak connection] state in
                DispatchQueue.main.async {
                    guard let self, let connection, self.usbConnection === connection else { return }
                    switch state {
                    case .ready:
                        self.usbConnectionReady = true
                        self.isConnected = true
                        self.lastStatusMessage = "Windows 控制 bridge 已连接"
                    case .failed(let error):
                        self.cleanupUSBConnection(connection, message: "Windows bridge 连接失败：\(error.localizedDescription)")
                    case .cancelled:
                        self.cleanupUSBConnection(connection, message: "Windows bridge 连接已断开")
                    default:
                        break
                    }
                }
            }

            connection.start(queue: self.usbQueue)
            self.receiveUSB(on: connection)
        }
    }

    private func receiveUSB(on connection: NWConnection) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self, weak connection] data, _, isComplete, error in
            guard let self, let connection else { return }

            if let error {
                DispatchQueue.main.async {
                    self.cleanupUSBConnection(connection, message: "Windows bridge 接收失败：\(error.localizedDescription)")
                }
                return
            }

            if let data, data.isEmpty == false {
                self.usbReceiveBuffer.append(data)
                self.processUSBReceiveBuffer()
            }

            if isComplete {
                DispatchQueue.main.async {
                    self.cleanupUSBConnection(connection, message: "Windows 控制 bridge 已关闭")
                }
                return
            }

            if self.usbConnection === connection {
                self.receiveUSB(on: connection)
            }
        }
    }

    private func processUSBReceiveBuffer() {
        let newline = UInt8(ascii: "\n")
        while let newlineIndex = usbReceiveBuffer.firstIndex(of: newline) {
            let line = usbReceiveBuffer[..<newlineIndex]
            usbReceiveBuffer.removeSubrange(...newlineIndex)
            guard line.isEmpty == false else {
                continue
            }
            handleUSBLine(Data(line))
        }
    }

    private func handleUSBLine(_ data: Data) {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        guard let payload = try? decoder.decode(WindowsControlResponse.self, from: data) else {
            return
        }

        DispatchQueue.main.async {
            let localEndMs = Date().timeIntervalSince1970 * 1000
            if let requestID = payload.requestId,
               let pending = self.pendingUSBRequests.removeValue(forKey: requestID) {
                let timing = self.computeTiming(
                    payload: payload,
                    localStartMs: pending.localStartMs,
                    localEndMs: localEndMs
                )
                let message = payload.message ?? "Windows 控制 bridge 已响应"
                let ok = payload.ok ?? true
                self.apply(
                    result: WindowsControlResult(
                        success: ok,
                        message: ok ? message : "Windows 控制失败：\(message)",
                        clockOffsetMs: timing.offsetMs,
                        roundTripMs: timing.roundTripMs,
                        serverUnixTimeMs: payload.serverUnixTimeMs ?? payload.serverSendUnixTimeMs,
                        pid: payload.pid,
                        isRemoteRunning: payload.running ?? self.isRemoteRunning
                    ),
                    completion: pending.completion
                )
                return
            }

            self.isConnected = true
            self.isRemoteRunning = payload.running ?? self.isRemoteRunning
            self.lastPID = payload.pid ?? self.lastPID
            self.lastServerUnixTimeMs = payload.serverUnixTimeMs
                ?? payload.serverSendUnixTimeMs
                ?? payload.serverReceiveUnixTimeMs
            self.lastStatusMessage = payload.message ?? "Windows 控制 bridge 已连接"
        }
    }

    private func sendUSBCommand(
        command: String,
        body: [String: Any],
        completion: ((WindowsControlResult) -> Void)?
    ) {
        startUSBListenerIfNeeded()

        guard usbConnectionReady, let connection = usbConnection else {
            let result = WindowsControlResult(
                success: false,
                message: "Windows bridge 未连接：USB 模式请运行 start_usb_bridge.bat；网线模式请运行 start_lan_bridge.bat",
                clockOffsetMs: nil,
                roundTripMs: nil,
                serverUnixTimeMs: nil,
                pid: nil,
                isRemoteRunning: false
            )
            apply(result: result, completion: completion)
            return
        }

        let requestID = UUID().uuidString
        var message = body
        message["request_id"] = requestID
        message["command"] = command
        message["ipad_send_unix_time_ms"] = unixTimeMs()

        guard var data = try? JSONSerialization.data(withJSONObject: message) else {
            let result = WindowsControlResult(
                success: false,
                message: "Windows 控制命令编码失败",
                clockOffsetMs: nil,
                roundTripMs: nil,
                serverUnixTimeMs: nil,
                pid: nil,
                isRemoteRunning: isRemoteRunning
            )
            apply(result: result, completion: completion)
            return
        }
        data.append(0x0A)

        let localStartMs = Date().timeIntervalSince1970 * 1000
        pendingUSBRequests[requestID] = PendingUSBRequest(localStartMs: localStartMs, completion: completion)
        isBusy = true
        lastStatusMessage = "正在向 Windows 发送 \(command) 命令..."

        connection.send(content: data, completion: .contentProcessed { [weak self] error in
            guard let self else { return }
            if let error {
                DispatchQueue.main.async {
                    self.pendingUSBRequests.removeValue(forKey: requestID)
                    self.apply(
                        result: WindowsControlResult(
                            success: false,
                            message: "Windows bridge 发送失败：\(error.localizedDescription)",
                            clockOffsetMs: nil,
                            roundTripMs: Date().timeIntervalSince1970 * 1000 - localStartMs,
                            serverUnixTimeMs: nil,
                            pid: nil,
                            isRemoteRunning: self.isRemoteRunning
                        ),
                        completion: completion
                    )
                }
            }
        })
    }

    private func cleanupUSBConnection(_ connection: NWConnection, message: String) {
        guard usbConnection === connection else {
            return
        }

        connection.cancel()
        usbConnection = nil
        usbConnectionReady = false
        isConnected = false
        isRemoteRunning = false
        lastStatusMessage = message
    }

    private func computeTiming(
        payload: WindowsControlResponse?,
        localStartMs: Double,
        localEndMs: Double
    ) -> (offsetMs: Double?, roundTripMs: Double?) {
        let roundTripMs = localEndMs - localStartMs
        guard let payload else {
            return (nil, roundTripMs)
        }

        let serverReceive = payload.serverReceiveUnixTimeMs ?? payload.serverUnixTimeMs
        let serverSend = payload.serverSendUnixTimeMs ?? payload.serverUnixTimeMs
        guard let serverReceive, let serverSend else {
            return (nil, roundTripMs)
        }

        let serverMidpoint = (Double(serverReceive) + Double(serverSend)) / 2
        let localMidpoint = (localStartMs + localEndMs) / 2
        return (serverMidpoint - localMidpoint, roundTripMs)
    }

    private func apply(result: WindowsControlResult, completion: ((WindowsControlResult) -> Void)?) {
        isBusy = false
        isConnected = result.success || usbConnectionReady
        isRemoteRunning = result.isRemoteRunning
        lastStatusMessage = result.message
        lastClockOffsetMs = result.clockOffsetMs
        lastRoundTripMs = result.roundTripMs
        lastServerUnixTimeMs = result.serverUnixTimeMs
        lastPID = result.pid
        completion?(result)
    }

    private func unixTimeMs() -> Int64 {
        Int64(Date().timeIntervalSince1970 * 1000)
    }
}
