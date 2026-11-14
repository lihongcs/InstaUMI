import Foundation
import Combine
import INSCameraServiceSDK

struct SyncTransferLog: Identifiable {
    let id = UUID()
    let timestamp: Date
    let fileName: String
    let detail: String
    let isError: Bool
}

final class SyncManager: ObservableObject {
    @Published var isSyncing: Bool = false
    @Published var lastSyncStatus: String = "未开始"
    @Published var transferLogs: [SyncTransferLog] = []

    var currentTaskName: String = "默认任务"

    // Using UserDefaults for simple persistence of credentials
    @Published var routerSSID: String {
        didSet { UserDefaults.standard.set(routerSSID, forKey: "sync_routerSSID") }
    }
    @Published var routerPassword: String {
        didSet { UserDefaults.standard.set(routerPassword, forKey: "sync_routerPassword") }
    }
    @Published var serverIP: String {
        didSet { UserDefaults.standard.set(serverIP, forKey: "sync_serverIP") }
    }
    @Published var serverPort: String {
        didSet { UserDefaults.standard.set(serverPort, forKey: "sync_serverPort") }
    }
    @Published var username: String {
        didSet { UserDefaults.standard.set(username, forKey: "sync_username") }
    }
    @Published var password: String {
        didSet { UserDefaults.standard.set(password, forKey: "sync_password") }
    }
    @Published var uploadPath: String {
        didSet { UserDefaults.standard.set(uploadPath, forKey: "sync_uploadPath") }
    }

    init() {
        self.routerSSID = UserDefaults.standard.string(forKey: "sync_routerSSID") ?? ""
        self.routerPassword = UserDefaults.standard.string(forKey: "sync_routerPassword") ?? ""
        self.serverIP = UserDefaults.standard.string(forKey: "sync_serverIP") ?? ""
        self.serverPort = UserDefaults.standard.string(forKey: "sync_serverPort") ?? "8000"
        self.username = UserDefaults.standard.string(forKey: "sync_username") ?? "admin"
        self.password = UserDefaults.standard.string(forKey: "sync_password") ?? ""
        self.uploadPath = UserDefaults.standard.string(forKey: "sync_uploadPath") ?? "/upload_file"
    }

    func clearTransferLogs() {
        transferLogs.removeAll()
        lastSyncStatus = "记录已清空"
    }

    func syncFiles(_ urls: [URL]) {
        guard serverIP.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false else {
            lastSyncStatus = "错误：服务器 IP 不能为空"
            appendTransferLog(fileName: "服务器配置", detail: lastSyncStatus, isError: true)
            return
        }

        guard let uploadURL = makeUploadURL() else {
            lastSyncStatus = "错误：服务器地址无效"
            appendTransferLog(fileName: "服务器配置", detail: lastSyncStatus, isError: true)
            return
        }

        var allURLs = urls
        allURLs.append(contentsOf: getLogURLs())

        let uniqueURLs = uniqueExistingURLs(from: allURLs)
        guard uniqueURLs.isEmpty == false else {
            lastSyncStatus = "错误：请先选择需要传输的数据"
            appendTransferLog(fileName: "传输队列", detail: lastSyncStatus, isError: true)
            return
        }

        isSyncing = true
        lastSyncStatus = "准备传输 \(uniqueURLs.count) 个文件"
        appendTransferLog(fileName: "传输队列", detail: "已创建 \(uniqueURLs.count) 个文件的同步任务", isError: false)
        uploadNext(urls: uniqueURLs, uploadURL: uploadURL, index: 0, successCount: 0)
    }

    func relayUploadCameraResources(_ resources: [MultiCameraRemoteResource]) {
        guard serverIP.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty == false else {
            lastSyncStatus = "错误：服务器 IP 不能为空"
            appendTransferLog(fileName: "服务器配置", detail: lastSyncStatus, isError: true)
            return
        }

        guard let uploadURL = makeUploadURL() else {
            lastSyncStatus = "错误：服务器地址无效"
            appendTransferLog(fileName: "服务器配置", detail: lastSyncStatus, isError: true)
            return
        }

        let uniqueResources = uniqueRemoteResources(resources)
        guard uniqueResources.isEmpty == false else {
            lastSyncStatus = "错误：请先选择相机上的数据记录"
            appendTransferLog(fileName: "直传队列", detail: lastSyncStatus, isError: true)
            return
        }

        isSyncing = true
        lastSyncStatus = "准备直传 \(uniqueResources.count) 条相机记录"
        appendTransferLog(fileName: "直传队列", detail: "相机数据不会保存到本机数据目录", isError: false)
        relayUploadNext(resources: uniqueResources, uploadURL: uploadURL, index: 0, successCount: 0)
    }

    func syncLatestRecord() {
        guard !serverIP.isEmpty else {
            lastSyncStatus = "错误：IP 不能为空"
            return
        }

        isSyncing = true
        lastSyncStatus = "正在打包记录..."

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }

            // Find latest session file
            let docsUrl = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            let sessionsUrl = docsUrl.appendingPathComponent("instaumi").appendingPathComponent("sessions")

            var targetPayload: Data? = nil
            var targetFileName: String = "unknown_session.json"

            do {
                if FileManager.default.fileExists(atPath: sessionsUrl.path) {
                    let files = try FileManager.default.contentsOfDirectory(at: sessionsUrl, includingPropertiesForKeys: nil)
                    if let latest = files.filter({ $0.pathExtension == "json" }).sorted(by: { $0.lastPathComponent > $1.lastPathComponent }).first {
                        targetPayload = try Data(contentsOf: latest)
                        targetFileName = latest.lastPathComponent
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    self.lastSyncStatus = "错误：读取本地记录失败"
                    self.isSyncing = false
                }
                return
            }

            guard let payload = targetPayload else {
                DispatchQueue.main.async {
                    self.lastSyncStatus = "错误：没有找到任何采集记录"
                    self.isSyncing = false
                }
                return
            }

            // Construct HTTP request
            let urlString = "http://\(self.serverIP):\(self.serverPort)/upload_record"
            guard let url = URL(string: urlString) else {
                DispatchQueue.main.async {
                    self.lastSyncStatus = "错误：无效的服务器地址"
                    self.isSyncing = false
                }
                return
            }

            var request = URLRequest(url: url)
            request.httpMethod = "POST"

            // Basic Auth
            let loginString = String(format: "%@:%@", self.username, self.password)
            if let loginData = loginString.data(using: .utf8) {
                let base64LoginString = loginData.base64EncodedString()
                request.setValue("Basic \(base64LoginString)", forHTTPHeaderField: "Authorization")
            }

            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.setValue(targetFileName, forHTTPHeaderField: "X-File-Name")
            request.httpBody = payload

            DispatchQueue.main.async {
                self.lastSyncStatus = "正在发送 \(targetFileName)..."
            }

            let task = URLSession.shared.dataTask(with: request) { data, response, error in
                DispatchQueue.main.async {
                    self.isSyncing = false
                    if let error = error {
                        self.lastSyncStatus = "失败：\(error.localizedDescription)"
                        return
                    }
                    if let httpResponse = response as? HTTPURLResponse {
                        if httpResponse.statusCode == 200 || httpResponse.statusCode == 201 {
                            self.lastSyncStatus = "✅ 成功：记录已同步"
                        } else {
                            self.lastSyncStatus = "失败：服务器返回 \(httpResponse.statusCode)"
                        }
                    }
                }
            }
            task.resume()
        }
    }

    private func getLogURLs() -> [URL] {
        let docsUrl = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let root = docsUrl.appendingPathComponent("instaumi")
        let sessionsUrl = root.appendingPathComponent("sessions")
        let timestampLogsUrl = root.appendingPathComponent("timestamp_logs")

        var logURLs: [URL] = []
        if let files = try? FileManager.default.contentsOfDirectory(at: sessionsUrl, includingPropertiesForKeys: nil) {
            logURLs.append(contentsOf: files.filter { $0.pathExtension == "json" })
        }
        if let files = try? FileManager.default.contentsOfDirectory(at: timestampLogsUrl, includingPropertiesForKeys: nil) {
            logURLs.append(contentsOf: files.filter { $0.pathExtension == "jsonl" })
        }
        return logURLs
    }

    private func uploadNext(urls: [URL], uploadURL: URL, index: Int, successCount: Int) {
        guard index < urls.count else {
            isSyncing = false
            lastSyncStatus = "完成：成功传输 \(successCount)/\(urls.count) 个文件"
            appendTransferLog(fileName: "传输完成", detail: lastSyncStatus, isError: successCount != urls.count)
            return
        }

        let fileURL = urls[index]
        let fileName = fileURL.lastPathComponent
        lastSyncStatus = "正在传输 \(index + 1)/\(urls.count)：\(fileName)"
        appendTransferLog(fileName: fileName, detail: "开始上传到 \(uploadURL.path)", isError: false)

        var request = URLRequest(url: uploadURL)
        request.httpMethod = "POST"
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        request.setValue(fileName, forHTTPHeaderField: "X-File-Name")
        request.setValue(relativePath(for: fileURL), forHTTPHeaderField: "X-Relative-Path")

        let loginString = String(format: "%@:%@", username, password)
        if let loginData = loginString.data(using: .utf8) {
            request.setValue("Basic \(loginData.base64EncodedString())", forHTTPHeaderField: "Authorization")
        }

        let task = URLSession.shared.uploadTask(with: request, fromFile: fileURL) { [weak self] _, response, error in
            DispatchQueue.main.async {
                guard let self = self else { return }
                if let error = error {
                    self.appendTransferLog(fileName: fileName, detail: "失败：\(error.localizedDescription)", isError: true)
                    self.uploadNext(urls: urls, uploadURL: uploadURL, index: index + 1, successCount: successCount)
                    return
                }

                if let httpResponse = response as? HTTPURLResponse {
                    if (200...299).contains(httpResponse.statusCode) {
                        self.appendTransferLog(fileName: fileName, detail: "成功：服务器返回 \(httpResponse.statusCode)", isError: false)
                        self.uploadNext(urls: urls, uploadURL: uploadURL, index: index + 1, successCount: successCount + 1)
                    } else {
                        self.appendTransferLog(fileName: fileName, detail: "失败：服务器返回 \(httpResponse.statusCode)", isError: true)
                        self.uploadNext(urls: urls, uploadURL: uploadURL, index: index + 1, successCount: successCount)
                    }
                } else {
                    self.appendTransferLog(fileName: fileName, detail: "失败：没有服务器响应", isError: true)
                    self.uploadNext(urls: urls, uploadURL: uploadURL, index: index + 1, successCount: successCount)
                }
            }
        }
        task.resume()
    }

    private func relayUploadNext(resources: [MultiCameraRemoteResource], uploadURL: URL, index: Int, successCount: Int) {
        guard index < resources.count else {
            isSyncing = false
            lastSyncStatus = "直传完成：成功 \(successCount)/\(resources.count) 条"
            appendTransferLog(fileName: "直传完成", detail: lastSyncStatus, isError: successCount != resources.count)
            return
        }

        let resource = resources[index]
        let cameraURL = INSHTTPURLForResourceURI(resource.uri)

        lastSyncStatus = "正在直传 \(index + 1)/\(resources.count)：\(resource.fileName)"
        appendTransferLog(fileName: resource.fileName, detail: "从相机读取临时数据，不写入本机数据目录", isError: false)

        let task = URLSession.shared.downloadTask(with: cameraURL) { [weak self] temporaryURL, _, error in
            guard let self = self else { return }

            if let error = error {
                DispatchQueue.main.async {
                    self.appendTransferLog(fileName: resource.fileName, detail: "读取相机失败：\(error.localizedDescription)", isError: true)
                    self.relayUploadNext(resources: resources, uploadURL: uploadURL, index: index + 1, successCount: successCount)
                }
                return
            }

            guard let temporaryURL = temporaryURL else {
                DispatchQueue.main.async {
                    self.appendTransferLog(fileName: resource.fileName, detail: "读取相机失败：没有临时文件", isError: true)
                    self.relayUploadNext(resources: resources, uploadURL: uploadURL, index: index + 1, successCount: successCount)
                }
                return
            }

            let relayURL = FileManager.default.temporaryDirectory
                .appendingPathComponent("instaumi-relay-\(UUID().uuidString)-\(self.safeTemporaryFileName(resource.fileName))")

            do {
                try FileManager.default.copyItem(at: temporaryURL, to: relayURL)
            } catch {
                DispatchQueue.main.async {
                    self.appendTransferLog(fileName: resource.fileName, detail: "创建直传临时文件失败：\(error.localizedDescription)", isError: true)
                    self.relayUploadNext(resources: resources, uploadURL: uploadURL, index: index + 1, successCount: successCount)
                }
                return
            }

            let sidecarURL = self.makeRelaySidecar(for: relayURL, resource: resource)
            self.uploadRelayFile(
                resource: resource,
                relayURL: relayURL,
                sidecarURL: sidecarURL,
                uploadURL: uploadURL,
                resources: resources,
                index: index,
                successCount: successCount
            )
        }
        task.resume()
    }

    private func uploadRelayFile(
        resource: MultiCameraRemoteResource,
        relayURL: URL,
        sidecarURL: URL?,
        uploadURL: URL,
        resources: [MultiCameraRemoteResource],
        index: Int,
        successCount: Int
    ) {
        var request = URLRequest(url: uploadURL)
        request.httpMethod = "POST"
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        request.setValue(resource.fileName, forHTTPHeaderField: "X-File-Name")
        request.setValue(resource.uri, forHTTPHeaderField: "X-Remote-URI")
        request.setValue("camera-relay", forHTTPHeaderField: "X-Transfer-Mode")
        request.setValue("camera/\(resource.fileName)", forHTTPHeaderField: "X-Relative-Path")

        let loginString = String(format: "%@:%@", username, password)
        if let loginData = loginString.data(using: .utf8) {
            request.setValue("Basic \(loginData.base64EncodedString())", forHTTPHeaderField: "Authorization")
        }

        let task = URLSession.shared.uploadTask(with: request, fromFile: relayURL) { [weak self] _, response, error in
            try? FileManager.default.removeItem(at: relayURL)

            DispatchQueue.main.async {
                guard let self = self else { return }
                if let error = error {
                    self.appendTransferLog(fileName: resource.fileName, detail: "上传失败：\(error.localizedDescription)", isError: true)
                    self.relayUploadNext(resources: resources, uploadURL: uploadURL, index: index + 1, successCount: successCount)
                    return
                }

                guard let httpResponse = response as? HTTPURLResponse else {
                    self.appendTransferLog(fileName: resource.fileName, detail: "上传失败：没有服务器响应", isError: true)
                    self.relayUploadNext(resources: resources, uploadURL: uploadURL, index: index + 1, successCount: successCount)
                    return
                }

                if (200...299).contains(httpResponse.statusCode) {
                    self.appendTransferLog(fileName: resource.fileName, detail: "直传成功：服务器返回 \(httpResponse.statusCode)", isError: false)
                    if let sidecarURL {
                        self.uploadRelaySidecarFile(
                            resource: resource,
                            sidecarURL: sidecarURL,
                            uploadURL: uploadURL,
                            resources: resources,
                            index: index,
                            successCount: successCount + 1
                        )
                    } else {
                        self.relayUploadNext(resources: resources, uploadURL: uploadURL, index: index + 1, successCount: successCount + 1)
                    }
                } else {
                    self.appendTransferLog(fileName: resource.fileName, detail: "上传失败：服务器返回 \(httpResponse.statusCode)", isError: true)
                    if let sidecarURL {
                        try? FileManager.default.removeItem(at: sidecarURL)
                    }
                    self.relayUploadNext(resources: resources, uploadURL: uploadURL, index: index + 1, successCount: successCount)
                }
            }
        }
        task.resume()
    }

    private func makeRelaySidecar(for relayURL: URL, resource: MultiCameraRemoteResource) -> URL? {
        do {
            let parsedURL = try MultiCameraMediaParser.parseAndWriteSidecar(
                for: relayURL,
                outputDirectory: FileManager.default.temporaryDirectory
            )
            let canonicalURL = FileManager.default.temporaryDirectory
                .appendingPathComponent(resource.fileName)
                .deletingPathExtension()
                .appendingPathExtension("sync.json")
            if parsedURL != canonicalURL {
                try? FileManager.default.removeItem(at: canonicalURL)
                try FileManager.default.moveItem(at: parsedURL, to: canonicalURL)
            }
            return canonicalURL
        } catch {
            DispatchQueue.main.async {
                self.appendTransferLog(
                    fileName: resource.fileName,
                    detail: "伴随数据解析失败：\(error.localizedDescription)",
                    isError: true
                )
            }
            return nil
        }
    }

    private func uploadRelaySidecarFile(
        resource: MultiCameraRemoteResource,
        sidecarURL: URL,
        uploadURL: URL,
        resources: [MultiCameraRemoteResource],
        index: Int,
        successCount: Int
    ) {
        let sidecarFileName = sidecarURL.lastPathComponent
        var request = URLRequest(url: uploadURL)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(sidecarFileName, forHTTPHeaderField: "X-File-Name")
        request.setValue(resource.uri, forHTTPHeaderField: "X-Remote-URI")
        request.setValue("camera-relay-sidecar", forHTTPHeaderField: "X-Transfer-Mode")
        request.setValue("camera/\(sidecarFileName)", forHTTPHeaderField: "X-Relative-Path")

        let loginString = String(format: "%@:%@", username, password)
        if let loginData = loginString.data(using: .utf8) {
            request.setValue("Basic \(loginData.base64EncodedString())", forHTTPHeaderField: "Authorization")
        }

        let task = URLSession.shared.uploadTask(with: request, fromFile: sidecarURL) { [weak self] _, response, error in
            try? FileManager.default.removeItem(at: sidecarURL)

            DispatchQueue.main.async {
                guard let self = self else { return }
                if let error = error {
                    self.appendTransferLog(fileName: sidecarFileName, detail: "伴随数据上传失败：\(error.localizedDescription)", isError: true)
                    self.relayUploadNext(resources: resources, uploadURL: uploadURL, index: index + 1, successCount: successCount)
                    return
                }

                guard let httpResponse = response as? HTTPURLResponse else {
                    self.appendTransferLog(fileName: sidecarFileName, detail: "伴随数据上传失败：没有服务器响应", isError: true)
                    self.relayUploadNext(resources: resources, uploadURL: uploadURL, index: index + 1, successCount: successCount)
                    return
                }

                if (200...299).contains(httpResponse.statusCode) {
                    self.appendTransferLog(fileName: sidecarFileName, detail: "伴随数据上传成功：服务器返回 \(httpResponse.statusCode)", isError: false)
                } else {
                    self.appendTransferLog(fileName: sidecarFileName, detail: "伴随数据上传失败：服务器返回 \(httpResponse.statusCode)", isError: true)
                }
                self.relayUploadNext(resources: resources, uploadURL: uploadURL, index: index + 1, successCount: successCount)
            }
        }
        task.resume()
    }

    private func makeUploadURL() -> URL? {
        let normalizedPath = uploadPath.hasPrefix("/") ? uploadPath : "/\(uploadPath)"
        return URL(string: "http://\(serverIP):\(serverPort)\(normalizedPath)")
    }

    private func uniqueExistingURLs(from urls: [URL]) -> [URL] {
        var seen = Set<String>()
        return urls.filter { url in
            let path = url.standardizedFileURL.path
            guard FileManager.default.fileExists(atPath: path), seen.contains(path) == false else {
                return false
            }
            seen.insert(path)
            return true
        }
    }

    private func uniqueRemoteResources(_ resources: [MultiCameraRemoteResource]) -> [MultiCameraRemoteResource] {
        var seen = Set<String>()
        return resources.filter { resource in
            guard seen.contains(resource.id) == false else {
                return false
            }
            seen.insert(resource.id)
            return true
        }
    }

    private func relativePath(for url: URL) -> String {
        let path = url.standardizedFileURL.path
        if let range = path.range(of: "/Documents/") {
            var relPath = String(path[range.upperBound...])
            if relPath.hasPrefix("instaumi/") {
                relPath = relPath.replacingOccurrences(
                    of: "instaumi/",
                    with: "\(currentTaskName.isEmpty ? "未命名任务" : currentTaskName)/",
                    options: [],
                    range: relPath.startIndex..<relPath.index(relPath.startIndex, offsetBy: 9)
                )
            }
            return relPath
        }
        return url.lastPathComponent
    }

    private func safeTemporaryFileName(_ fileName: String) -> String {
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "._-"))
        let scalars = fileName.unicodeScalars.map { allowed.contains($0) ? String($0) : "_" }
        let safeName = scalars.joined()
        return safeName.isEmpty ? "camera-resource.bin" : safeName
    }

    private func appendTransferLog(fileName: String, detail: String, isError: Bool) {
        transferLogs.insert(
            SyncTransferLog(
                timestamp: Date(),
                fileName: fileName,
                detail: detail,
                isError: isError
            ),
            at: 0
        )
        if transferLogs.count > 80 {
            transferLogs = Array(transferLogs.prefix(80))
        }
    }
}
