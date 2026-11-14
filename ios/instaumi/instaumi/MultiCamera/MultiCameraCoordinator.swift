import Foundation
import Combine
import INSCameraSDK
import INSCameraServiceSDK
import INSCoreMedia
import UIKit

protocol MultiCameraCoordinatorDelegate: AnyObject {
    func multiCameraCoordinatorDidUpdateDevices(_ coordinator: MultiCameraCoordinator)
    func multiCameraCoordinator(_ coordinator: MultiCameraCoordinator, didAppendLog message: String)
}

final class MultiCameraCoordinator: NSObject, INSBluetoothManagerDelegate, ObservableObject {
    static let shared = MultiCameraCoordinator()

    @Published var isConnected: Bool = false
    @Published var connectedCount: Int = 0
    @Published private(set) var inspectionItems: [MultiCameraInspectionItem] = []
    @Published private(set) var cameraRemoteResources: [MultiCameraRemoteResource] = []
    @Published private(set) var cameraResourceStatusText: String = "未读取相机记录"

    weak var delegate: MultiCameraCoordinatorDelegate?

    private let bluetoothManager = INSBluetoothManager()
    private(set) var devices: [MultiCameraDeviceRecord] = []
    private(set) var captureEvents: [MultiCameraCaptureEvent] = []
    @Published private(set) var downloadedResources: [MultiCameraDownloadedResource] = []
    private(set) var lastMultiCameraSyncUnixTimeMs: Int64?
    @Published private(set) var wifiSocketStatusText: String = "未连接"
    var commandEventHandler: ((MultiCameraCaptureEvent) -> Void)?

    private var activeImageExporters: [INSExportImageSimplify] = []
    private var activeVideoExporters: [INSExportSimplify] = []
    private var shouldRefreshWiFiSocketWhenActive = false
    private var periodicTimeSyncTimer: Timer?
    private var isDataCollectionActive = false
    private var lastWiFiSocketSetupAt: Date?
    private let wifiSocketSetupCooldown: TimeInterval = 3.0
    private let dataProcessingQueue = DispatchQueue(label: "com.instaumi.data-processing", qos: .userInitiated)

    let workingDirectory: URL
    let eventLogURL: URL

    var connectedCameraCount: Int {
        return commandRecords().count
    }

    var captureMode: MultiCameraCaptureMode {
        switch connectedCameraCount {
        case 0:
            return .noCamera
        case 1:
            return .singleX5
        default:
            return .multiX5
        }
    }

    var hasCompletedMultiCameraSync: Bool {
        return lastMultiCameraSyncUnixTimeMs != nil
    }

    override init() {
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first!
        let root = documents.appendingPathComponent("instaumi", isDirectory: true)
        self.workingDirectory = root
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd_HHmmss"
        self.eventLogURL = root
            .appendingPathComponent("sessions", isDirectory: true)
            .appendingPathComponent("session_\(formatter.string(from: Date())).json")

        super.init()
        bluetoothManager.delegate = self
        ensureWorkingDirectories()
        refreshWiFiSocketStatus()
        loadInspectionItems()
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(applicationDidBecomeActive),
            name: UIApplication.didBecomeActiveNotification,
            object: nil
        )
        startPeriodicTimeSync()
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
        periodicTimeSyncTimer?.invalidate()
    }

    private func startPeriodicTimeSync() {
        periodicTimeSyncTimer?.invalidate()
        periodicTimeSyncTimer = Timer.scheduledTimer(withTimeInterval: 10.0, repeats: true) { [weak self] _ in
            guard let self, self.isDataCollectionActive == false else {
                return
            }
            self.syncTimeOnAll(tryCount: 5, maxDeltaMs: 40, silent: true)
        }
    }

    func setDataCollectionActive(_ active: Bool) {
        isDataCollectionActive = active
    }

    func scanBluetoothCameras(duration: TimeInterval = 4.0) {
        devices.removeAll()
        lastMultiCameraSyncUnixTimeMs = nil
        appendLog("正在扫描影石 X5 蓝牙相机，时长 \(Int(duration)) 秒")
        notifyDevicesChanged()

        bluetoothManager.scanedDeviceRepeatCallback = false
        bluetoothManager.scanCameras { [weak self] device, rssi, _ in
            DispatchQueue.main.async {
                self?.upsert(device: device, rssi: rssi.intValue)
            }
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + duration) { [weak self] in
            self?.bluetoothManager.stopScan()
            self?.appendLog("扫描结束，发现 \(self?.devices.count ?? 0) 台相机")
        }
    }

    func connectAllBluetoothCameras() {
        let targets = devices.filter { $0.bluetoothDevice != nil }
        guard targets.isEmpty == false else {
            appendLog("没有可连接的影石 X5 蓝牙相机")
            return
        }

        appendLog("正在连接 \(targets.count) 台影石 X5 蓝牙相机")
        targets.forEach { connect($0) }
    }

    func connectBluetoothCamera(at index: Int) {
        guard devices.indices.contains(index) else {
            return
        }
        appendLog("正在连接单台影石 X5：\(devices[index].name)")
        connect(devices[index])
    }

    func disconnectBluetoothCamera(at index: Int) {
        guard devices.indices.contains(index) else {
            return
        }

        let record = devices[index]
        if let device = record.bluetoothDevice {
            bluetoothManager.disconnectDevice(device)
        }
        record.state = .disconnected
        record.commandManager = nil
        lastMultiCameraSyncUnixTimeMs = nil
        appendLog("\(record.name) 已由用户断开蓝牙连接")
        notifyDevicesChanged()
    }

    func disconnectAllBluetoothCameras() {
        devices.forEach { record in
            if let device = record.bluetoothDevice {
                bluetoothManager.disconnectDevice(device)
            }
            record.state = .disconnected
            record.commandManager = nil
        }
        lastMultiCameraSyncUnixTimeMs = nil
        appendLog("已断开蓝牙相机")
        notifyDevicesChanged()
    }

    func setupWiFiSocketManager() {
        refreshWiFiSocketStatus()
        if INSCameraManager.socket().cameraState == .connected {
            shouldRefreshWiFiSocketWhenActive = false
            appendLog("Wi-Fi 管理器已连接")
            return
        }
        shouldRefreshWiFiSocketWhenActive = true
        resetAndSetupWiFiSocket(reason: "手动连接 Wi-Fi 通道")
        refreshWiFiSocketStatus()
        appendLog("已启动 Wi-Fi 管理器；如有需要，请将手机 Wi-Fi 连接到目标相机")
    }

    func resetWiFiSocketManager() {
        shouldRefreshWiFiSocketWhenActive = true
        resetAndSetupWiFiSocket(reason: "用户手动重置 Wi-Fi 通道", force: true)
        refreshWiFiSocketStatus()
    }

    func shutdownWiFiSocketManager() {
        INSCameraManager.socket().shutdown()
        refreshWiFiSocketStatus()
        appendLog("已停止 Wi-Fi 管理器")
    }

    func refreshWiFiSocketStatus() {
        wifiSocketStatusText = describeCameraState(INSCameraManager.socket().cameraState)
    }

    private func resetAndSetupWiFiSocket(reason: String, force: Bool = false) {
        let state = INSCameraManager.socket().cameraState
        if state == .connected {
            shouldRefreshWiFiSocketWhenActive = false
            refreshWiFiSocketStatus()
            appendLog("\(reason)：Wi-Fi Socket 已连接，不重复挂载")
            return
        }

        if force == false,
           let lastWiFiSocketSetupAt,
           Date().timeIntervalSince(lastWiFiSocketSetupAt) < wifiSocketSetupCooldown {
            refreshWiFiSocketStatus()
            appendLog("\(reason)：Wi-Fi Socket 正在挂载/冷却中，先等待结果，当前状态：\(wifiSocketStatusText)")
            return
        }

        lastWiFiSocketSetupAt = Date()
        appendLog("\(reason)：重置并挂载 Wi-Fi Socket，当前状态：\(describeCameraState(state))")
        INSCameraManager.socket().shutdown()
        INSCameraManager.socket().setup()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
            guard let self = self else { return }
            self.refreshWiFiSocketStatus()
            self.appendLog("\(reason)：Wi-Fi Socket 挂载结果：\(self.wifiSocketStatusText)")
        }
    }

    private func openSystemWiFiSettings() {
        if let wifiURL = URL(string: "App-Prefs:root=WIFI") {
            UIApplication.shared.open(wifiURL, options: [:]) { success in
                guard success == false,
                      let settingsURL = URL(string: UIApplication.openSettingsURLString) else {
                    return
                }
                UIApplication.shared.open(settingsURL, options: [:], completionHandler: nil)
            }
            return
        }

        if let settingsURL = URL(string: UIApplication.openSettingsURLString) {
            UIApplication.shared.open(settingsURL, options: [:], completionHandler: nil)
        }
    }

    func openCameraWiFiOnAll(channel: UInt32 = 0) {
        let targets = commandRecords()
        guard targets.isEmpty == false else {
            appendLog("没有已连通的蓝牙相机，无法发送打开热点指令")
            return
        }
        appendLog("正在通过蓝牙命令相机开启 Wi-Fi 热点...")
        shouldRefreshWiFiSocketWhenActive = true

        let options = INSCameraRequestOptions()
        options.repeatCount = 3
        options.timeout = 20.0

        targets.forEach { record, commandManager in
            appendCommandEvent(record: record, command: "openCameraWifi", stage: "sent", uri: nil, error: nil)
            commandManager.openCameraWifi(with: options, channel: channel) { [weak self, weak record] error in
                guard let self = self, let record = record else { return }
                self.appendCommandEvent(
                    record: record,
                    command: "openCameraWifi",
                    stage: "completed",
                    uri: nil,
                    error: error
                )
                self.appendLog("\(record.name) 打开 Wi-Fi：\(self.describe(error))")
            }
        }
    }

    func openCameraWiFi(for record: MultiCameraDeviceRecord, channel: UInt32 = 0) {
        shouldRefreshWiFiSocketWhenActive = true
        var targetSSID: String? = nil

        if record.name.contains("3DJ8JT") {
            targetSSID = "X5 3DJ8JT.OSC"
        } else if record.name.contains("DGRTS") {
            targetSSID = "\(record.name).OSC"
        }

        let ssidHint = targetSSID ?? "\(record.name).OSC"

        guard let commandManager = record.commandManager else {
            appendLog("\(record.name) 蓝牙未连通，无法开启热点。请先连接蓝牙相机。")
            return
        }

        appendLog("正在通过蓝牙命令 \(record.name) 开启 Wi-Fi 热点...")
        appendLog("系统 Wi-Fi 页面打开后，请手动连接相机热点：\(ssidHint)")

        let options = INSCameraRequestOptions()
        options.repeatCount = 3
        options.timeout = 20.0

        appendCommandEvent(record: record, command: "openCameraWifi", stage: "sent", uri: nil, error: nil)
        commandManager.openCameraWifi(with: options, channel: channel) { [weak self, weak record] error in
            guard let self = self, let record = record else { return }
            self.appendCommandEvent(
                record: record,
                command: "openCameraWifi",
                stage: "completed",
                uri: nil,
                error: error
            )
            self.appendLog("\(record.name) 打开 Wi-Fi：\(self.describe(error))")
            if error == nil {
                self.appendLog("请在系统 Wi-Fi 中手动连接 \(ssidHint)，连接完成后回到采集助手挂载 Wi-Fi 通道。")
            }
        }

        DispatchQueue.main.async {
            self.openSystemWiFiSettings()
        }
    }

    func syncTimeOnAll(tryCount: UInt = 5, maxDeltaMs: UInt = 30, silent: Bool = false) {
        let targets = commandRecords()
        guard targets.isEmpty == false else {
            if !silent { appendLog("没有已连接的影石 X5 相机可进行时钟同步") }
            return
        }

        lastMultiCameraSyncUnixTimeMs = nil
        if !silent { appendLog("多台 X5 模式：正在同步 \(targets.count) 台相机的时间") }

        var finishedCount = 0
        var successCount = 0
        targets.forEach { record, commandManager in
            if !silent { appendCommandEvent(record: record, command: "syncTimeMsToCamera", stage: "sent", uri: nil, error: nil) }
            commandManager.syncTimeMsToCamera(withTryCount: tryCount, dTimeMsMax: maxDeltaMs) { [weak self, weak record] deltaMs, error in
                DispatchQueue.main.async {
                    guard let self = self, let record = record else { return }
                    let uri = error == nil ? "deltaMs=\(deltaMs)" : nil
                    if !silent {
                        self.appendCommandEvent(
                            record: record,
                            command: "syncTimeMsToCamera",
                            stage: "completed",
                            uri: uri,
                            error: error
                        )
                    }
                    if error == nil {
                        successCount += 1
                    }
                    finishedCount += 1
                    if !silent {
                        if let err = error {
                            self.appendLog("\(record.name) 时钟偏差：\(deltaMs) ms，\(self.describe(err))")
                        } else {
                            self.appendLog("\(record.name) 时钟偏差：\(deltaMs) ms")
                        }
                    }

                    if finishedCount == targets.count {
                        if successCount == targets.count {
                            self.lastMultiCameraSyncUnixTimeMs = MultiCameraClock.unixTimeMs()
                            if !silent { self.appendLog("多台 X5 时钟同步完成：\(successCount)/\(targets.count) 台成功") }
                        } else {
                            if !silent {
                                self.appendLog("多台 X5 时钟同步未完成：\(successCount)/\(targets.count) 台成功")
                            }
                        }
                        self.notifyDevicesChanged()
                    }
                }
            }
        }
    }

    func takePictureOnAll() {
        let targets = commandRecords()
        guard targets.isEmpty == false else {
            appendLog("没有已连接的蓝牙相机可拍照")
            return
        }
        appendCaptureModeLog(for: "拍照", targetCount: targets.count)

        targets.forEach { record, commandManager in
            let options = INSTakePictureOptions()
            let metadata = INSExtraMetadata()
            metadata.createDate = Date()
            options.extraMetadata = metadata.serializeToProtobufData()

            appendCommandEvent(record: record, command: "takePicture", stage: "sent", uri: nil, error: nil)
            commandManager.takePicture(with: options) { [weak self, weak record] error, photoInfo in
                guard let self = self, let record = record else { return }
                let uri = photoInfo?.uri
                record.lastPhotoURI = uri
                record.lastError = error?.localizedDescription
                self.appendCommandEvent(
                    record: record,
                    command: "takePicture",
                    stage: "completed",
                    uri: uri,
                    error: error
                )
                self.appendLog("\(record.name) 照片 URI：\(uri ?? "-") \(self.describe(error))")
                self.notifyDevicesChanged()
            }
        }
    }

    func startRecordingOnAll() {
        let targets = commandRecords()
        guard targets.isEmpty == false else {
            appendLog("没有已连接的蓝牙相机可开始录像")
            return
        }
        appendCaptureModeLog(for: "开始录像", targetCount: targets.count)

        targets.forEach { record, commandManager in
            let options = INSCaptureOptions()
            let metadata = INSExtraMetadata()
            metadata.createDate = Date()
            options.extraMetadata = metadata.serializeToProtobufData()

            appendCommandEvent(record: record, command: "startCapture", stage: "sent", uri: nil, error: nil)
            commandManager.startCapture(with: options) { [weak self, weak record] error in
                guard let self = self, let record = record else { return }
                record.lastError = error?.localizedDescription
                self.appendCommandEvent(
                    record: record,
                    command: "startCapture",
                    stage: "completed",
                    uri: nil,
                    error: error
                )
                self.appendLog("\(record.name) 开始录像：\(self.describe(error))")
                self.notifyDevicesChanged()
            }
        }
    }

    func stopRecordingOnAll() {
        let targets = commandRecords()
        guard targets.isEmpty == false else {
            appendLog("没有已连接的蓝牙相机可停止录像")
            return
        }
        appendCaptureModeLog(for: "停止录像", targetCount: targets.count)

        targets.forEach { record, commandManager in
            appendCommandEvent(record: record, command: "stopCapture", stage: "sent", uri: nil, error: nil)
            commandManager.stopCapture(with: nil) { [weak self, weak record] error, videoInfo in
                guard let self = self, let record = record else { return }
                let uri = videoInfo?.uri
                record.lastVideoURI = uri
                record.lastError = error?.localizedDescription
                self.appendCommandEvent(
                    record: record,
                    command: "stopCapture",
                    stage: "completed",
                    uri: uri,
                    error: error
                )
                if let uri = uri, error == nil {
                    guard let metadataCommandManager = commandManager as? INSCameraSimpleUSBCommands else {
                        self.appendLog("metadata 读取跳过 \(uri)：当前 command manager 不支持 getFileMnd")
                        self.notifyDevicesChanged()
                        return
                    }
                    self.writeRemoteMetadataSidecar(
                        uri: uri,
                        kind: "video",
                        commandManager: metadataCommandManager,
                        cameraName: record.name
                    )
                }
                self.appendLog("\(record.name) 视频 URI：\(uri ?? "-") \(self.describe(error))")
                self.notifyDevicesChanged()
            }
        }
    }

    func fetchCameraResourceRecords() {
        cameraResourceStatusText = "正在读取相机记录"
        if INSCameraManager.socket().cameraState == .connected {
            refreshWiFiSocketStatus()
            shouldRefreshWiFiSocketWhenActive = false
            executeFetchCameraResourceRecords()
        } else {
            shouldRefreshWiFiSocketWhenActive = true
            refreshWiFiSocketStatus()
            appendLog("读取相机记录前需要 Wi-Fi Socket，当前状态：\(wifiSocketStatusText)")
            resetAndSetupWiFiSocket(reason: "读取相机记录前")
            waitForSocketThenFetchCameraRecords(attempt: 1, maxAttempts: 10)
        }
    }

    func downloadCameraResources(_ resources: [MultiCameraRemoteResource]) {
        guard resources.isEmpty == false else {
            appendLog("请先选择要下载的相机记录")
            return
        }

        if INSCameraManager.socket().cameraState == .connected {
            refreshWiFiSocketStatus()
            executeDownloadCameraResources(resources)
        } else {
            shouldRefreshWiFiSocketWhenActive = true
            refreshWiFiSocketStatus()
            appendLog("下载选中记录前需要 Wi-Fi Socket，当前状态：\(wifiSocketStatusText)")
            resetAndSetupWiFiSocket(reason: "下载相机记录前")
            waitForSocketThenDownload(resources, attempt: 1, maxAttempts: 10)
        }
    }



    private func executeFetchCameraResourceRecords() {
        let commandManager = INSCameraManager.socket().commandManager
        let options = INSGetFileListOptions()
        options.type = .camera

        appendLog("正在读取相机上的数据记录")
        commandManager.fetchPhotoList(with: options) { [weak self] photoError, photoResources in
            guard let self = self else { return }
            let photos = photoResources?.cameraResources ?? []

            commandManager.fetchVideoList(with: options) { [weak self] videoError, videoResources in
                guard let self = self else { return }
                let videos = videoResources?.cameraResources ?? []

                if let photoError = photoError {
                    self.appendLog("读取照片记录失败：\(photoError.localizedDescription)")
                }
                if let videoError = videoError {
                    self.appendLog("读取视频记录失败：\(videoError.localizedDescription)")
                }

                let remoteResources = photos.map { self.makeRemoteResource(kind: "photo", fileInfo: $0) }
                    + videos.map { self.makeRemoteResource(kind: "video", fileInfo: $0) }
                let sortedResources = remoteResources.sorted { $0.uri > $1.uri }

                DispatchQueue.main.async {
                    self.cameraRemoteResources = sortedResources
                    self.cameraResourceStatusText = sortedResources.isEmpty
                        ? "相机上没有读取到记录"
                        : "已读取 \(sortedResources.count) 条相机记录"
                }
                self.appendLog("已读取 \(sortedResources.count) 条相机记录，请选择需要下载的数据")
            }
        }
    }

    private func executeDownloadCameraResources(_ resources: [MultiCameraRemoteResource]) {
        let cameraManager = INSCameraManager.socket()
        let commandManager = cameraManager.commandManager
        let camera = cameraManager.currentCamera
        let cameraID = camera?.serialNumber ?? "current-camera"
        let cameraName = camera?.name ?? "当前相机"

        appendLog("开始下载选中的 \(resources.count) 条相机记录")
        resources.forEach { resource in
            download(
                uri: resource.uri,
                kind: resource.kind,
                commandManager: commandManager,
                cameraID: cameraID,
                cameraName: cameraName
            )
        }
    }

    func deleteCameraResources(_ resources: [MultiCameraRemoteResource]) {
        appendLog("收到删除相机文件请求：\(resources.count) 个")
        guard resources.isEmpty == false else {
            appendLog("删除相机文件已取消：未选择任何相机记录")
            return
        }

        if INSCameraManager.socket().cameraState == .connected {
            refreshWiFiSocketStatus()
            executeDeleteCameraResources(resources)
        } else {
            shouldRefreshWiFiSocketWhenActive = true
            refreshWiFiSocketStatus()
            appendLog("删除相机文件前需要 Wi-Fi Socket，当前状态：\(wifiSocketStatusText)")
            resetAndSetupWiFiSocket(reason: "删除相机文件前")
            waitForSocketThenDelete(resources, attempt: 1, maxAttempts: 10)
        }
    }

    private func executeDeleteCameraResources(_ resources: [MultiCameraRemoteResource]) {
        let uris = resources.map { $0.uri }
        guard uris.isEmpty == false else { return }

        let commandManager = INSCameraManager.socket().commandManager
        let preview = uris.prefix(3).joined(separator: "，")
        appendLog("准备通过 Wi-Fi 命令通道删除相机上的 \(uris.count) 个文件：\(preview)")

        commandManager.deleteFiles(uris) { [weak self] error in
            DispatchQueue.main.async {
                guard let self = self else { return }
                if let error = error {
                    self.refreshWiFiSocketStatus()
                    self.appendLog("删除相机文件失败：\(error.localizedDescription)。Socket：\(self.wifiSocketStatusText)，请确认 iPad 当前 Wi-Fi 仍连接相机热点，且相机没有进入待机。")
                } else {
                    self.appendLog("成功删除 \(uris.count) 个相机文件")
                    self.executeFetchCameraResourceRecords()
                }
            }
        }
    }

    private func waitForSocketThenDelete(_ resources: [MultiCameraRemoteResource], attempt: Int, maxAttempts: Int) {
        if INSCameraManager.socket().cameraState == .connected {
            refreshWiFiSocketStatus()
            shouldRefreshWiFiSocketWhenActive = false
            executeDeleteCameraResources(resources)
            return
        }

        guard attempt < maxAttempts else {
            refreshWiFiSocketStatus()
            appendLog("Wi-Fi 通道未连接，无法删除相机记录。请在系统 Wi-Fi 中确认已连接影石相机热点，然后回到 App 点“重置通道”，再重新删除。")
            return
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            guard let self = self else { return }
            self.refreshWiFiSocketStatus()
            self.appendLog("等待 Wi-Fi 相机后删除记录（\(attempt + 1)/\(maxAttempts)），Socket 状态：\(self.wifiSocketStatusText)")
            self.waitForSocketThenDelete(resources, attempt: attempt + 1, maxAttempts: maxAttempts)
        }
    }

    private func waitForSocketThenFetchCameraRecords(attempt: Int, maxAttempts: Int) {
        if INSCameraManager.socket().cameraState == .connected {
            refreshWiFiSocketStatus()
            shouldRefreshWiFiSocketWhenActive = false
            executeFetchCameraResourceRecords()
            return
        }

        guard attempt < maxAttempts else {
            cameraResourceStatusText = "Wi-Fi 通道未连接，无法读取相机记录"
            appendLog("Wi-Fi 通道仍未识别到相机，无法读取相机记录。请确认 iPad 当前 Wi-Fi 是影石相机热点，不是路由器。")
            return
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            guard let self = self else { return }
            self.refreshWiFiSocketStatus()
            self.appendLog("等待 Wi-Fi 相机后读取记录（\(attempt + 1)/\(maxAttempts)），Socket 状态：\(self.wifiSocketStatusText)")
            self.waitForSocketThenFetchCameraRecords(attempt: attempt + 1, maxAttempts: maxAttempts)
        }
    }

    private func waitForSocketThenDownload(_ resources: [MultiCameraRemoteResource], attempt: Int, maxAttempts: Int) {
        if INSCameraManager.socket().cameraState == .connected {
            refreshWiFiSocketStatus()
            shouldRefreshWiFiSocketWhenActive = false
            executeDownloadCameraResources(resources)
            return
        }

        guard attempt < maxAttempts else {
            appendLog("Wi-Fi 通道仍未识别到相机，无法下载选中记录。请确认 iPad 当前 Wi-Fi 是影石相机热点，不是路由器。")
            return
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            guard let self = self else { return }
            self.refreshWiFiSocketStatus()
            self.appendLog("等待 Wi-Fi 相机后下载选中记录（\(attempt + 1)/\(maxAttempts)），Socket 状态：\(self.wifiSocketStatusText)")
            self.waitForSocketThenDownload(resources, attempt: attempt + 1, maxAttempts: maxAttempts)
        }
    }

    private func makeRemoteResource(kind: String, fileInfo: INSCameraBaseFileInfo) -> MultiCameraRemoteResource {
        let uri = fileInfo.uri
        let fileName = URL(string: uri)?.lastPathComponent ?? uri.components(separatedBy: "/").last ?? uri
        let localFileName = safeFileName(for: uri, kind: kind)
        let cameraName = INSCameraManager.socket().currentCamera?.name ?? "当前相机"
        let cameraDir = workingDirectory.appendingPathComponent(cameraName)
        let isDownloaded = FileManager.default.fileExists(atPath: cameraDir.appendingPathComponent(localFileName).path)
        let videoInfo = fileInfo as? INSCameraVideoInfo

        return MultiCameraRemoteResource(
            id: "\(kind):\(uri)",
            kind: kind,
            uri: uri,
            fileName: fileName.isEmpty ? uri : fileName,
            fileSizeBytes: videoInfo?.fileSize,
            durationSeconds: videoInfo.map { UInt32(Double($0.totalTime) / 1000.0) },
            isDownloaded: isDownloaded
        )
    }

    private func refreshRemoteDownloadStates() {
        let cameraName = INSCameraManager.socket().currentCamera?.name ?? "当前相机"
        let cameraDir = workingDirectory.appendingPathComponent(cameraName)

        let updatedResources = cameraRemoteResources.map { resource in
            MultiCameraRemoteResource(
                id: resource.id,
                kind: resource.kind,
                uri: resource.uri,
                fileName: resource.fileName,
                fileSizeBytes: resource.fileSizeBytes,
                durationSeconds: resource.durationSeconds,
                isDownloaded: FileManager.default.fileExists(
                    atPath: cameraDir.appendingPathComponent(safeFileName(for: resource.uri, kind: resource.kind)).path
                )
            )
        }
        DispatchQueue.main.async {
            self.cameraRemoteResources = updatedResources
        }
    }

    func downloadLatestFromCurrentCamera(limitPerKind: Int = 2) {
        if INSCameraManager.socket().cameraState == .connected {
            refreshWiFiSocketStatus()
            shouldRefreshWiFiSocketWhenActive = false
            executeDownloadLatestFromSocket(limitPerKind: limitPerKind)
        } else {
            shouldRefreshWiFiSocketWhenActive = true
            refreshWiFiSocketStatus()
            appendLog("检测到 Wi-Fi 相机尚未挂载，Socket 状态：\(describeCameraState(INSCameraManager.socket().cameraState))")
            resetAndSetupWiFiSocket(reason: "下载最新记录前")
            waitForSocketConnection(limitPerKind: limitPerKind, attempt: 1, maxAttempts: 10)
        }
    }

    private func waitForSocketConnection(limitPerKind: Int, attempt: Int, maxAttempts: Int) {
        if INSCameraManager.socket().cameraState == .connected {
            refreshWiFiSocketStatus()
            shouldRefreshWiFiSocketWhenActive = false
            appendLog("Wi-Fi 相机已连接，开始执行下载任务")
            executeDownloadLatestFromSocket(limitPerKind: limitPerKind)
            return
        }

        guard attempt < maxAttempts else {
            appendLog("Wi-Fi 通道仍未识别到相机。请确认系统 Wi-Fi 已连接到相机热点，然后回到 App 再点下载。")
            return
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            guard let self = self else { return }
            self.refreshWiFiSocketStatus()
            let state = self.describeCameraState(INSCameraManager.socket().cameraState)
            self.appendLog("正在等待 Wi-Fi 相机挂载（\(attempt + 1)/\(maxAttempts)），Socket 状态：\(state)")
            self.waitForSocketConnection(limitPerKind: limitPerKind, attempt: attempt + 1, maxAttempts: maxAttempts)
        }
    }

    private func executeDownloadLatestFromSocket(limitPerKind: Int) {
        let cameraManager = INSCameraManager.socket()
        let commandManager = cameraManager.commandManager
        let camera = cameraManager.currentCamera
        let cameraID = camera?.serialNumber ?? "current-camera"
        let cameraName = camera?.name ?? "当前相机"
        let options = INSGetFileListOptions()
        options.type = .camera

        appendLog("正在通过 Wi-Fi 获取当前相机文件列表")
        commandManager.fetchPhotoList(with: options) { [weak self] photoError, photoResources in
            guard let self = self else { return }
            let photos = photoResources?.cameraResources ?? []

            commandManager.fetchVideoList(with: options) { [weak self] videoError, videoResources in
                guard let self = self else { return }
                let videos = videoResources?.cameraResources ?? []
                let selectedPhotos = photos.sorted(by: { $0.uri > $1.uri }).prefix(limitPerKind)
                let selectedVideos = videos.sorted(by: { $0.uri > $1.uri }).prefix(limitPerKind)

                if let photoError = photoError {
                    self.appendLog("获取照片列表失败：\(photoError.localizedDescription)")
                }
                if let videoError = videoError {
                    self.appendLog("获取视频列表失败：\(videoError.localizedDescription)")
                }

                let resources = selectedPhotos.map { ("photo", $0) } + selectedVideos.map { ("video", $0) }
                guard resources.isEmpty == false else {
                    let lastVideoURIs = self.lastRecordedVideoURIs()
                    if lastVideoURIs.isEmpty == false {
                        self.appendLog("文件列表暂未返回资源，改用刚结束录像的 URI 下载 \(lastVideoURIs.count) 个视频")
                        lastVideoURIs.forEach { uri in
                            self.download(
                                uri: uri,
                                kind: "video",
                                commandManager: commandManager,
                                cameraID: cameraID,
                                cameraName: cameraName
                            )
                        }
                        return
                    }

                    self.appendLog("没有找到可下载的相机资源")
                    return
                }

                self.appendLog("正在下载最新的 \(resources.count) 个资源")
                resources.forEach { kind, resource in
                    self.download(
                        resource: resource,
                        kind: kind,
                        commandManager: commandManager,
                        cameraID: cameraID,
                        cameraName: cameraName
                    )
                }
            }
        }
    }

    func parseDownloadedResources() {
        appendLog("开始后台提取本机 IMU 数据")
        dataProcessingQueue.async { [weak self] in
            self?.performParseDownloadedResources()
        }
    }

    private func performParseDownloadedResources() {
        do {
            let contents = try FileManager.default.contentsOfDirectory(
                at: workingDirectory,
                includingPropertiesForKeys: [.isDirectoryKey],
                options: [.skipsHiddenFiles]
            )

            for dir in contents {
                let isDir = (try? dir.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) ?? false
                if isDir && dir.lastPathComponent != "sessions" && dir.lastPathComponent != "timestamp_logs" {
                    let urls = try FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil, options: [.skipsHiddenFiles])
                    let mediaURLs = urls.filter { url in
                        let ext = url.pathExtension.lowercased()
                        return isGeneratedPanorama(url) == false
                            && ["insp", "jpg", "jpeg", "dng", "insv", "mp4", "mov", "lrv"].contains(ext)
                    }

                    mediaURLs.forEach { url in
                        do {
                            let sidecarURL = try MultiCameraMediaParser.parseAndWriteSidecar(
                                for: url,
                                outputDirectory: dir
                            )
                            appendLog("已写入伴随数据：\(sidecarURL.lastPathComponent)")
                        } catch {
                            appendLog("解析失败 \(url.lastPathComponent)：\(error.localizedDescription)")
                        }
                    }
                }
            }
            loadInspectionItems()
            appendLog("本机 IMU 数据提取完成")
        } catch {
            appendLog("读取下载目录失败：\(error.localizedDescription)")
        }
    }

    func exportDownloadedPanoramas(width: Int = 4096, height: Int = 2048) {
        appendLog("开始后台导出全景预览")
        dataProcessingQueue.async { [weak self] in
            self?.performExportDownloadedPanoramas(width: width, height: height)
        }
    }

    private func performExportDownloadedPanoramas(width: Int, height: Int) {
        do {
            let contents = try FileManager.default.contentsOfDirectory(
                at: workingDirectory,
                includingPropertiesForKeys: [.isDirectoryKey],
                options: [.skipsHiddenFiles]
            )

            for dir in contents {
                let isDir = (try? dir.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) ?? false
                if isDir && dir.lastPathComponent != "sessions" && dir.lastPathComponent != "timestamp_logs" {
                    let urls = try FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil, options: [.skipsHiddenFiles])

                    let imageURLs = urls.filter {
                        isGeneratedPanorama($0) == false
                            && ["insp", "jpg", "jpeg"].contains($0.pathExtension.lowercased())
                    }
                    let videoURLs = urls.filter {
                        isGeneratedPanorama($0) == false
                            && ["insv", "mp4", "mov"].contains($0.pathExtension.lowercased())
                    }

                    imageURLs.forEach { inputURL in
                        let outputURL = dir
                            .appendingPathComponent(inputURL.deletingPathExtension().lastPathComponent)
                            .appendingPathExtension("pano.jpg")
                        let exporter = INSExportImageSimplify()
                        exporter.width = Int32(width)
                        exporter.height = Int32(height)
                        if let error = exporter.exportImage(withInputUrl: inputURL, outputUrl: outputURL) {
                            appendLog("图片全景导出失败：\(error.localizedDescription)")
                        }
                        DispatchQueue.main.async {
                            self.activeImageExporters.append(exporter)
                        }
                        appendLog("已开始导出图片全景：\(outputURL.lastPathComponent)")
                    }

                    videoURLs.forEach { inputURL in
                        let outputURL = dir
                            .appendingPathComponent(inputURL.deletingPathExtension().lastPathComponent)
                            .appendingPathExtension("pano.mp4")
                        let exporter = INSExportSimplify(urls: [inputURL], outputUrl: outputURL)
                        exporter.width = Int32(width)
                        exporter.height = Int32(height)
                        let delegate = MultiCameraPanoramaExportDelegate(
                            coordinator: self,
                            exporter: exporter,
                            outputURL: outputURL
                        )
                        exporter.exportManagedelegate = delegate
                        if let error = exporter.start() {
                            appendLog("视频全景导出失败：\(error.localizedDescription)")
                            return
                        }
                        DispatchQueue.main.async {
                            self.activeVideoExporters.append(exporter)
                        }
                        appendLog("已开始导出视频全景：\(outputURL.lastPathComponent)")
                    }
                }
            }
            loadInspectionItems()
            scheduleInspectionRefresh(after: 2.0)
            scheduleInspectionRefresh(after: 6.0)
        } catch {
            appendLog("导出准备失败：\(error.localizedDescription)")
        }
    }

    private func isGeneratedPanorama(_ url: URL) -> Bool {
        url.deletingPathExtension().lastPathComponent.hasSuffix(".pano")
    }

    fileprivate func videoPanoramaExportDidProgress(outputURL: URL, progress: Float) {
        let percent = Int((progress * 100).rounded())
        guard percent == 25 || percent == 50 || percent == 75 else {
            return
        }
        appendLog("视频全景导出进度 \(percent)%：\(outputURL.lastPathComponent)")
    }

    fileprivate func videoPanoramaExportDidFinish(exporter: INSExportSimplify?, outputURL: URL, state: INSExporter2State, error: Error?) {
        if let exporter = exporter {
            activeVideoExporters.removeAll { $0 === exporter }
        }

        switch state {
        case .complete:
            appendLog("视频全景导出完成：\(outputURL.lastPathComponent)")
            loadInspectionItems()
        case .cancel:
            appendLog("视频全景导出已取消：\(outputURL.lastPathComponent)")
        default:
            let message = error?.localizedDescription ?? "状态 \(state.rawValue)"
            appendLog("视频全景导出未完成：\(outputURL.lastPathComponent)，\(message)")
        }
    }

    private func scheduleInspectionRefresh(after delay: TimeInterval) {
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            self?.loadInspectionItems()
        }
    }

    func writeEventLog() {
        do {
            try FileManager.default.createDirectory(
                at: eventLogURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            let data = try encoder.encode(captureEvents)
            try data.write(to: eventLogURL, options: .atomic)
            appendLog("已写入事件日志：\(eventLogURL.path)")
        } catch {
            appendLog("事件日志写入失败：\(error.localizedDescription)")
        }
    }

    func deviceDidConnected(_ device: INSBluetoothDevice) {
        if let record = devices.first(where: { $0.id == device.identifierUUIDStringSafe }) {
            let wasConnected = (record.state == .connected)
            record.state = .connected
            record.commandManager = bluetoothManager.command(by: device)

            if !wasConnected {
                appendLog("\(record.name) 蓝牙已连接")
                fetchParameters(for: record)
            }
        }
        notifyDevicesChanged()
    }

    private func fetchParameters(for record: MultiCameraDeviceRecord) {
        guard let commandManager = record.commandManager else { return }
        record.serialNumber = record.bluetoothDevice?.serialNumber ?? "未知"

        // 1: INSCameraOptionsTypeVideoResolution, 11: INSCameraOptionsTypeBatteryStatus
        let optionTypes: [NSNumber] = [1, 11]

        commandManager.getOptionsWithTypes(optionTypes) { [weak self, weak record] error, options, successTypes in
            DispatchQueue.main.async {
                guard let record = record else { return }

                if let options = options {
                    let res = options.videoResolution
                    record.resolutionStr = "\(res.width)x\(res.height)"
                    record.fps = Int(res.fps)

                    if res.width >= 5760 {
                        record.lensModeStr = "全景模式"
                    } else if res.width >= 3840 {
                        record.lensModeStr = "单视角"
                    } else {
                        record.lensModeStr = "未知视角"
                    }

                    if let battery = options.batteryStatus {
                        record.batteryLevel = Int(battery.batteryLevel)
                    }
                } else {
                    self?.appendLog("\(record.name) 获取参数失败: \(self?.describe(error) ?? "未知错误")")
                }
                self?.notifyDevicesChanged()
            }
        }
    }

    func device(_ device: INSBluetoothDevice, didDisconnectWithError error: Error?) {
        if let record = devices.first(where: { $0.id == device.identifierUUIDStringSafe }) {
            record.state = .disconnected
            record.commandManager = nil
            record.lastError = error?.localizedDescription
            lastMultiCameraSyncUnixTimeMs = nil
            appendLog("\(record.name) 蓝牙已断开 \(describe(error))")
        }
        notifyDevicesChanged()
    }

    private func connect(_ record: MultiCameraDeviceRecord) {
        guard let device = record.bluetoothDevice else {
            return
        }

        record.state = .connecting
        record.lastError = nil
        notifyDevicesChanged()

        bluetoothManager.connect(device) { [weak self, weak record] error in
            guard let self = self, let record = record else { return }
            DispatchQueue.main.async {
                if let error = error {
                    record.state = .failed
                    record.lastError = error.localizedDescription
                    self.appendLog("\(record.name) 蓝牙连接失败：\(error.localizedDescription)")
                } else {
                    record.state = .connected
                    record.commandManager = self.bluetoothManager.command(by: device)
                    self.appendLog("\(record.name) 蓝牙已连接")
                }
                if self.connectedCameraCount < 2 {
                    self.lastMultiCameraSyncUnixTimeMs = nil
                }
                self.notifyDevicesChanged()
            }
        }
    }

    private func upsert(device: INSBluetoothDevice, rssi: Int) {
        if let existing = devices.first(where: { $0.id == device.identifierUUIDStringSafe }) {
            existing.rssi = rssi
            existing.bluetoothDevice = device
        } else {
            devices.append(MultiCameraDeviceRecord(device: device, rssi: rssi))
            let deviceName = device.name.isEmpty ? "影石相机" : device.name
            appendLog("发现 \(deviceName)，信号 \(rssi)")
        }
        devices.sort { $0.rssi > $1.rssi }
        notifyDevicesChanged()
    }

    private func commandRecords() -> [(MultiCameraDeviceRecord, INSCameraBasicCommands)] {
        return devices.compactMap { record in
            guard record.state == .connected, let commandManager = record.commandManager else {
                return nil
            }
            return (record, commandManager)
        }
    }

    private func lastRecordedVideoURIs() -> [String] {
        var seen = Set<String>()
        return devices.compactMap { $0.lastVideoURI }.filter { uri in
            guard seen.contains(uri) == false else {
                return false
            }
            seen.insert(uri)
            return true
        }
    }

    private func download(
        resource: INSCameraBaseFileInfo,
        kind: String,
        commandManager: INSAllUSBCommands,
        cameraID: String,
        cameraName: String
    ) {
        download(
            uri: resource.uri,
            kind: kind,
            commandManager: commandManager,
            cameraID: cameraID,
            cameraName: cameraName
        )
    }

    private func download(
        uri: String,
        kind: String,
        commandManager: INSAllUSBCommands,
        cameraID: String,
        cameraName: String
    ) {
        let cameraDir = workingDirectory.appendingPathComponent(cameraName)
        try? FileManager.default.createDirectory(at: cameraDir, withIntermediateDirectories: true)
        let outputURL = cameraDir.appendingPathComponent(safeFileName(for: uri, kind: kind))

        appendLog("正在下载 \(localizedKind(kind))：\(uri)")
        _ = commandManager.fetchResource(withURI: uri, toLocalFile: outputURL, progress: { _ in }) { [weak self] error in
            guard let self = self else { return }
            if let error = error {
                self.appendLog("下载失败 \(uri)：\(error.localizedDescription)")
                return
            }

            let downloaded = MultiCameraDownloadedResource(
                cameraID: cameraID,
                cameraName: cameraName,
                kind: kind,
                remoteURI: uri,
                localPath: outputURL.path,
                downloadedAtUnixTimeMs: MultiCameraClock.unixTimeMs()
            )
            DispatchQueue.main.async {
                self.downloadedResources.append(downloaded)
            }
            self.appendLog("已下载：\(outputURL.lastPathComponent)")

            do {
                let sidecarURL = try MultiCameraMediaParser.parseAndWriteSidecar(
                    for: outputURL,
                    outputDirectory: cameraDir
                )
                self.appendLog("已写入伴随数据：\(sidecarURL.lastPathComponent)")
            } catch {
                self.appendLog("伴随数据解析失败：\(error.localizedDescription)")
            }
            self.loadInspectionItems()
            self.refreshRemoteDownloadStates()
        }
    }

    private func writeRemoteMetadataSidecar(
        uri: String,
        kind: String,
        commandManager: INSCameraSimpleUSBCommands,
        cameraName: String
    ) {
        appendLog("正在读取相机 metadata：\(uri)")
        commandManager.getFileMnd(withURI: uri, type: .metadata) { [weak self] error, data in
            guard let self = self else { return }
            if let error = error {
                self.appendLog("metadata 读取失败 \(uri)：\(error.localizedDescription)")
                return
            }
            guard let data = data else {
                self.appendLog("metadata 读取失败 \(uri)：返回为空")
                return
            }

            guard let metadata = self.decodeExtraMetadata(from: data) else {
                self.appendLog("metadata 解析失败 \(uri)：无法反序列化 INSExtraMetadata")
                return
            }

            let sidecar = MultiCameraMediaSidecar(
                mediaPath: uri,
                mediaKind: "\(kind)_remote_metadata",
                cameraType: metadata.cameraType,
                serialNumber: metadata.serialNumber,
                firmwareVersion: metadata.fwVersion,
                createUnixTimeMs: self.unixMs(metadata.createDate),
                firstFrameTimestampMs: metadata.firstFrameTimestamp,
                firstGpsTimestampMs: metadata.firstGpsTimestamp,
                gyroTimestampAdjustMs: metadata.gyroTimestampAdjust,
                rollingShutterTimeMs: metadata.rollingShutterTime,
                fileTotalTimeMs: metadata.fileTotalTimeMs,
                fileSize: metadata.fileSize,
                width: Double(metadata.fileDimension.width),
                height: Double(metadata.fileDimension.height),
                gyro: nil
            )

            do {
                let sessionsDir = self.workingDirectory.appendingPathComponent("sessions", isDirectory: true)
                try FileManager.default.createDirectory(at: sessionsDir, withIntermediateDirectories: true)
                let safeName = self.safeFileName(for: uri, kind: kind)
                let baseName = (safeName as NSString).deletingPathExtension
                let outputURL = sessionsDir
                    .appendingPathComponent("\(baseName).sync")
                    .appendingPathExtension("json")
                let encoder = JSONEncoder()
                encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
                let payload = try encoder.encode(sidecar)
                try payload.write(to: outputURL, options: .atomic)
                self.appendLog(
                    "已写入相机 metadata sidecar：\(outputURL.lastPathComponent)，firstGps=\(metadata.firstGpsTimestamp)"
                )
            } catch {
                self.appendLog("metadata sidecar 写入失败 \(uri)：\(error.localizedDescription)")
            }
        }
    }

    private func decodeExtraMetadata(from data: Data) -> INSExtraMetadata? {
        let metadataClass: AnyObject = INSExtraMetadata.self
        for selectorName in ["metadataWithPBData:", "metadataWithJSONData:"] {
            let selector = NSSelectorFromString(selectorName)
            guard metadataClass.responds(to: selector),
                  let result = metadataClass.perform(selector, with: data as NSData)?
                    .takeUnretainedValue() as? INSExtraMetadata else {
                continue
            }
            return result
        }
        return nil
    }

    func loadInspectionItems() {
        do {
            ensureWorkingDirectories()

            var mediaURLs: [URL] = []

            let contents = try FileManager.default.contentsOfDirectory(
                at: workingDirectory,
                includingPropertiesForKeys: [.isDirectoryKey],
                options: [.skipsHiddenFiles]
            )

            for dir in contents {
                let isDir = (try? dir.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) ?? false
                if isDir && dir.lastPathComponent != "sessions" && dir.lastPathComponent != "timestamp_logs" {
                    if let files = try? FileManager.default.contentsOfDirectory(
                        at: dir,
                        includingPropertiesForKeys: [.fileSizeKey, .contentModificationDateKey],
                        options: [.skipsHiddenFiles]
                    ) {
                        mediaURLs.append(contentsOf: files.filter { url in
                            let baseName = url.deletingPathExtension().lastPathComponent
                            return baseName.hasSuffix(".pano") == false
                                && ["insp", "jpg", "jpeg", "dng", "insv", "mp4", "mov", "lrv"].contains(url.pathExtension.lowercased())
                        })
                    }
                }
            }

            let sortedMediaURLs = mediaURLs.sorted { lhs, rhs in
                let lhsDate = (try? lhs.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
                let rhsDate = (try? rhs.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
                return lhsDate > rhsDate
            }

            let items = sortedMediaURLs.map { makeInspectionItem(for: $0) }
            DispatchQueue.main.async {
                self.inspectionItems = items
            }
        } catch {
            appendLog("读取数据检查目录失败：\(error.localizedDescription)")
            DispatchQueue.main.async {
                self.inspectionItems = []
            }
        }
    }

    @discardableResult
    func deleteInspectionItem(_ item: MultiCameraInspectionItem) -> String {
        var deletedCount = 0
        var failedMessages: [String] = []

        var urlsToDelete = item.relatedURLs

        // Also clean up log files
        let logsDir = workingDirectory.appendingPathComponent("timestamp_logs")
        if let files = try? FileManager.default.contentsOfDirectory(at: logsDir, includingPropertiesForKeys: nil) {
            urlsToDelete.append(contentsOf: files)
        }
        let sessionsDir = workingDirectory.appendingPathComponent("sessions")
        if let files = try? FileManager.default.contentsOfDirectory(at: sessionsDir, includingPropertiesForKeys: nil) {
            urlsToDelete.append(contentsOf: files)
        }

        urlsToDelete.forEach { url in
            guard isURLInsideWorkingDirectory(url) else {
                failedMessages.append("跳过非工作目录文件：\(url.lastPathComponent)")
                return
            }

            guard FileManager.default.fileExists(atPath: url.path) else {
                return
            }

            do {
                try FileManager.default.removeItem(at: url)
                deletedCount += 1
            } catch {
                failedMessages.append("\(url.lastPathComponent)：\(error.localizedDescription)")
            }
        }

        loadInspectionItems()

        if failedMessages.isEmpty {
            let message = "已删除 \(deletedCount) 个文件：\(item.fileName)"
            appendLog(message)
            return message
        }

        let message = "部分删除完成，失败：\(failedMessages.joined(separator: "；"))"
        appendLog(message)
        return message
    }

    private func makeInspectionItem(for mediaURL: URL) -> MultiCameraInspectionItem {
        let baseName = mediaURL.deletingPathExtension().lastPathComponent
        let dir = mediaURL.deletingLastPathComponent()
        let sidecarURL = dir
            .appendingPathComponent(baseName)
            .appendingPathExtension("sync.json")
        let gyroURL = dir
            .appendingPathComponent(baseName)
            .appendingPathExtension("gyro.bin")
        let secondaryGyroURL = dir
            .appendingPathComponent(baseName)
            .appendingPathExtension("gyro2.bin")
        let panoramaURL = findPanoramaURL(forBaseName: baseName, in: dir)
        let sidecar = decodeSidecar(at: sidecarURL)
        let values = try? mediaURL.resourceValues(forKeys: [.fileSizeKey, .contentModificationDateKey])

        return MultiCameraInspectionItem(
            id: mediaURL.path,
            mediaURL: mediaURL,
            sidecarURL: FileManager.default.fileExists(atPath: sidecarURL.path) ? sidecarURL : nil,
            panoramaURL: panoramaURL,
            gyroURL: FileManager.default.fileExists(atPath: gyroURL.path) ? gyroURL : nil,
            secondaryGyroURL: FileManager.default.fileExists(atPath: secondaryGyroURL.path) ? secondaryGyroURL : nil,
            sidecar: sidecar,
            fileSizeBytes: UInt64(values?.fileSize ?? 0),
            modifiedAt: values?.contentModificationDate
        )
    }

    private func findPanoramaURL(forBaseName baseName: String, in directory: URL) -> URL? {
        guard let urls = try? FileManager.default.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        ) else {
            return nil
        }

        return urls.first { url in
            url.deletingPathExtension().lastPathComponent == "\(baseName).pano"
        }
    }

    private func decodeSidecar(at url: URL) -> MultiCameraMediaSidecar? {
        guard FileManager.default.fileExists(atPath: url.path),
              let data = try? Data(contentsOf: url) else {
            return nil
        }
        return try? JSONDecoder().decode(MultiCameraMediaSidecar.self, from: data)
    }

    private func isURLInsideWorkingDirectory(_ url: URL) -> Bool {
        let rootPath = workingDirectory.standardizedFileURL.path
        let path = url.standardizedFileURL.path
        return path == rootPath || path.hasPrefix(rootPath + "/")
    }

    @objc private func applicationDidBecomeActive() {
        let state = INSCameraManager.socket().cameraState
        refreshWiFiSocketStatus()

        if state == .connected {
            shouldRefreshWiFiSocketWhenActive = false
            return
        }

        if shouldRefreshWiFiSocketWhenActive || state != .connected {
            appendLog("App 回到前台，尝试挂载 Wi-Fi Socket")
            resetAndSetupWiFiSocket(reason: "App 回到前台")
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
            guard let self = self else { return }
            let refreshedState = INSCameraManager.socket().cameraState
            if refreshedState == .connected {
                self.shouldRefreshWiFiSocketWhenActive = false
            }
            self.refreshWiFiSocketStatus()
            self.appendLog("回到前台后 Socket 状态：\(self.describeCameraState(refreshedState))")
        }
    }

    private func appendCommandEvent(
        record: MultiCameraDeviceRecord,
        command: String,
        stage: String,
        uri: String?,
        error: Error?
    ) {
        let clock = MultiCameraClock.sample("\(command).\(stage)")
        let event = MultiCameraCaptureEvent(
            eventID: UUID().uuidString,
            cameraID: record.id,
            cameraName: record.name,
            captureMode: captureMode.displayName,
            connectedCameraCount: connectedCameraCount,
            requiresClockSync: captureMode.requiresClockSync,
            command: command,
            stage: stage,
            localUnixTimeMs: clock.unixTimeMs,
            monotonicTime: clock.monotonicTime,
            cameraURI: uri,
            errorDescription: error?.localizedDescription
        )
        captureEvents.append(event)
        writeEventLog()
        commandEventHandler?(event)
    }

    private func appendLog(_ message: String) {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss.SSS"
        let line = "\(formatter.string(from: Date())) \(message)"
        let deliver = {
            print(line)
            self.delegate?.multiCameraCoordinator(self, didAppendLog: line)
        }

        if Thread.isMainThread {
            deliver()
        } else {
            DispatchQueue.main.async(execute: deliver)
        }
    }

    private func notifyDevicesChanged() {
        DispatchQueue.main.async {
            self.connectedCount = self.connectedCameraCount
            self.isConnected = self.connectedCount > 0
        }
        delegate?.multiCameraCoordinatorDidUpdateDevices(self)
    }

    private func ensureWorkingDirectories() {
        [workingDirectory, eventLogURL.deletingLastPathComponent()].forEach { url in
            try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        }
    }

    private func unixMs(_ date: Date?) -> Int64? {
        guard let date = date else {
            return nil
        }
        return Int64(date.timeIntervalSince1970 * 1000)
    }

    private func appendCaptureModeLog(for command: String, targetCount: Int) {
        if targetCount == 1 {
            appendLog("单台 X5 模式：\(command) 不需要时钟对齐")
        } else if lastMultiCameraSyncUnixTimeMs == nil {
            appendLog("多台 X5 模式：\(command) 将在本次会话未完成时钟同步的情况下执行")
        } else {
            appendLog("多台 X5 模式：\(command) 使用最近一次完成的时钟同步")
        }
    }

    private func safeFileName(for uri: String, kind: String) -> String {
        let lastPath = URL(string: uri)?.lastPathComponent ?? uri.components(separatedBy: "/").last ?? UUID().uuidString
        let fallbackExtension = kind == "video" ? "insv" : "insp"
        let candidate = lastPath.isEmpty ? "\(UUID().uuidString).\(fallbackExtension)" : lastPath
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "._-"))
        let scalars = candidate.unicodeScalars.map { allowed.contains($0) ? String($0) : "_" }
        return scalars.joined()
    }

    private func describe(_ error: Error?) -> String {
        guard let error = error else {
            return "成功"
        }
        return "错误：\(error.localizedDescription)"
    }

    private func describeWifiConnectionResult(_ result: INSWifiConnectionResult) -> String {
        switch result.rawValue {
        case -1:
            return "App 等待超时"
        case 0:
            return "已连接且网络可用"
        case 1:
            return "已连接但公网检测超时"
        case 2:
            return "连接失败"
        case 3:
            return "密码错误"
        case 4:
            return "无法分配 IP"
        case 5:
            return "无密码 Wi-Fi 已连接"
        case 6:
            return "未扫描到 Wi-Fi"
        case 7:
            return "路由器限制或干扰"
        case 8:
            return "信号弱"
        case 9:
            return "不支持相机自身热点"
        case 10:
            return "加密方式不支持"
        case 11:
            return "当前信道不支持"
        default:
            return "未知返回码 \(result.rawValue)"
        }
    }

    private func describeCameraState(_ state: INSCameraState) -> String {
        switch state {
        case .connected:
            return "已连接"
        case .found:
            return "已发现"
        case .synchronized:
            return "已同步"
        case .connectFailed:
            return "连接失败"
        case .noConnection:
            return "未连接"
        @unknown default:
            return "未知 \(state.rawValue)"
        }
    }

    private func localizedKind(_ kind: String) -> String {
        switch kind {
        case "photo":
            return "照片"
        case "video":
            return "视频"
        default:
            return kind
        }
    }
}

private final class MultiCameraPanoramaExportDelegate: NSObject, INSRExporter2ManagerDelegate {
    private weak var coordinator: MultiCameraCoordinator?
    private weak var exporter: INSExportSimplify?
    private let outputURL: URL
    private var reportedProgressBuckets = Set<Int>()

    init(coordinator: MultiCameraCoordinator, exporter: INSExportSimplify, outputURL: URL) {
        self.coordinator = coordinator
        self.exporter = exporter
        self.outputURL = outputURL
        super.init()
    }

    func exporter2Manager(_ manager: INSExporter2Manager, progress: Float) {
        let bucket = Int((progress * 4).rounded()) * 25
        guard [25, 50, 75].contains(bucket), reportedProgressBuckets.insert(bucket).inserted else {
            return
        }
        coordinator?.videoPanoramaExportDidProgress(outputURL: outputURL, progress: Float(bucket) / 100.0)
    }

    func exporter2Manager(_ manager: INSExporter2Manager, state: INSExporter2State, error: Error?) {
        coordinator?.videoPanoramaExportDidFinish(exporter: exporter, outputURL: outputURL, state: state, error: error)
    }

    func exporter2Manager(_ manager: INSExporter2Manager, correctOffset: String, errorNum: Int32, totalNum: Int32, clipIndex: Int32, type: String) {
    }
}
