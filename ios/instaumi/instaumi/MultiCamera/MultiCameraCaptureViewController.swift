import UIKit

final class MultiCameraCaptureViewController: UIViewController {
    private let coordinator = MultiCameraCoordinator.shared
    private let statusLabel = UILabel()
    private let tableView = UITableView(frame: .zero, style: .plain)
    private let logView = UITextView()
    private var syncTimeButton: UIButton?

    override func viewDidLoad() {
        super.viewDidLoad()
        title = "连接影石"
        view.backgroundColor = ConsoleColors.background
        coordinator.delegate = self
        setupNavigationBar()
        setupUI()
        updateStatus()
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        setupNavigationBar()
    }

    private func setupNavigationBar() {
        navigationController?.navigationBar.tintColor = ConsoleColors.accent
        navigationController?.navigationBar.backgroundColor = ConsoleColors.background
        navigationController?.navigationBar.prefersLargeTitles = false
    }

    private func setupUI() {
        statusLabel.font = .systemFont(ofSize: 15, weight: .semibold)
        statusLabel.textColor = ConsoleColors.secondaryText
        statusLabel.numberOfLines = 0

        tableView.dataSource = self
        tableView.delegate = self
        tableView.rowHeight = 78
        tableView.backgroundColor = .clear
        tableView.separatorColor = ConsoleColors.border
        tableView.tableFooterView = UIView()
        tableView.layer.cornerRadius = 8
        tableView.clipsToBounds = true

        logView.isEditable = false
        logView.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        logView.textColor = ConsoleColors.primaryText
        logView.backgroundColor = ConsoleColors.metricFill
        logView.layer.cornerRadius = 8
        logView.textContainerInset = UIEdgeInsets(top: 12, left: 10, bottom: 12, right: 10)
        let scrollView = UIScrollView()
        scrollView.alwaysBounceVertical = true
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(scrollView)

        let contentStack = UIStackView()
        contentStack.axis = .vertical
        contentStack.spacing = 14
        contentStack.translatesAutoresizingMaskIntoConstraints = false
        scrollView.addSubview(contentStack)

        let header = makeHeaderCard()
        let step1Card = makeCard(
            title: "蓝牙连通控制",
            subtitle: "用于低功耗唤醒设备和发送基础控制指令",
            arrangedSubviews: [
                buttonRow([
                    makeButton("扫描蓝牙", #selector(scanBLE), style: .primary),
                    makeButton("连接全部", #selector(connectAll), style: .primary),
                    makeButton("断开全部", #selector(disconnectAll), style: .neutral)
                ])
            ]
        )

        let devicesCard = makeCard(
            title: "设备列表",
            subtitle: "点击设备可连接或断开",
            arrangedSubviews: [tableView]
        )

        let logCard = makeCard(
            title: "操作日志",
            subtitle: "显示连接与设备控制日志",
            arrangedSubviews: [logView]
        )

        [header, step1Card, devicesCard, logCard].forEach {
            contentStack.addArrangedSubview($0)
        }

        NSLayoutConstraint.activate([
            scrollView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            scrollView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            scrollView.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor),

            contentStack.topAnchor.constraint(equalTo: scrollView.contentLayoutGuide.topAnchor, constant: 18),
            contentStack.leadingAnchor.constraint(equalTo: scrollView.frameLayoutGuide.leadingAnchor, constant: 22),
            contentStack.trailingAnchor.constraint(equalTo: scrollView.frameLayoutGuide.trailingAnchor, constant: -22),
            contentStack.bottomAnchor.constraint(equalTo: scrollView.contentLayoutGuide.bottomAnchor, constant: -22),

            tableView.heightAnchor.constraint(equalToConstant: 250),
            logView.heightAnchor.constraint(greaterThanOrEqualToConstant: 190)
        ])
    }

    private func makeHeaderCard() -> UIView {
        let iconView = UIImageView(image: UIImage(systemName: "camera.fill"))
        iconView.tintColor = .white
        iconView.contentMode = .center
        iconView.preferredSymbolConfiguration = UIImage.SymbolConfiguration(pointSize: 26, weight: .bold)
        iconView.backgroundColor = ConsoleColors.accent
        iconView.layer.cornerRadius = 8
        iconView.clipsToBounds = true
        iconView.translatesAutoresizingMaskIntoConstraints = false

        let titleLabel = UILabel()
        titleLabel.text = "连接控制台"
        titleLabel.font = .systemFont(ofSize: 30, weight: .heavy)
        titleLabel.textColor = ConsoleColors.primaryText
        titleLabel.adjustsFontSizeToFitWidth = true
        titleLabel.minimumScaleFactor = 0.7

        let subtitleLabel = UILabel()
        subtitleLabel.text = "保持与首页一致的采集工作台风格"
        subtitleLabel.font = .systemFont(ofSize: 15, weight: .semibold)
        subtitleLabel.textColor = ConsoleColors.secondaryText
        subtitleLabel.numberOfLines = 0

        let titleStack = UIStackView(arrangedSubviews: [titleLabel, subtitleLabel, statusLabel])
        titleStack.axis = .vertical
        titleStack.spacing = 6

        let row = UIStackView(arrangedSubviews: [iconView, titleStack])
        row.axis = .horizontal
        row.alignment = .top
        row.spacing = 16

        NSLayoutConstraint.activate([
            iconView.widthAnchor.constraint(equalToConstant: 64),
            iconView.heightAnchor.constraint(equalToConstant: 64)
        ])

        return makeCard(arrangedSubviews: [row])
    }

    private func makeCard(title: String? = nil, subtitle: String? = nil, arrangedSubviews: [UIView]) -> UIView {
        let card = UIView()
        card.backgroundColor = ConsoleColors.surface
        card.layer.cornerRadius = 8
        card.layer.borderWidth = 1
        card.layer.borderColor = ConsoleColors.border.cgColor

        let stack = UIStackView()
        stack.axis = .vertical
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(stack)

        if let title {
            let titleLabel = UILabel()
            titleLabel.text = title
            titleLabel.font = .systemFont(ofSize: 20, weight: .bold)
            titleLabel.textColor = ConsoleColors.primaryText
            stack.addArrangedSubview(titleLabel)
        }

        if let subtitle {
            let subtitleLabel = UILabel()
            subtitleLabel.text = subtitle
            subtitleLabel.font = .systemFont(ofSize: 13, weight: .semibold)
            subtitleLabel.textColor = ConsoleColors.secondaryText
            subtitleLabel.numberOfLines = 0
            stack.addArrangedSubview(subtitleLabel)
        }

        arrangedSubviews.forEach {
            stack.addArrangedSubview($0)
        }

        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: card.topAnchor, constant: 18),
            stack.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 18),
            stack.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -18),
            stack.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -18)
        ])

        return card
    }

    private enum ButtonStyle {
        case primary
        case secondary
        case neutral
    }

    private func makeButton(_ title: String, _ action: Selector, style: ButtonStyle) -> UIButton {
        let button = UIButton(type: .system)
        button.addTarget(self, action: action, for: .touchUpInside)

        var configuration = UIButton.Configuration.filled()
        configuration.title = title
        configuration.contentInsets = NSDirectionalEdgeInsets(top: 13, leading: 8, bottom: 13, trailing: 8)
        configuration.background.cornerRadius = 8
        configuration.titleTextAttributesTransformer = UIConfigurationTextAttributesTransformer { incoming in
            var outgoing = incoming
            outgoing.font = .systemFont(ofSize: 15, weight: .bold)
            return outgoing
        }

        switch style {
        case .primary:
            configuration.baseForegroundColor = .white
            configuration.baseBackgroundColor = ConsoleColors.accent
        case .secondary:
            configuration.baseForegroundColor = ConsoleColors.accent
            configuration.baseBackgroundColor = ConsoleColors.accentSoft
        case .neutral:
            configuration.baseForegroundColor = ConsoleColors.primaryText
            configuration.baseBackgroundColor = ConsoleColors.controlFill
        }

        button.configuration = configuration
        button.heightAnchor.constraint(equalToConstant: 48).isActive = true
        return button
    }

    private func buttonRow(_ buttons: [UIButton]) -> UIStackView {
        let row = UIStackView(arrangedSubviews: buttons)
        row.axis = .horizontal
        row.distribution = .fillEqually
        row.spacing = 10
        return row
    }

    private func updateStatus() {
        let mode = coordinator.captureMode
        let syncStatus: String
        if mode == .multiX5 {
            syncStatus = coordinator.hasCompletedMultiCameraSync ? "已同步" : "需要同步"
        } else {
            syncStatus = "无需同步"
        }

        statusLabel.text = "模式：\(mode.displayName)   已连接：\(coordinator.connectedCameraCount)   已下载：\(coordinator.downloadedResources.count)\n时钟：\(syncStatus)\n目录：\(coordinator.workingDirectory.path)"
        syncTimeButton?.isEnabled = mode.requiresClockSync
        syncTimeButton?.alpha = mode.requiresClockSync ? 1.0 : 0.45
    }

    private func appendLog(_ line: String) {
        let old = logView.text ?? ""
        logView.text = old.isEmpty ? line : old + "\n" + line
        let end = NSRange(location: max(logView.text.count - 1, 0), length: 1)
        logView.scrollRangeToVisible(end)
    }

    @objc private func scanBLE() {
        coordinator.scanBluetoothCameras()
    }

    @objc private func connectAll() {
        coordinator.connectAllBluetoothCameras()
    }

    @objc private func disconnectAll() {
        coordinator.disconnectAllBluetoothCameras()
    }

    @objc private func setupWiFiMode() {
        coordinator.setupWiFiSocketManager()
    }

    @objc private func openCameraWiFi() {
        coordinator.openCameraWiFiOnAll()
    }

    @objc private func syncTime() {
        coordinator.syncTimeOnAll()
    }

    @objc private func takePhoto() {
        coordinator.takePictureOnAll()
    }

    @objc private func startRecording() {
        coordinator.startRecordingOnAll()
    }

    @objc private func stopRecording() {
        coordinator.stopRecordingOnAll()
    }
}

extension MultiCameraCaptureViewController: UITableViewDataSource, UITableViewDelegate {
    func tableView(_ tableView: UITableView, numberOfRowsInSection section: Int) -> Int {
        return max(coordinator.devices.count, 1)
    }

    func tableView(_ tableView: UITableView, cellForRowAt indexPath: IndexPath) -> UITableViewCell {
        let identifier = "CameraCell"
        let cell = tableView.dequeueReusableCell(withIdentifier: identifier)
            ?? UITableViewCell(style: .subtitle, reuseIdentifier: identifier)

        cell.backgroundColor = ConsoleColors.surface
        cell.textLabel?.font = .systemFont(ofSize: 16, weight: .bold)
        cell.textLabel?.textColor = ConsoleColors.primaryText
        cell.detailTextLabel?.font = .systemFont(ofSize: 13, weight: .semibold)
        cell.detailTextLabel?.textColor = ConsoleColors.secondaryText
        cell.detailTextLabel?.numberOfLines = 2
        cell.selectedBackgroundView = {
            let view = UIView()
            view.backgroundColor = ConsoleColors.accentSoft
            return view
        }()

        guard coordinator.devices.isEmpty == false else {
            cell.textLabel?.text = "未发现蓝牙相机"
            cell.detailTextLabel?.text = "点击“扫描蓝牙”查找相机；发现后可点击设备单独连接，或点击“连接全部”。"
            cell.accessoryType = .none
            return cell
        }

        let record = coordinator.devices[indexPath.row]
        var details = ["状态：\(record.displayState)", "信号：\(record.rssi)"]
        if let lastPhotoURI = record.lastPhotoURI {
            details.append("照片：\(lastPhotoURI)")
        }
        if let lastVideoURI = record.lastVideoURI {
            details.append("视频：\(lastVideoURI)")
        }
        if let lastError = record.lastError {
            details.append("错误：\(lastError)")
        }

        cell.textLabel?.text = record.name
        cell.detailTextLabel?.text = details.joined(separator: "   ") + "\n点击执行操作 (如开启 Wi-Fi 或断开)"
        cell.accessoryType = record.state == .connected ? .checkmark : .none
        cell.tintColor = ConsoleColors.accent
        return cell
    }

    func tableView(_ tableView: UITableView, didSelectRowAt indexPath: IndexPath) {
        tableView.deselectRow(at: indexPath, animated: true)
        guard coordinator.devices.indices.contains(indexPath.row) else {
            return
        }

        let record = coordinator.devices[indexPath.row]
        if record.state == .connected || record.state == .connecting {
            let alert = UIAlertController(title: record.name, message: "请选择针对此相机的操作", preferredStyle: .actionSheet)

            alert.addAction(UIAlertAction(title: "开启此相机热点并打开系统 Wi-Fi", style: .default, handler: { [weak self] _ in
                self?.coordinator.openCameraWiFi(for: record)
            }))

            alert.addAction(UIAlertAction(title: "断开蓝牙连接", style: .destructive, handler: { [weak self] _ in
                self?.coordinator.disconnectBluetoothCamera(at: indexPath.row)
            }))

            alert.addAction(UIAlertAction(title: "取消", style: .cancel, handler: nil))

            if let popover = alert.popoverPresentationController {
                if let cell = tableView.cellForRow(at: indexPath) {
                    popover.sourceView = cell
                    popover.sourceRect = cell.bounds
                } else {
                    popover.sourceView = tableView
                    popover.sourceRect = tableView.bounds
                }
            }

            present(alert, animated: true, completion: nil)
        } else {
            coordinator.connectBluetoothCamera(at: indexPath.row)
        }
    }
}

extension MultiCameraCaptureViewController: MultiCameraCoordinatorDelegate {
    func multiCameraCoordinatorDidUpdateDevices(_ coordinator: MultiCameraCoordinator) {
        DispatchQueue.main.async {
            self.tableView.reloadData()
            self.updateStatus()
        }
    }

    func multiCameraCoordinator(_ coordinator: MultiCameraCoordinator, didAppendLog message: String) {
        DispatchQueue.main.async {
            self.appendLog(message)
            self.updateStatus()
        }
    }
}

private enum ConsoleColors {
    static let background = UIColor(red: 0.945, green: 0.960, blue: 0.978, alpha: 1)
    static let surface = UIColor(red: 0.990, green: 0.993, blue: 0.997, alpha: 1)
    static let primaryText = UIColor(red: 0.055, green: 0.075, blue: 0.100, alpha: 1)
    static let secondaryText = UIColor(red: 0.305, green: 0.360, blue: 0.425, alpha: 1)
    static let border = UIColor(red: 0.800, green: 0.850, blue: 0.900, alpha: 1)
    static let accent = UIColor(red: 0.035, green: 0.420, blue: 0.760, alpha: 1)
    static let accentSoft = UIColor(red: 0.870, green: 0.930, blue: 0.985, alpha: 1)
    static let controlFill = UIColor(red: 0.925, green: 0.950, blue: 0.975, alpha: 1)
    static let metricFill = UIColor(red: 0.955, green: 0.972, blue: 0.988, alpha: 1)
}
