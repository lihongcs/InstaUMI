//
//  ContentView.swift
//  instaumi
//
//  Created by LIHONG on 2026/6/9.
//

import SwiftUI
import UIKit
import Combine

struct ContentView: View {
    @StateObject private var cameraManager = MultiCameraCoordinator.shared
    @StateObject private var collectionSession = CollectionSessionController()
    @StateObject private var windowsControl = WindowsControlManager()
    @State private var selectedFeature: WorkbenchFeature = .connectInsta
    @State private var showingInstaConnection = false

    var body: some View {
        GeometryReader { geometry in
            let sidebarWidth = sidebarWidth(for: geometry.size.width)

            HStack(spacing: 0) {
                FeatureSidebar(
                    selectedFeature: $selectedFeature,
                    cameraManager: cameraManager,
                    width: sidebarWidth
                )

                OperationWorkspace(
                    selectedFeature: selectedFeature,
                    cameraManager: cameraManager,
                    collectionSession: collectionSession,
                    windowsControl: windowsControl,
                    onOpenInstaConnection: {
                        showingInstaConnection = true
                    }
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            .frame(width: geometry.size.width, height: geometry.size.height)
            .background(MusicColors.background)
        }
        .fullScreenCover(isPresented: $showingInstaConnection) {
            MultiCameraCaptureContainer {
                showingInstaConnection = false
            }
            .ignoresSafeArea(.keyboard)
        }
    }

    private func sidebarWidth(for totalWidth: CGFloat) -> CGFloat {
        min(max(totalWidth * 0.25, 220), 320)
    }
}

private enum WorkbenchFeature: String, CaseIterable, Identifiable {
    case connectInsta
    case receiveTask
    case dataCollection
    case dataInspection
    case syncData

    var id: String {
        rawValue
    }

    var title: String {
        switch self {
        case .connectInsta:
            return "连接影石"
        case .receiveTask:
            return "领取任务"
        case .dataCollection:
            return "数据采集"
        case .dataInspection:
            return "数据传输与检查"
        case .syncData:
            return "同步数据"
        }
    }

    var subtitle: String {
        switch self {
        case .connectInsta:
            return "影石设备"
        case .receiveTask:
            return "任务队列"
        case .dataCollection:
            return "启动与暂停"
        case .dataInspection:
            return "传输与核验"
        case .syncData:
            return "传输记录"
        }
    }

    var icon: String {
        switch self {
        case .connectInsta:
            return "camera.fill"
        case .receiveTask:
            return "tray.and.arrow.down.fill"
        case .dataCollection:
            return "waveform.path.ecg"
        case .dataInspection:
            return "checkmark.seal.fill"
        case .syncData:
            return "arrow.triangle.2.circlepath.circle.fill"
        }
    }
}

private struct FeatureSidebar: View {
    @Binding var selectedFeature: WorkbenchFeature
    @ObservedObject var cameraManager: MultiCameraCoordinator
    let width: CGFloat

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            sidebarHeader

            VStack(spacing: 8) {
                ForEach(WorkbenchFeature.allCases) { feature in
                    Button {
                        selectedFeature = feature
                    } label: {
                        SidebarFeatureRow(
                            feature: feature,
                            isSelected: selectedFeature == feature
                        )
                    }
                    .buttonStyle(.plain)
                }
            }

            Spacer()

            SidebarStatus(cameraManager: cameraManager)
        }
        .padding(.horizontal, 16)
        .padding(.top, 26)
        .padding(.bottom, 18)
        .frame(width: width, alignment: .leading)
        .frame(maxHeight: .infinity, alignment: .leading)
        .background(MusicColors.sidebar)
        .overlay(alignment: .trailing) {
            Rectangle()
                .fill(MusicColors.border)
                .frame(width: 1)
        }
    }

    private var sidebarHeader: some View {
        HStack(spacing: 12) {
            ZStack {
                LinearGradient(
                    colors: [MusicColors.accent, MusicColors.sensorAccent],
                    startPoint: .topLeading,
                    endPoint: .bottomTrailing
                )

                Image(systemName: "waveform")
                    .font(.system(size: 20, weight: .heavy))
                    .foregroundColor(.white)
            }
            .frame(width: 44, height: 44)
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .stroke(Color.white.opacity(0.28), lineWidth: 1)
            )

            VStack(alignment: .leading, spacing: 3) {
                Text("采集助手")
                    .font(.system(size: 23, weight: .heavy))
                    .foregroundColor(MusicColors.primaryText)

                Text("移动数据工作台")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(MusicColors.secondaryText)
            }
        }
    }
}

private struct SidebarFeatureRow: View {
    let feature: WorkbenchFeature
    let isSelected: Bool

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: feature.icon)
                .font(.system(size: 18, weight: .bold))
                .foregroundColor(isSelected ? .white : MusicColors.accent)
                .frame(width: 38, height: 38)
                .background(isSelected ? MusicColors.accent : MusicColors.accentSoft)
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))

            VStack(alignment: .leading, spacing: 3) {
                Text(feature.title)
                    .font(.system(size: 16, weight: .bold))
                    .foregroundColor(isSelected ? MusicColors.primaryText : MusicColors.secondaryText)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)

                Text(feature.subtitle)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundColor(MusicColors.mutedText)
                    .lineLimit(1)
            }

            Spacer(minLength: 4)

            if isSelected {
                Circle()
                    .fill(MusicColors.accent)
                    .frame(width: 8, height: 8)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, minHeight: 62, alignment: .leading)
        .background(isSelected ? MusicColors.surface : Color.clear)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(isSelected ? MusicColors.border : Color.clear, lineWidth: 1)
        )
    }
}

private struct SidebarStatus: View {
    @ObservedObject var cameraManager: MultiCameraCoordinator

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Circle()
                    .fill(cameraManager.isConnected ? MusicColors.green : MusicColors.orange)
                    .frame(width: 9, height: 9)

                Text(cameraManager.isConnected ? "相机已连接" : "相机未连接")
                    .font(.system(size: 13, weight: .bold))
                    .foregroundColor(MusicColors.primaryText)
            }

            Text("任务 0   待检查 0")
                .font(.system(size: 12, weight: .semibold))
                .foregroundColor(MusicColors.secondaryText)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(MusicColors.surface)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(MusicColors.border, lineWidth: 1)
        )
    }
}

private struct OperationWorkspace: View {
    let selectedFeature: WorkbenchFeature
    @ObservedObject var cameraManager: MultiCameraCoordinator
    @ObservedObject var collectionSession: CollectionSessionController
    @ObservedObject var windowsControl: WindowsControlManager
    let onOpenInstaConnection: () -> Void

    var body: some View {
        ZStack {
            MusicColors.background.ignoresSafeArea()

            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    WorkspaceHeader(feature: selectedFeature)

                    switch selectedFeature {
                    case .connectInsta:
                        ConnectInstaOperation(cameraManager: cameraManager, onOpenInstaConnection: onOpenInstaConnection)
                    case .receiveTask:
                        ReceiveTaskOperation(collectionSession: collectionSession)
                    case .dataCollection:
                        DataCollectionOperation(
                            cameraManager: cameraManager,
                            collectionSession: collectionSession,
                            windowsControl: windowsControl
                        )
                    case .dataInspection:
                        DataInspectionOperation(cameraManager: cameraManager)
                    case .syncData:
                        SyncDataOperation(
                            cameraManager: cameraManager,
                            collectionSession: collectionSession
                        )
                    }
                }
                .padding(.horizontal, 28)
                .padding(.vertical, 24)
                .frame(maxWidth: 980, alignment: .leading)
            }
        }
    }
}

private struct WorkspaceHeader: View {
    let feature: WorkbenchFeature

    var body: some View {
        HStack(spacing: 18) {
            ZStack {
                LinearGradient(
                    colors: [MusicColors.accent, MusicColors.sensorAccent],
                    startPoint: .topLeading,
                    endPoint: .bottomTrailing
                )

                Image(systemName: feature.icon)
                    .font(.system(size: 28, weight: .heavy))
                    .foregroundColor(.white)
            }
            .frame(width: 68, height: 68)
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .stroke(Color.white.opacity(0.28), lineWidth: 1)
            )

            VStack(alignment: .leading, spacing: 6) {
                Text(feature.title)
                    .font(.system(size: 32, weight: .heavy))
                    .foregroundColor(MusicColors.primaryText)
                    .lineLimit(1)
                    .minimumScaleFactor(0.72)

                Text(feature.subtitle)
                    .font(.system(size: 17, weight: .semibold))
                    .foregroundColor(MusicColors.secondaryText)
            }

            Spacer()
        }
        .padding(.top, 4)
    }
}

private struct ReceiveTaskOperation: View {
    @ObservedObject var collectionSession: CollectionSessionController

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("当前任务")
                .font(.system(size: 17, weight: .bold))
                .foregroundColor(MusicColors.secondaryText)

            TextField("输入任务名称，例如：TaskA", text: $collectionSession.currentTaskName)
                .font(.system(size: 24, weight: .semibold))
                .padding()
                .background(MusicColors.controlFill)
                .cornerRadius(8)
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(MusicColors.border, lineWidth: 1)
                )

            Text("上传到服务端时，所有相机文件将会归档到该任务名称对应的文件夹下。")
                .font(.system(size: 14))
                .foregroundColor(MusicColors.secondaryText)
        }
        .padding(22)
        .background(MusicColors.surface)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(MusicColors.border, lineWidth: 1)
        )
    }
}
private struct ConnectInstaOperation: View {
    @ObservedObject var cameraManager: MultiCameraCoordinator
    let onOpenInstaConnection: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .center, spacing: 18) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(cameraManager.isConnected ? "已连接" : "未连接")
                        .font(.system(size: 46, weight: .heavy, design: .rounded))
                        .foregroundColor(MusicColors.primaryText)
                        .lineLimit(1)
                        .minimumScaleFactor(0.6)

                    Text("影石相机")
                        .font(.system(size: 17, weight: .bold))
                        .foregroundColor(MusicColors.secondaryText)
                }

                Spacer()

                Image(systemName: "camera.aperture")
                    .font(.system(size: 54, weight: .bold))
                    .foregroundColor(MusicColors.accent)
                    .frame(width: 108, height: 108)
                    .background(MusicColors.accentSoft)
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            }

            Button {
                onOpenInstaConnection()
            } label: {
                Label("打开连接控制台", systemImage: "dot.radiowaves.left.and.right")
                    .font(.system(size: 18, weight: .bold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 16)
                    .foregroundColor(.white)
                    .background(MusicColors.accent)
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            }

            HStack(spacing: 12) {
                SmallMetric(title: "连接状态", value: cameraManager.isConnected ? "在线" : "离线")
                SmallMetric(title: "相机数量", value: "\(cameraManager.connectedCount)")
                SmallMetric(title: "当前任务", value: "--")
            }
        }
        .padding(22)
        .background(MusicColors.surface)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(MusicColors.border, lineWidth: 1)
        )
    }
}

private struct DataCollectionOperation: View {
    @ObservedObject var cameraManager: MultiCameraCoordinator
    @ObservedObject var collectionSession: CollectionSessionController
    @ObservedObject var windowsControl: WindowsControlManager

    @StateObject private var speechRecognizer = SpeechRecognizer()
    @State private var showingPermissionAlert = false
    @State private var actionMessage = "待启动时脚踏三踩总启动；采集中单踩按当前模式记录。"
    @State private var pedalMode: PedalControlMode = .slamCalibration
    @State private var lastPedalShortPressUptime: TimeInterval = 0
    @State private var lastPedalActionUptime: TimeInterval = 0
    @State private var pendingPedalStartupWorkItem: DispatchWorkItem?
    @State private var pendingPedalStartupFirstTimestamp: CollectionInputTimestamp?
    @State private var pendingPedalStartupCount = 0
    private let pedalShortPressDebounce: TimeInterval = 0.08
    private let pedalActionCooldown: TimeInterval = 0.8
    private let pedalTripleStartWindow: TimeInterval = 1.2
    private let windowsClockSyncTimer = Timer.publish(every: 5, on: .main, in: .common).autoconnect()

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(collectionSession.isTotalRunning ? "总采集中" : "总采集待启动")
                        .font(.system(size: 18, weight: .bold))
                        .foregroundColor(collectionSession.isTotalRunning ? MusicColors.green : MusicColors.secondaryText)

                    TimelineView(.periodic(from: Date(), by: 1)) { context in
                        Text(formatDuration(collectionSession.elapsed(at: context.date)))
                            .font(.system(size: 62, weight: .heavy, design: .monospaced))
                            .foregroundColor(MusicColors.primaryText)
                            .lineLimit(1)
                            .minimumScaleFactor(0.5)
                    }
                }

                Spacer()

                SessionStatusBadge(isRunning: collectionSession.isTotalRunning)
            }

            if cameraManager.isConnected == false {
                WarningBanner(text: "未连接相机，不能启动总采集。请先到“连接影石”连接相机。")
            }

            Text(actionMessage)
                .font(.system(size: 14, weight: .semibold))
                .foregroundColor(actionMessage.contains("不能") || actionMessage.contains("请先") ? .red : MusicColors.secondaryText)
                .padding(.vertical, 4)

            HStack(spacing: 12) {
                SmallMetric(title: "已完成分段", value: "\(collectionSession.completedSegmentCount)")
                SmallMetric(title: "当前分段", value: collectionSession.currentSegmentIndex == 0 ? "--" : "\(collectionSession.currentSegmentIndex)")
                SmallMetric(title: "设备", value: cameraManager.isConnected ? "在线" : "离线")
                SmallMetric(title: "日志", value: collectionSession.logURL == nil ? "未创建" : "已记录")
            }

            windowsControlCard
            collectionControlCard
            pedalControlCard
            slamTimestampCard
            segmentControlCard
            voiceControlCard

            if !cameraManager.devices.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    Text("设备参数巡检")
                        .font(.system(size: 14, weight: .bold))
                        .foregroundColor(MusicColors.secondaryText)

                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 12) {
                            ForEach(cameraManager.devices, id: \.id) { device in
                                CameraStatusCard(device: device)
                            }
                        }
                    }
                }
            }

            timestampLogCard
        }
        .padding(22)
        .background(MusicColors.surface)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(MusicColors.border, lineWidth: 1)
        )
        .alert(isPresented: $showingPermissionAlert) {
            Alert(title: Text("需要权限"), message: Text("请在系统设置中允许 App 访问麦克风与语音识别。"), dismissButton: .default(Text("好的")))
        }
        .onChange(of: speechRecognizer.detectedCommand) { command in
            guard let command = command else { return }
            handleVoiceCommand(command)
        }
        .onReceive(windowsClockSyncTimer) { _ in
            recordPeriodicWindowsClockSync()
        }
        .onAppear {
            windowsControl.prepareIfNeeded()
            cameraManager.commandEventHandler = { [collectionSession] event in
                collectionSession.recordCameraCommandEvent(event)
            }
        }
        .onDisappear {
            speechRecognizer.stopListening()
            resetPendingPedalStartup()
            cameraManager.commandEventHandler = nil
        }
        .background(
            HardwareKeyCommandBridge { command in
                handleHardwareKeyCommand(command)
            }
            .frame(width: 0, height: 0)
        )
    }

    private var pedalControlCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionTitle(
                title: "脚踏短踩模式",
                subtitle: "只决定脚踏短踩的含义；上面的手动按钮始终可用。"
            )

            HStack(spacing: 12) {
                ForEach(PedalControlMode.allCases) { mode in
                    pedalModeButton(mode)
                }
            }

            VStack(alignment: .leading, spacing: 4) {
                Text("当前短踩：\(pedalMode.shortPressHint)")
                    .font(.system(size: 13, weight: .bold))
                    .foregroundColor(MusicColors.primaryText)

                Text(collectionSession.isTotalRunning ? "采集中：单踩按当前模式记录，0.8 秒内重复触发会忽略；总结束请手动点击上方按钮。" : "待启动：1.2 秒内连续短踩三次 = 上方“总启动”快捷键。")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundColor(MusicColors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(14)
        .background(MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private func pedalModeButton(_ mode: PedalControlMode) -> some View {
        let selected = pedalMode == mode
        return Button {
            pedalMode = mode
            actionMessage = "脚踏短踩已切换为：\(mode.shortPressHint)。"
        } label: {
            VStack(spacing: 6) {
                Text(mode.title)
                    .font(.system(size: 17, weight: .heavy))
                    .foregroundColor(selected ? .white : MusicColors.primaryText)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)

                Text(mode.shortPressHint)
                    .font(.system(size: 12, weight: .bold))
                    .foregroundColor(selected ? .white.opacity(0.86) : MusicColors.secondaryText)
                    .lineLimit(1)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 14)
            .background(selected ? MusicColors.accent : MusicColors.surface)
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .stroke(selected ? MusicColors.accent : MusicColors.border, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
    }

    private var windowsControlCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .center, spacing: 12) {
                SectionTitle(
                    title: "Windows HTC 控制",
                    subtitle: "可选：总启动时请求 Windows 启动 htc_only.py，并记录 Windows - iPad 时间差"
                )

                Spacer()

                Toggle("", isOn: $windowsControl.isEnabled)
                    .labelsHidden()
            }

            if windowsControl.isEnabled {
                VStack(spacing: 10) {
                    HStack(spacing: 10) {
                        SyncInputField(title: "USB/LAN 端口", placeholder: "8766", text: $windowsControl.usbPort)
                        SyncInputField(title: "Cap ID", placeholder: "112501", text: $windowsControl.capID)
                    }

                    HStack(alignment: .center, spacing: 10) {
                        Toggle(isOn: $windowsControl.startCollectingOnLaunch) {
                            Text("启动后立即采集 HTC")
                                .font(.system(size: 13, weight: .bold))
                                .foregroundColor(MusicColors.primaryText)
                        }
                        .toggleStyle(.switch)
                        .padding(12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(MusicColors.surface)
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                    }
                }

                HStack(spacing: 12) {
                    SmallMetric(title: "连接", value: windowsControl.isConnected ? "在线" : "未确认")
                    SmallMetric(title: "HTC", value: windowsControl.isRemoteRunning ? "运行中" : "未运行")
                    SmallMetric(title: "时钟差", value: formatOptionalMs(windowsControl.lastClockOffsetMs))
                    SmallMetric(title: "往返", value: formatOptionalMs(windowsControl.lastRoundTripMs))
                }

                HStack(spacing: 12) {
                    Button {
                        windowsControl.checkConnection()
                    } label: {
                        ActionButtonLabel(
                            title: windowsControl.isBusy ? "连接中..." : "测试连接",
                            icon: "network",
                            foreground: .white,
                            background: windowsControl.isBusy ? MusicColors.secondaryText : MusicColors.accent
                        )
                    }
                    .disabled(windowsControl.isBusy)
                    .opacity(windowsControl.isBusy ? 0.55 : 1)

                    Button {
                        stopWindowsHTC()
                    } label: {
                        ActionButtonLabel(
                            title: "停止 HTC",
                            icon: "stop.fill",
                            foreground: MusicColors.accent,
                            background: MusicColors.accentSoft
                        )
                    }
                    .disabled(windowsControl.isBusy || windowsControl.isRemoteRunning == false)
                    .opacity(windowsControl.isBusy || windowsControl.isRemoteRunning == false ? 0.45 : 1)
                }

                Text(windowsControl.lastStatusMessage)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(windowsControl.lastStatusMessage.contains("失败") || windowsControl.lastStatusMessage.contains("无效") ? .red : MusicColors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(14)
        .background(MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var collectionControlCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionTitle(title: "总采集", subtitle: "启动会开始相机连续拍摄；结束会停止相机拍摄。")

            HStack(spacing: 12) {
                InstantTimestampButton(
                    disabled: collectionSession.isTotalRunning || !cameraManager.isConnected
                ) { inputTimestamp in
                    startTotalCollection(inputTimestamp: inputTimestamp)
                } label: {
                    ActionButtonLabel(
                        title: "总启动",
                        icon: "record.circle",
                        foreground: .white,
                        background: MusicColors.accent
                    )
                }
                .opacity(collectionSession.isTotalRunning || !cameraManager.isConnected ? 0.45 : 1)

                InstantTimestampButton(
                    disabled: collectionSession.isTotalRunning == false
                ) { inputTimestamp in
                    endTotalCollection(inputTimestamp: inputTimestamp)
                } label: {
                    ActionButtonLabel(
                        title: "总结束",
                        icon: "stop.circle.fill",
                        foreground: MusicColors.accent,
                        background: MusicColors.accentSoft
                    )
                }
                .opacity(collectionSession.isTotalRunning ? 1 : 0.45)
            }
        }
        .padding(14)
        .background(MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }



    private var slamTimestampCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionTitle(title: "SLAM 时间戳", subtitle: "最多保存两个时刻；脚踏短踩只添加，不删除；手动按钮仍可删除误点。")

            HStack(spacing: 8) {
                ForEach(0..<2, id: \.self) { index in
                    VStack(alignment: .leading, spacing: 5) {
                        Text("时刻 \(index + 1)")
                            .font(.system(size: 12, weight: .bold))
                            .foregroundColor(MusicColors.secondaryText)

                        Text(slamTimestampText(at: index))
                            .font(.system(size: 13, weight: .bold, design: .monospaced))
                            .foregroundColor(slamTimestampText(at: index) == "--" ? MusicColors.mutedText : MusicColors.accent)
                            .lineLimit(1)
                            .minimumScaleFactor(0.72)
                    }
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(MusicColors.surface)
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                }
            }

            HStack(spacing: 12) {
                Button {
                    addSlamTimestamp()
                } label: {
                    ActionButtonLabel(
                        title: "添加时间戳",
                        icon: "plus.circle.fill",
                        foreground: .white,
                        background: MusicColors.green
                    )
                }
                .disabled(collectionSession.isTotalRunning == false || collectionSession.slamTimestampCount >= 2)
                .opacity(collectionSession.isTotalRunning && collectionSession.slamTimestampCount < 2 ? 1 : 0.45)

                Button {
                    deleteSlamTimestamp()
                } label: {
                    ActionButtonLabel(
                        title: "删除时间戳",
                        icon: "trash.fill",
                        foreground: MusicColors.accent,
                        background: MusicColors.accentSoft
                    )
                }
                .disabled(collectionSession.slamTimestampCount == 0)
                .opacity(collectionSession.slamTimestampCount > 0 ? 1 : 0.45)
            }
        }
        .padding(14)
        .background(MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private func slamTimestampText(at index: Int) -> String {
        let events = collectionSession.slamTimestampEvents
        guard events.indices.contains(index) else {
            return "--"
        }

        return "\(events[index].unixTimeMs)"
    }

    private var segmentControlCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionTitle(title: "分段时间戳", subtitle: "每段启动/结束只写日志，连续拍摄不会中断。")

            HStack(spacing: 12) {
                InstantTimestampButton(
                    disabled: collectionSession.isTotalRunning == false || collectionSession.isSegmentRunning
                ) { inputTimestamp in
                    startSegment(inputTimestamp: inputTimestamp)
                } label: {
                    ActionButtonLabel(
                        title: "分段启动",
                        icon: "flag.fill",
                        foreground: .white,
                        background: MusicColors.green
                    )
                }
                .opacity(collectionSession.isTotalRunning && collectionSession.isSegmentRunning == false ? 1 : 0.45)

                InstantTimestampButton(
                    disabled: collectionSession.isSegmentRunning == false
                ) { inputTimestamp in
                    endSegment(inputTimestamp: inputTimestamp)
                } label: {
                    ActionButtonLabel(
                        title: "分段结束",
                        icon: "flag.checkered",
                        foreground: MusicColors.accent,
                        background: MusicColors.accentSoft
                    )
                }
                .opacity(collectionSession.isSegmentRunning ? 1 : 0.45)
            }

            SegmentCountPanel(
                completedCount: collectionSession.completedSegmentCount,
                currentSegmentIndex: collectionSession.currentSegmentIndex,
                isRunning: collectionSession.isSegmentRunning
            )
        }
        .padding(14)
        .background(MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var voiceControlCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Image(systemName: speechRecognizer.isListening ? "mic.fill" : "mic.slash.fill")
                    .foregroundColor(speechRecognizer.isListening ? MusicColors.green : MusicColors.secondaryText)

                Text("语音控制")
                    .font(.system(size: 15, weight: .bold))
                    .foregroundColor(MusicColors.primaryText)

                Spacer()

                Toggle("", isOn: Binding(
                    get: { speechRecognizer.isListening },
                    set: { newValue in
                        if newValue {
                            speechRecognizer.requestAuthorization { authorized in
                                if authorized {
                                    speechRecognizer.startListening()
                                } else {
                                    showingPermissionAlert = true
                                }
                            }
                        } else {
                            speechRecognizer.stopListening()
                        }
                    }
                ))
                .labelsHidden()
            }

            Text("可说：总启动、总结束、对齐启动、对齐结束、分段启动、分段结束。待启动时脚踏三踩可总启动。")
                .font(.system(size: 13, weight: .semibold))
                .foregroundColor(MusicColors.secondaryText)

            if speechRecognizer.isListening {
                Text(speechRecognizer.recognizedText.isEmpty ? "正在聆听..." : speechRecognizer.recognizedText)
                    .font(.system(size: 13, weight: .medium))
                    .foregroundColor(MusicColors.green)
                    .lineLimit(2)
            }
        }
        .padding(14)
        .background(MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var timestampLogCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionTitle(title: "时间戳日志", subtitle: collectionSession.logURL?.path ?? "总采集启动后自动创建独立日志")

            if collectionSession.events.isEmpty {
                Text("暂无时间戳")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundColor(MusicColors.secondaryText)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(12)
                    .background(MusicColors.controlFill)
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            } else {
                VStack(spacing: 8) {
                    ForEach(Array(collectionSession.events.suffix(8).reversed())) { event in
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text(event.title)
                                .font(.system(size: 13, weight: .bold))
                                .foregroundColor(MusicColors.primaryText)
                                .frame(width: 96, alignment: .leading)

                            Text("\(event.unixTimeMs)")
                                .font(.system(size: 13, weight: .bold, design: .monospaced))
                                .foregroundColor(MusicColors.accent)

                            Spacer()

                            if let segmentIndex = event.segmentIndex {
                                Text("第 \(segmentIndex) 段")
                                    .font(.system(size: 12, weight: .bold))
                                    .foregroundColor(MusicColors.secondaryText)
                            }
                        }
                        .padding(10)
                        .background(MusicColors.controlFill)
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                    }
                }
            }
        }
        .padding(14)
        .background(MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private func startTotalCollection(inputTimestamp: CollectionInputTimestamp? = nil) {
        let result = collectionSession.startTotal(
            connectedCameraCount: cameraManager.connectedCount,
            inputTimestamp: inputTimestamp
        )
        actionMessage = result.message
        if result.success {
            pedalMode = .slamCalibration
            cameraManager.setDataCollectionActive(true)
            cameraManager.startRecordingOnAll()
            startWindowsHTCIfNeeded()
            actionMessage = "总采集已启动；已进入 SLAM 标定。脚踏短踩两次记录 SLAM 时间戳。"
        }
    }

    private func endTotalCollection(inputTimestamp: CollectionInputTimestamp? = nil) {
        let result = collectionSession.endTotal(
            connectedCameraCount: cameraManager.connectedCount,
            inputTimestamp: inputTimestamp
        )
        actionMessage = result.message
        if result.success {
            cameraManager.stopRecordingOnAll()
            stopWindowsHTCIfNeeded()
            cameraManager.setDataCollectionActive(false)
            pedalMode = .slamCalibration
        }
    }

    private func startClockSync() {
        let result = collectionSession.startClockSync(connectedCameraCount: cameraManager.connectedCount)
        actionMessage = result.message
    }

    private func endClockSync() {
        let result = collectionSession.endClockSync(connectedCameraCount: cameraManager.connectedCount)
        actionMessage = result.message
    }

    private func addSlamTimestamp(inputTimestamp: CollectionInputTimestamp? = nil) {
        let result = collectionSession.addSlamTimestamp(
            connectedCameraCount: cameraManager.connectedCount,
            inputTimestamp: inputTimestamp
        )
        actionMessage = result.message
        if result.success && collectionSession.slamTimestampCount >= 2 {
            pedalMode = .segmentCollection
            actionMessage = "\(result.message)；SLAM 标定完成，已自动进入分段采集。"
        }
    }

    private func deleteSlamTimestamp() {
        let result = collectionSession.deleteSlamTimestamp()
        actionMessage = result.message
    }

    private func startSegment(inputTimestamp: CollectionInputTimestamp? = nil) {
        let result = collectionSession.startSegment(
            connectedCameraCount: cameraManager.connectedCount,
            inputTimestamp: inputTimestamp
        )
        actionMessage = result.message
    }

    private func endSegment(inputTimestamp: CollectionInputTimestamp? = nil) {
        let result = collectionSession.endSegment(
            connectedCameraCount: cameraManager.connectedCount,
            inputTimestamp: inputTimestamp
        )
        actionMessage = result.message
    }

    private func handleVoiceCommand(_ command: SpeechRecognizer.VoiceCommand) {
        switch command {
        case .startTotal:
            startTotalCollection()
        case .endTotal:
            endTotalCollection()
        case .startClockSync:
            startClockSync()
        case .endClockSync:
            endClockSync()
        case .startSegment:
            startSegment()
        case .endSegment:
            endSegment()
        case .start:
            if collectionSession.isTotalRunning {
                startSegment()
            } else {
                startTotalCollection()
            }
        case .pause:
            if collectionSession.isSegmentRunning {
                endSegment()
            } else if collectionSession.isClockSyncRunning {
                endClockSync()
            } else {
                actionMessage = "请说“分段结束”或“对齐结束”"
            }
        case .finish:
            if collectionSession.isSegmentRunning {
                endSegment()
            } else if collectionSession.isClockSyncRunning {
                endClockSync()
            } else {
                endTotalCollection()
            }
        }
    }

    private func handleHardwareKeyCommand(_ command: HardwareKeyCommand) {
        switch command {
        case .pedalShortPress(let inputTimestamp):
            handlePedalShortPress(inputTimestamp: inputTimestamp)
        case .pedalLongPress(let inputTimestamp):
            handlePedalLongPress(inputTimestamp: inputTimestamp)
        case .startTotal:
            startTotalCollection(inputTimestamp: .now())
        case .endTotal:
            endTotalCollection(inputTimestamp: .now())
        case .startSegment:
            startSegment(inputTimestamp: .now())
        case .endSegment:
            endSegment(inputTimestamp: .now())
        }
    }

    private func handlePedalShortPress(inputTimestamp: CollectionInputTimestamp) {
        let now = ProcessInfo.processInfo.systemUptime
        guard now - lastPedalShortPressUptime >= pedalShortPressDebounce else {
            return
        }
        lastPedalShortPressUptime = now

        guard collectionSession.isTotalRunning else {
            handlePedalTripleStartPress(inputTimestamp: inputTimestamp)
            return
        }

        resetPendingPedalStartup()
        guard now - lastPedalActionUptime >= pedalActionCooldown else {
            return
        }
        lastPedalActionUptime = now
        performPedalSingleShortPress(inputTimestamp: inputTimestamp)
    }

    private func handlePedalTripleStartPress(inputTimestamp: CollectionInputTimestamp) {
        let shouldStartNewWindow: Bool
        if let firstTimestamp = pendingPedalStartupFirstTimestamp {
            shouldStartNewWindow = inputTimestamp.monotonicTime - firstTimestamp.monotonicTime > pedalTripleStartWindow
        } else {
            shouldStartNewWindow = true
        }

        if shouldStartNewWindow {
            pendingPedalStartupWorkItem?.cancel()
            pendingPedalStartupFirstTimestamp = inputTimestamp
            pendingPedalStartupCount = 1
            let workItem = DispatchWorkItem {
                pendingPedalStartupWorkItem = nil
                pendingPedalStartupFirstTimestamp = nil
                pendingPedalStartupCount = 0
                actionMessage = "脚踏启动未完成；请在 1.2 秒内连续短踩三次。"
            }
            pendingPedalStartupWorkItem = workItem
            DispatchQueue.main.asyncAfter(
                deadline: .now() + .milliseconds(Int(pedalTripleStartWindow * 1000)),
                execute: workItem
            )
        } else {
            pendingPedalStartupCount += 1
        }

        if pendingPedalStartupCount >= 3 {
            resetPendingPedalStartup()
            actionMessage = "脚踏三踩已触发：正在执行总启动。"
            startTotalCollection(inputTimestamp: inputTimestamp)
        } else {
            actionMessage = "脚踏启动 \(pendingPedalStartupCount)/3；1.2 秒内连续短踩三次启动总采集。"
        }
    }

    private func performPedalSingleShortPress(inputTimestamp: CollectionInputTimestamp) {
        switch pedalMode {
        case .slamCalibration:
            addSlamTimestamp(inputTimestamp: inputTimestamp)
        case .segmentCollection:
            if collectionSession.isSegmentRunning {
                endSegment(inputTimestamp: inputTimestamp)
            } else {
                startSegment(inputTimestamp: inputTimestamp)
            }
        }
    }

    private func resetPendingPedalStartup() {
        pendingPedalStartupWorkItem?.cancel()
        pendingPedalStartupWorkItem = nil
        pendingPedalStartupFirstTimestamp = nil
        pendingPedalStartupCount = 0
    }

    private func handlePedalLongPress(inputTimestamp: CollectionInputTimestamp) {
        resetPendingPedalStartup()

        if collectionSession.isTotalRunning {
            actionMessage = "脚踏长踩已触发：正在执行总结束。"
            endTotalCollection(inputTimestamp: inputTimestamp)
        } else {
            actionMessage = "脚踏长踩已触发：正在执行总启动。"
            startTotalCollection(inputTimestamp: inputTimestamp)
        }
    }

    private func formatDuration(_ duration: TimeInterval) -> String {
        let seconds = max(Int(duration), 0)
        let hours = seconds / 3600
        let minutes = (seconds % 3600) / 60
        let remainingSeconds = seconds % 60

        return String(format: "%02d:%02d:%02d", hours, minutes, remainingSeconds)
    }

    private func startWindowsHTCIfNeeded() {
        guard windowsControl.isEnabled else {
            return
        }

        windowsControl.startHTC(
            sessionID: collectionSession.sessionID,
            taskName: collectionSession.currentTaskName
        ) { result in
            if result.success {
                collectionSession.recordWindowsControlStart(
                    pid: result.pid,
                    connectedCameraCount: cameraManager.connectedCount,
                    note: result.message
                )

                if let offsetMs = result.clockOffsetMs, let roundTripMs = result.roundTripMs {
                    collectionSession.recordWindowsClockSync(
                        offsetMs: offsetMs,
                        roundTripMs: roundTripMs,
                        serverTimeMs: result.serverUnixTimeMs,
                        connectedCameraCount: cameraManager.connectedCount,
                        note: "Windows - iPad = \(formatMs(offsetMs))，RTT = \(formatMs(roundTripMs))"
                    )
                }

                actionMessage = "总采集已启动；Windows HTC 已请求启动"
            } else {
                actionMessage = "总采集已启动；\(result.message)"
            }
        }
    }

    private func recordPeriodicWindowsClockSync() {
        guard collectionSession.isTotalRunning,
              windowsControl.isEnabled,
              windowsControl.isConnected,
              windowsControl.isBusy == false else {
            return
        }

        windowsControl.checkConnection { result in
            guard result.success,
                  let offsetMs = result.clockOffsetMs,
                  let roundTripMs = result.roundTripMs else {
                return
            }
            collectionSession.recordWindowsClockSync(
                offsetMs: offsetMs,
                roundTripMs: roundTripMs,
                serverTimeMs: result.serverUnixTimeMs,
                connectedCameraCount: cameraManager.connectedCount,
                note: "periodic ping；Windows - iPad = \(formatMs(offsetMs))，RTT = \(formatMs(roundTripMs))"
            )
        }
    }

    private func stopWindowsHTCIfNeeded() {
        guard windowsControl.isEnabled else {
            return
        }

        stopWindowsHTC()
    }

    private func stopWindowsHTC() {
        windowsControl.stopHTC { result in
            if result.success {
                collectionSession.recordWindowsControlStop(
                    connectedCameraCount: cameraManager.connectedCount,
                    note: result.message
                )
                if collectionSession.isTotalRunning == false {
                    actionMessage = "总采集已结束；Windows HTC 已请求停止"
                }
            } else if collectionSession.isTotalRunning == false {
                actionMessage = "总采集已结束；\(result.message)"
            } else {
                actionMessage = result.message
            }
        }
    }

    private func formatOptionalMs(_ value: Double?) -> String {
        guard let value else {
            return "--"
        }
        return formatMs(value)
    }

    private func formatMs(_ value: Double) -> String {
        String(format: "%.1f ms", value)
    }
}

private struct SectionTitle: View {
    let title: String
    let subtitle: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.system(size: 16, weight: .bold))
                .foregroundColor(MusicColors.primaryText)

            Text(subtitle)
                .font(.system(size: 13, weight: .semibold))
                .foregroundColor(MusicColors.secondaryText)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct WarningBanner: View {
    let text: String

    var body: some View {
        Label(text, systemImage: "exclamationmark.triangle.fill")
            .font(.system(size: 13, weight: .bold))
            .foregroundColor(MusicColors.orange)
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(MusicColors.orange.opacity(0.12))
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

private struct SessionStatusBadge: View {
    let isRunning: Bool

    var body: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(isRunning ? MusicColors.green : MusicColors.secondaryText)
                .frame(width: 10, height: 10)

            Text(isRunning ? "总采集中" : "未启动")
                .font(.system(size: 14, weight: .bold))
                .foregroundColor(isRunning ? MusicColors.green : MusicColors.secondaryText)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background((isRunning ? MusicColors.green : MusicColors.secondaryText).opacity(0.12))
        .clipShape(Capsule())
    }
}

private struct CameraStatusCard: View {
    let device: MultiCameraDeviceRecord

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(device.name)
                    .font(.system(size: 14, weight: .bold))
                    .foregroundColor(MusicColors.primaryText)
                Spacer(minLength: 16)
                if let battery = device.batteryLevel {
                    HStack(spacing: 4) {
                        Image(systemName: battery < 20 ? "battery.25" : "battery.100")
                        Text("\(battery)%")
                    }
                    .font(.system(size: 11, weight: .medium))
                    .foregroundColor(battery < 20 ? .red : MusicColors.secondaryText)
                }
            }

            Text("SN: \(device.serialNumber ?? "未知")")
                .font(.system(size: 12, weight: .regular, design: .monospaced))
                .foregroundColor(MusicColors.secondaryText)

            HStack(spacing: 8) {
                if let lensMode = device.lensModeStr {
                    badgeView(text: lensMode, color: MusicColors.accent)
                }
                if let res = device.resolutionStr, let fps = device.fps {
                    badgeView(text: "\(res) @ \(fps)fps", color: MusicColors.sensorAccent)
                }
            }
            .padding(.top, 4)
        }
        .padding(12)
        .background(MusicColors.controlFill)
        .cornerRadius(8)
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(MusicColors.border, lineWidth: 1)
        )
    }

    private func badgeView(text: String, color: Color) -> some View {
        Text(text)
            .font(.system(size: 10, weight: .bold))
            .foregroundColor(.white)
            .padding(.horizontal, 6)
            .padding(.vertical, 3)
            .background(color)
            .cornerRadius(4)
    }
}

private struct SyncDataOperation: View {
    @ObservedObject var cameraManager: MultiCameraCoordinator
    @ObservedObject var collectionSession: CollectionSessionController
    @StateObject private var syncManager = SyncManager()
    @State private var selectedItemIDs = Set<String>()
    @State private var syncMessage = "请先确认 iPad 已连接到路由器 Wi-Fi"

    private var selectedItems: [MultiCameraInspectionItem] {
        cameraManager.inspectionItems.filter { selectedItemIDs.contains($0.id) }
    }

    private var selectedURLs: [URL] {
        selectedItems.flatMap { $0.relatedURLs }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("\(selectedItems.count)")
                        .font(.system(size: 58, weight: .heavy, design: .rounded))
                        .foregroundColor(MusicColors.primaryText)

                    Text("已选择数据组")
                        .font(.system(size: 17, weight: .bold))
                        .foregroundColor(MusicColors.secondaryText)
                }

                Spacer()

                SyncStatusBadge(isSyncing: syncManager.isSyncing)
            }

            Text(syncMessage)
                .font(.system(size: 14, weight: .semibold))
                .foregroundColor(syncMessage.contains("错误") || syncMessage.contains("失败") ? .red : MusicColors.secondaryText)

            routerWiFiCard
            serverConfigCard
            selectedDataCard
            transferActionCard
            transferRecordCard
        }
        .padding(22)
        .background(MusicColors.surface)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(MusicColors.border, lineWidth: 1)
        )
        .onAppear {
            cameraManager.loadInspectionItems()
        }
    }

    private var routerWiFiCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionTitle(title: "1. 路由器 Wi-Fi 设置", subtitle: "同步传输需要 iPad 手动连接局域网路由器，不是连接相机热点")

            VStack(alignment: .leading, spacing: 12) {
                Text("由于当前签名不支持 App 内自动切换 Wi-Fi，请在系统 Wi-Fi 页面手动连接路由器。连接完成后回到采集助手，再继续配置服务器和选择数据。")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(MusicColors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)

                HStack(spacing: 12) {
                    Button {
                        syncMessage = "请在系统 Wi-Fi 中手动连接路由器，连接完成后回到采集助手。"
                        openSystemWiFiSettings()
                    } label: {
                        ActionButtonLabel(
                            title: "打开系统 Wi-Fi",
                            icon: "wifi",
                            foreground: .white,
                            background: MusicColors.accent
                        )
                    }

                    Button {
                        syncMessage = "已确认连接路由器 Wi-Fi，可继续配置服务器"
                    } label: {
                        ActionButtonLabel(
                            title: "已连接路由器",
                            icon: "checkmark.circle.fill",
                            foreground: MusicColors.accent,
                            background: MusicColors.accentSoft
                        )
                    }
                }
            }
        }
        .padding(14)
        .background(MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var serverConfigCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionTitle(title: "2. 服务器配置", subtitle: "设置服务器地址、上传接口和认证信息")

            VStack(spacing: 10) {
                HStack(spacing: 10) {
                    SyncInputField(title: "IP 地址", placeholder: "192.168.1.100", text: $syncManager.serverIP)
                    SyncInputField(title: "端口", placeholder: "8000", text: $syncManager.serverPort)
                }

                SyncInputField(title: "上传接口", placeholder: "/upload_file", text: $syncManager.uploadPath)

                HStack(spacing: 10) {
                    SyncInputField(title: "用户名", placeholder: "admin", text: $syncManager.username)
                    SyncSecureInputField(title: "密码", placeholder: "服务器密码", text: $syncManager.password)
                }
            }
        }
        .padding(14)
        .background(MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var selectedDataCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                SectionTitle(title: "3. 选择需要传输的数据", subtitle: "一组数据会包含媒体、解析文件、gyro 和全景文件")
                Spacer()
                Button {
                    cameraManager.loadInspectionItems()
                    syncMessage = "已刷新本地数据列表"
                } label: {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 16, weight: .bold))
                        .foregroundColor(MusicColors.accent)
                        .frame(width: 42, height: 42)
                        .background(MusicColors.surface)
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                }
            }

            HStack(spacing: 12) {
                SmallMetric(title: "本地数据组", value: "\(cameraManager.inspectionItems.count)")
                SmallMetric(title: "已选文件数", value: "\(selectedURLs.count)")
                SmallMetric(title: "已选媒体大小", value: selectedSizeText)
            }

            if cameraManager.inspectionItems.isEmpty {
                Text("暂无可传输数据，请先在“数据传输与检查”中下载并解析数据。")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(MusicColors.secondaryText)
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(MusicColors.surface)
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            } else {
                VStack(spacing: 8) {
                    ForEach(cameraManager.inspectionItems) { item in
                        SyncSelectableDataRow(
                            item: item,
                            isSelected: selectedItemIDs.contains(item.id),
                            onToggle: {
                                if selectedItemIDs.contains(item.id) {
                                    selectedItemIDs.remove(item.id)
                                } else {
                                    selectedItemIDs.insert(item.id)
                                }
                            }
                        )
                    }
                }
            }
        }
        .padding(14)
        .background(MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var transferActionCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionTitle(title: "4. 开始同步传输", subtitle: "传输过程会在下方实时记录每个文件的结果")

            HStack(spacing: 12) {
                Button {
                    syncManager.currentTaskName = collectionSession.currentTaskName
                    syncManager.syncFiles(selectedURLs)
                    syncMessage = selectedURLs.isEmpty ? "错误：请先选择需要传输的数据" : "正在同步 \(selectedURLs.count) 个文件"
                } label: {
                    ActionButtonLabel(
                        title: syncManager.isSyncing ? "正在同步..." : "开始同步",
                        icon: "arrow.up.circle.fill",
                        foreground: .white,
                        background: syncManager.isSyncing ? MusicColors.secondaryText : MusicColors.accent
                    )
                }
                .disabled(syncManager.isSyncing || selectedURLs.isEmpty)
                .opacity(syncManager.isSyncing || selectedURLs.isEmpty ? 0.55 : 1)

                Button {
                    syncManager.clearTransferLogs()
                    syncMessage = "已清空传输记录"
                } label: {
                    ActionButtonLabel(
                        title: "清空记录",
                        icon: "trash",
                        foreground: MusicColors.accent,
                        background: MusicColors.accentSoft
                    )
                }
            }

            Text("状态：\(syncManager.lastSyncStatus)")
                .font(.system(size: 13, weight: .bold))
                .foregroundColor(syncManager.lastSyncStatus.contains("错误") || syncManager.lastSyncStatus.contains("失败") ? .red : MusicColors.secondaryText)
        }
        .padding(14)
        .background(MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var transferRecordCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionTitle(title: "实时传输记录", subtitle: "逐文件显示开始、成功或失败")

            if syncManager.transferLogs.isEmpty {
                Text("暂无传输记录")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(MusicColors.secondaryText)
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(MusicColors.surface)
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            } else {
                VStack(spacing: 8) {
                    ForEach(syncManager.transferLogs) { log in
                        SyncTransferLogRow(log: log)
                    }
                }
            }
        }
        .padding(14)
        .background(MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var selectedSizeText: String {
        let total = selectedItems.reduce(UInt64(0)) { $0 + $1.fileSizeBytes }
        return ByteCountFormatter.string(fromByteCount: Int64(total), countStyle: .file)
    }

}

private struct SyncStatusBadge: View {
    let isSyncing: Bool

    var body: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(isSyncing ? MusicColors.green : MusicColors.secondaryText)
                .frame(width: 10, height: 10)

            Text(isSyncing ? "同步中" : "未同步")
                .font(.system(size: 14, weight: .bold))
                .foregroundColor(isSyncing ? MusicColors.green : MusicColors.secondaryText)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background((isSyncing ? MusicColors.green : MusicColors.secondaryText).opacity(0.12))
        .clipShape(Capsule())
    }
}

private struct SyncInputField: View {
    let title: String
    let placeholder: String
    @Binding var text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.system(size: 12, weight: .bold))
                .foregroundColor(MusicColors.secondaryText)
            TextField(placeholder, text: $text)
                .font(.system(size: 15, weight: .semibold))
                .foregroundColor(MusicColors.primaryText)
                .padding(12)
                .background(MusicColors.surface)
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .stroke(MusicColors.border, lineWidth: 1)
                )
        }
        .frame(maxWidth: .infinity)
    }
}

private struct SyncSecureInputField: View {
    let title: String
    let placeholder: String
    @Binding var text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.system(size: 12, weight: .bold))
                .foregroundColor(MusicColors.secondaryText)
            SecureField(placeholder, text: $text)
                .font(.system(size: 15, weight: .semibold))
                .foregroundColor(MusicColors.primaryText)
                .padding(12)
                .background(MusicColors.surface)
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .stroke(MusicColors.border, lineWidth: 1)
                )
        }
        .frame(maxWidth: .infinity)
    }
}

private struct SyncSelectableDataRow: View {
    let item: MultiCameraInspectionItem
    let isSelected: Bool
    let onToggle: () -> Void

    var body: some View {
        Button(action: onToggle) {
            HStack(spacing: 10) {
                Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 20, weight: .bold))
                    .foregroundColor(isSelected ? MusicColors.accent : MusicColors.secondaryText)
                    .frame(width: 28)

                Image(systemName: item.mediaKindText == "视频" ? "video.fill" : "photo.fill")
                    .foregroundColor(MusicColors.accent)
                    .frame(width: 34, height: 34)
                    .background(MusicColors.accentSoft)
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))

                VStack(alignment: .leading, spacing: 3) {
                    Text(item.fileName)
                        .font(.system(size: 13, weight: .bold))
                        .foregroundColor(MusicColors.primaryText)
                        .lineLimit(1)
                        .truncationMode(.middle)

                    Text("\(item.mediaKindText) · \(item.fileSizeText) · \(item.relatedURLs.count) 个关联文件")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundColor(MusicColors.secondaryText)
                        .lineLimit(1)
                }

                Spacer()

                HStack(spacing: 6) {
                    InspectionBadge(title: item.hasSidecar ? "已解析" : "未解析", isReady: item.hasSidecar)
                }
            }
            .padding(10)
            .background(isSelected ? MusicColors.accentSoft : MusicColors.surface)
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .stroke(isSelected ? MusicColors.accent : MusicColors.border, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
    }
}

private struct SyncTransferLogRow: View {
    let log: SyncTransferLog

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: log.isError ? "exclamationmark.triangle.fill" : "arrow.up.doc.fill")
                .foregroundColor(log.isError ? .red : MusicColors.green)
                .frame(width: 32, height: 32)
                .background((log.isError ? Color.red : MusicColors.green).opacity(0.12))
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))

            VStack(alignment: .leading, spacing: 3) {
                Text(log.fileName)
                    .font(.system(size: 13, weight: .bold))
                    .foregroundColor(MusicColors.primaryText)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(log.detail)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundColor(log.isError ? .red : MusicColors.secondaryText)
                    .lineLimit(2)
            }

            Spacer()

            Text(formatTime(log.timestamp))
                .font(.system(size: 12, weight: .bold, design: .monospaced))
                .foregroundColor(MusicColors.secondaryText)
        }
        .padding(10)
        .background(MusicColors.surface)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private func formatTime(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter.string(from: date)
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

private struct DataInspectionOperation: View {
    @ObservedObject var cameraManager: MultiCameraCoordinator
    @StateObject private var directSyncManager = SyncManager()
    @State private var selectedItemID: String?
    @State private var selectedRemoteResourceIDs = Set<String>()
    @State private var pendingDeleteItem: MultiCameraInspectionItem?
    @State private var actionMessage = "等待读取本地数据"

    private var selectedItem: MultiCameraInspectionItem? {
        cameraManager.inspectionItems.first { $0.id == selectedItemID } ?? cameraManager.inspectionItems.first
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("\(cameraManager.cameraRemoteResources.count) / \(cameraManager.inspectionItems.count)")
                        .font(.system(size: 58, weight: .heavy, design: .rounded))
                        .foregroundColor(MusicColors.primaryText)

                    Text("相机记录 / 本机数据")
                        .font(.system(size: 17, weight: .bold))
                        .foregroundColor(MusicColors.secondaryText)
                }

                Spacer()

                Button {
                    refreshItems()
                } label: {
                    Label("刷新", systemImage: "arrow.clockwise")
                        .font(.system(size: 15, weight: .bold))
                        .foregroundColor(MusicColors.accent)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .background(MusicColors.accentSoft)
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                }
            }

            Text(actionMessage)
                .font(.system(size: 14, weight: .semibold))
                .foregroundColor(actionMessage.contains("删除") ? MusicColors.orange : MusicColors.secondaryText)

            sourceSummary
            instaDataSection
            localDataSection
        }
        .padding(22)
        .background(MusicColors.surface)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(MusicColors.border, lineWidth: 1)
        )
        .onAppear {
            refreshItems()
        }
        .onChange(of: cameraManager.inspectionItems.count) { _ in
            if selectedItem == nil {
                selectedItemID = cameraManager.inspectionItems.first?.id
            }
        }
        .alert(item: $pendingDeleteItem) { item in
            Alert(
                title: Text("删除这组数据？"),
                message: Text("会删除原始媒体、伴随数据及 gyro 文件。"),
                primaryButton: .destructive(Text("删除")) {
                    actionMessage = cameraManager.deleteInspectionItem(item)
                    selectedItemID = cameraManager.inspectionItems.first?.id
                },
                secondaryButton: .cancel(Text("取消"))
            )
        }
    }

    private var sourceSummary: some View {
        HStack(spacing: 12) {
            SmallMetric(title: "相机记录", value: "\(cameraManager.cameraRemoteResources.count)")
            SmallMetric(title: "相机待下载", value: "\(cameraManager.cameraRemoteResources.filter { !$0.isDownloaded }.count)")
            SmallMetric(title: "本机数据组", value: "\(cameraManager.inspectionItems.count)")
            SmallMetric(title: "本机总大小", value: totalSizeText)
        }
    }

    private var instaDataSection: some View {
        DataSourceSection(
            title: "影石相机上的数据",
            subtitle: "连接相机 Wi-Fi，读取相机内记录，然后只下载选中的数据",
            icon: "camera.fill",
            badge: cameraManager.wifiSocketStatusText
        ) {
            WiFiTransferConnectionCard(cameraManager: cameraManager)
            postExtractionCard
            CameraResourceSelectionCard(
                resources: cameraManager.cameraRemoteResources,
                statusText: cameraManager.cameraResourceStatusText,
                selectedIDs: $selectedRemoteResourceIDs
            )
        }
    }

    private var localDataSection: some View {
        DataSourceSection(
            title: "本机已保存的数据",
            subtitle: "这里是已经从影石相机下载到 iPad 的数据，可检查、快放、解析、导出或删除",
            icon: "internaldrive.fill",
            badge: "\(cameraManager.inspectionItems.count) 组"
        ) {
            TransferRecordsCard(
                resources: Array(cameraManager.downloadedResources.suffix(6).reversed()),
                itemCount: cameraManager.inspectionItems.count
            )

            SectionTitle(title: "数据可视化与检查", subtitle: "查看本机媒体、IMU 摘要、解析完整性，并支持加速播放预览")
            inspectionSteps

            if cameraManager.inspectionItems.isEmpty {
                emptyState
            } else {
                HStack(alignment: .top, spacing: 16) {
                    itemList
                        .frame(maxWidth: 340)

                    if let selectedItem = selectedItem {
                        InspectionDetailPanel(
                            item: selectedItem,
                            onDelete: {
                                pendingDeleteItem = selectedItem
                            }
                        )
                    }
                }
            }
        }
    }

    private var inspectionSteps: some View {
        HStack(spacing: 12) {
            SmallMetric(title: "已下载", value: "\(cameraManager.inspectionItems.count)")
            SmallMetric(title: "已解析", value: "\(cameraManager.inspectionItems.filter(\.hasSidecar).count)")
            SmallMetric(title: "总大小", value: totalSizeText)
        }
    }

    private var postExtractionCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionTitle(title: "相机与本地数据管理", subtitle: "下载或删除相机记录；提取并分析本地已保存数据的 IMU 信息")

            HStack(spacing: 12) {
                Button {
                    selectedRemoteResourceIDs.removeAll()
                    cameraManager.fetchCameraResourceRecords()
                    actionMessage = "正在读取相机上的数据记录"
                } label: {
                    ActionButtonLabel(
                        title: "读取相机记录",
                        icon: "list.bullet.rectangle.fill",
                        foreground: .white,
                        background: MusicColors.accent
                    )
                }

                Button {
                    let selectedResources = cameraManager.cameraRemoteResources.filter { selectedRemoteResourceIDs.contains($0.id) }
                    cameraManager.downloadCameraResources(selectedResources)
                    actionMessage = selectedResources.isEmpty ? "请先选择要下载的数据" : "正在下载选中的 \(selectedResources.count) 条记录"
                } label: {
                    ActionButtonLabel(
                        title: "下载选中 \(selectedRemoteResourceIDs.count)",
                        icon: "arrow.down.circle.fill",
                        foreground: selectedRemoteResourceIDs.isEmpty ? MusicColors.secondaryText : MusicColors.accent,
                        background: selectedRemoteResourceIDs.isEmpty ? MusicColors.controlFill : MusicColors.accentSoft
                    )
                }
                .disabled(selectedRemoteResourceIDs.isEmpty)
                .opacity(selectedRemoteResourceIDs.isEmpty ? 0.55 : 1)

                Button {
                    let selectedResources = cameraManager.cameraRemoteResources.filter { selectedRemoteResourceIDs.contains($0.id) }
                    cameraManager.deleteCameraResources(selectedResources)
                    actionMessage = selectedResources.isEmpty ? "请先选择要删除的相机文件" : "正在删除 \(selectedResources.count) 个相机文件"
                } label: {
                    ActionButtonLabel(
                        title: "删除相机文件 \(selectedRemoteResourceIDs.count)",
                        icon: "trash.fill",
                        foreground: selectedRemoteResourceIDs.isEmpty ? MusicColors.secondaryText : .red,
                        background: selectedRemoteResourceIDs.isEmpty ? MusicColors.controlFill : Color.red.opacity(0.15)
                    )
                }
                .disabled(selectedRemoteResourceIDs.isEmpty)
                .opacity(selectedRemoteResourceIDs.isEmpty ? 0.55 : 1)
            }

            HStack(spacing: 12) {
                Button {
                    cameraManager.parseDownloadedResources()
                    actionMessage = "正在后台提取 IMU 数据"
                } label: {
                    ActionButtonLabel(
                        title: "提取 IMU 运动数据",
                        icon: "waveform.path.ecg",
                        foreground: MusicColors.accent,
                        background: MusicColors.accentSoft
                    )
                }
            }
        }
        .padding(14)
        .background(MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }


    private var emptyState: some View {
        VStack(spacing: 12) {
            Image(systemName: "externaldrive.badge.magnifyingglass")
                .font(.system(size: 42, weight: .bold))
                .foregroundColor(MusicColors.accent)

            Text("还没有可检查的数据")
                .font(.system(size: 17, weight: .bold))
                .foregroundColor(MusicColors.primaryText)

            Button {
                cameraManager.parseDownloadedResources()
                actionMessage = "正在后台解析下载目录"
            } label: {
                ActionButtonLabel(
                    title: "重新解析下载目录",
                    icon: "doc.text.magnifyingglass",
                    foreground: .white,
                    background: MusicColors.accent
                )
            }
        }
        .padding(24)
        .frame(maxWidth: .infinity)
        .background(MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private var itemList: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionTitle(title: "数据列表", subtitle: "downloads / sidecars / panoramas")

            ForEach(cameraManager.inspectionItems) { item in
                Button {
                    selectedItemID = item.id
                } label: {
                    InspectionItemRow(
                        item: item,
                        isSelected: selectedItem?.id == item.id
                    )
                }
                .buttonStyle(.plain)
            }
        }
    }

    private var totalSizeText: String {
        let total = cameraManager.inspectionItems.reduce(UInt64(0)) { $0 + $1.fileSizeBytes }
        return ByteCountFormatter.string(fromByteCount: Int64(total), countStyle: .file)
    }

    private func refreshItems() {
        cameraManager.loadInspectionItems()
        selectedItemID = selectedItemID ?? cameraManager.inspectionItems.first?.id
        actionMessage = cameraManager.inspectionItems.isEmpty ? "未发现本地媒体文件" : "已读取本地保存数据"
    }
}

private struct DataSourceSection<Content: View>: View {
    let title: String
    let subtitle: String
    let icon: String
    let badge: String
    let content: Content

    init(
        title: String,
        subtitle: String,
        icon: String,
        badge: String,
        @ViewBuilder content: () -> Content
    ) {
        self.title = title
        self.subtitle = subtitle
        self.icon = icon
        self.badge = badge
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 12) {
                Image(systemName: icon)
                    .font(.system(size: 18, weight: .heavy))
                    .foregroundColor(.white)
                    .frame(width: 44, height: 44)
                    .background(MusicColors.accent)
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))

                VStack(alignment: .leading, spacing: 4) {
                    Text(title)
                        .font(.system(size: 22, weight: .heavy))
                        .foregroundColor(MusicColors.primaryText)
                    Text(subtitle)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(MusicColors.secondaryText)
                        .fixedSize(horizontal: false, vertical: true)
                }

                Spacer()

                Text(badge)
                    .font(.system(size: 13, weight: .bold))
                    .foregroundColor(MusicColors.accent)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .background(MusicColors.accentSoft)
                    .clipShape(Capsule())
            }

            Rectangle()
                .fill(MusicColors.accent)
                .frame(height: 3)
                .clipShape(Capsule())

            content
        }
        .padding(.top, 8)
    }
}


private struct WiFiTransferConnectionCard: View {
    @ObservedObject var cameraManager: MultiCameraCoordinator
    @State private var connectionMessage = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                SectionTitle(title: "Wi-Fi 传输连接", subtitle: "先开启相机热点，再到系统 Wi-Fi 手动连接，返回后建立通讯 Socket")

                Spacer()

                HStack(spacing: 8) {
                    Circle()
                        .fill(cameraManager.wifiSocketStatusText == "已连接" ? MusicColors.green : MusicColors.orange)
                        .frame(width: 9, height: 9)
                    Text("Socket \(cameraManager.wifiSocketStatusText)")
                        .font(.system(size: 13, weight: .bold))
                        .foregroundColor(MusicColors.primaryText)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 7)
                .background(MusicColors.surface)
                .clipShape(Capsule())
            }

            VStack(alignment: .leading, spacing: 10) {
                Text("当前账号不能在 App 内一键切换 Wi-Fi。请先让影石相机开启热点，再跳到系统 Wi-Fi 手动选择相机热点；连接完成后回到这里点击“连接 Wi-Fi 通道”。")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(MusicColors.secondaryText)
                    .fixedSize(horizontal: false, vertical: true)

                if !connectionMessage.isEmpty {
                    Text(connectionMessage)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(MusicColors.secondaryText)
                }

                HStack(spacing: 12) {
                    Button {
                        cameraManager.openCameraWiFiOnAll()
                        connectionMessage = "已请求相机开启热点。请点击“打开系统 Wi-Fi”后手动连接相机热点。"
                    } label: {
                        ActionButtonLabel(
                            title: "开启相机热点",
                            icon: "dot.radiowaves.left.and.right",
                            foreground: .white,
                            background: MusicColors.accent
                        )
                    }
                    .disabled(cameraManager.isConnected == false)
                    .opacity(cameraManager.isConnected ? 1 : 0.55)

                    Button {
                        connectionMessage = "请在系统 Wi-Fi 中手动连接相机热点，连接完成后回到采集助手。"
                        openSystemWiFiSettings()
                    } label: {
                        ActionButtonLabel(
                            title: "打开系统 Wi-Fi",
                            icon: "wifi",
                            foreground: MusicColors.accent,
                            background: MusicColors.accentSoft
                        )
                    }

                    Button {
                        cameraManager.setupWiFiSocketManager()
                        connectionMessage = "正在挂载 Wi-Fi 通道；如果仍未连接，请确认系统 Wi-Fi 已切到相机热点。"
                    } label: {
                        ActionButtonLabel(
                            title: "连接 Wi-Fi 通道",
                            icon: "cable.connector",
                            foreground: MusicColors.accent,
                            background: MusicColors.accentSoft
                        )
                    }

                    Button {
                        cameraManager.resetWiFiSocketManager()
                        connectionMessage = "已重置 Wi-Fi 通道；如果仍未连接，请确认系统 Wi-Fi 是影石相机热点。"
                    } label: {
                        ActionButtonLabel(
                            title: "重置通道",
                            icon: "arrow.clockwise",
                            foreground: .white,
                            background: MusicColors.secondaryText
                        )
                    }
                }
            }
        }
        .padding(14)
        .background(MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

private struct TransferRecordsCard: View {
    let resources: [MultiCameraDownloadedResource]
    let itemCount: Int

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionTitle(title: "传输记录", subtitle: "最近通过 Wi-Fi 保存到本机的媒体")

            if resources.isEmpty {
                Text(itemCount > 0 ? "当前会话暂无新传输，下面已读取历史本地数据。" : "暂无传输记录")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(MusicColors.secondaryText)
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(MusicColors.surface)
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            } else {
                VStack(spacing: 8) {
                    ForEach(resources, id: \.localPath) { resource in
                        HStack(spacing: 10) {
                            Image(systemName: resource.kind == "video" ? "video.fill" : "photo.fill")
                                .foregroundColor(MusicColors.accent)
                                .frame(width: 32, height: 32)
                                .background(MusicColors.accentSoft)
                                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))

                            VStack(alignment: .leading, spacing: 3) {
                                Text(URL(fileURLWithPath: resource.localPath).lastPathComponent)
                                    .font(.system(size: 13, weight: .bold))
                                    .foregroundColor(MusicColors.primaryText)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                                Text("\(resource.cameraName) · \(formatDate(resource.downloadedAtUnixTimeMs))")
                                    .font(.system(size: 12, weight: .semibold))
                                    .foregroundColor(MusicColors.secondaryText)
                            }

                            Spacer()

                            Text(resource.kind == "video" ? "视频" : "照片")
                                .font(.system(size: 12, weight: .bold))
                                .foregroundColor(MusicColors.green)
                        }
                        .padding(10)
                        .background(MusicColors.surface)
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                    }
                }
            }
        }
        .padding(14)
        .background(MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private func formatDate(_ unixTimeMs: Int64) -> String {
        let date = Date(timeIntervalSince1970: TimeInterval(unixTimeMs) / 1000.0)
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter.string(from: date)
    }
}

private struct CameraResourceSelectionCard: View {
    let resources: [MultiCameraRemoteResource]
    let statusText: String
    @Binding var selectedIDs: Set<String>

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                SectionTitle(title: "相机数据记录", subtitle: statusText)
                Spacer()
                if resources.isEmpty == false {
                    Text("已选 \(selectedIDs.count)")
                        .font(.system(size: 12, weight: .bold))
                        .foregroundColor(MusicColors.accent)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(MusicColors.accentSoft)
                        .clipShape(Capsule())
                }
            }

            if resources.isEmpty {
                Text("点击“读取相机记录”后，这里会显示相机里的视频和照片。")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(MusicColors.secondaryText)
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(MusicColors.surface)
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            } else {
                VStack(spacing: 8) {
                    ForEach(resources) { resource in
                        CameraRemoteResourceRow(
                            resource: resource,
                            isSelected: selectedIDs.contains(resource.id),
                            onToggle: {
                                if selectedIDs.contains(resource.id) {
                                    selectedIDs.remove(resource.id)
                                } else {
                                    selectedIDs.insert(resource.id)
                                }
                            }
                        )
                    }
                }
            }
        }
        .padding(14)
        .background(MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .onChange(of: resources) { _ in
            selectedIDs = selectedIDs.filter { id in
                resources.contains { $0.id == id }
            }
        }
    }
}

private struct CameraRemoteResourceRow: View {
    let resource: MultiCameraRemoteResource
    let isSelected: Bool
    let onToggle: () -> Void

    var body: some View {
        Button(action: onToggle) {
            HStack(spacing: 10) {
                Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 20, weight: .bold))
                    .foregroundColor(isSelected ? MusicColors.accent : MusicColors.secondaryText)
                    .frame(width: 28)

                Image(systemName: resource.kind == "video" ? "video.fill" : "photo.fill")
                    .foregroundColor(MusicColors.accent)
                    .frame(width: 34, height: 34)
                    .background(MusicColors.accentSoft)
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))

                VStack(alignment: .leading, spacing: 3) {
                    Text(resource.fileName)
                        .font(.system(size: 13, weight: .bold))
                        .foregroundColor(MusicColors.primaryText)
                        .lineLimit(1)
                        .truncationMode(.middle)

                    Text("\(resource.kindText) · \(resource.fileSizeText) · \(resource.durationText)")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundColor(MusicColors.secondaryText)
                }

                Spacer()

                if resource.isDownloaded {
                    Text("已下载")
                        .font(.system(size: 12, weight: .bold))
                        .foregroundColor(MusicColors.green)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 5)
                        .background(MusicColors.green.opacity(0.12))
                        .clipShape(Capsule())
                }
            }
            .padding(10)
            .background(isSelected ? MusicColors.accentSoft : MusicColors.surface)
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .stroke(isSelected ? MusicColors.accent : MusicColors.border, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
    }
}

private struct InspectionItemRow: View {
    let item: MultiCameraInspectionItem
    let isSelected: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: item.mediaKindText == "视频" ? "video.fill" : "photo.fill")
                    .foregroundColor(isSelected ? .white : MusicColors.accent)
                    .frame(width: 30, height: 30)
                    .background(isSelected ? MusicColors.accent : MusicColors.accentSoft)
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))

                VStack(alignment: .leading, spacing: 3) {
                    Text(item.fileName)
                        .font(.system(size: 13, weight: .bold))
                        .foregroundColor(MusicColors.primaryText)
                        .lineLimit(1)
                        .truncationMode(.middle)

                    Text("\(item.mediaKindText) · \(item.fileSizeText) · \(item.durationText)")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundColor(MusicColors.secondaryText)
                        .lineLimit(1)
                }

                Spacer(minLength: 4)
            }

            HStack(spacing: 6) {
                InspectionBadge(title: item.hasSidecar ? "已解析" : "未解析", isReady: item.hasSidecar)
                InspectionBadge(title: item.hasGyro ? "有 IMU" : "无 IMU", isReady: item.hasGyro)
            }
        }
        .padding(12)
        .background(isSelected ? MusicColors.accentSoft : MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(isSelected ? MusicColors.accent : MusicColors.border, lineWidth: 1)
        )
    }
}

private struct InspectionDetailPanel: View {
    let item: MultiCameraInspectionItem
    let onDelete: () -> Void



    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("可视化解读")
                        .font(.system(size: 20, weight: .heavy))
                        .foregroundColor(MusicColors.primaryText)

                    Text(item.fileName)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(MusicColors.secondaryText)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }

                Spacer()

                Button(action: onDelete) {
                    Image(systemName: "trash.fill")
                        .font(.system(size: 16, weight: .bold))
                        .foregroundColor(.white)
                        .frame(width: 42, height: 42)
                        .background(MusicColors.accent)
                        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                }
            }

            HStack(spacing: 12) {
                SmallMetric(title: "时长", value: item.durationText)
                SmallMetric(title: "分辨率", value: item.resolutionText)
                SmallMetric(title: "IMU 点数", value: "\(item.gyroCount)")
            }

            VStack(alignment: .leading, spacing: 10) {
                SectionTitle(title: "完整性", subtitle: "媒体 / 伴随数据")

                HStack(spacing: 8) {
                    HealthPill(title: "原始媒体", value: item.fileSizeText, isReady: true)
                    HealthPill(title: "伴随 JSON", value: item.hasSidecar ? "完成" : "缺失", isReady: item.hasSidecar)
                }
            }

            GyroVisualization(item: item)

            metadataGrid

            VStack(alignment: .leading, spacing: 8) {
                SectionTitle(title: "保存位置", subtitle: "App Documents / instaumi")
                PathLine(title: "媒体", path: item.mediaURL.path)
                if let sidecarURL = item.sidecarURL {
                    PathLine(title: "解析", path: sidecarURL.path)
                }

            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))

    }

    private func formatMs(_ value: Double?) -> String {
        guard let value = value else { return "--" }
        return String(format: "%.2f ms", value)
    }

    private func formatDouble(_ value: Double?) -> String {
        guard let value = value else { return "--" }
        return String(format: "%.2f", value)
    }

    private var metadataGrid: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionTitle(title: "元数据", subtitle: item.sidecar?.serialNumber ?? "未读取到序列号")

            HStack(spacing: 8) {
                MetadataCell(title: "相机", value: item.sidecar?.cameraType ?? "--")
                MetadataCell(title: "固件", value: item.sidecar?.firmwareVersion ?? "--")
                MetadataCell(title: "Rolling", value: formatMs(item.sidecar?.rollingShutterTimeMs))
            }

            HStack(spacing: 8) {
                MetadataCell(title: "首帧", value: formatDouble(item.sidecar?.firstFrameTimestampMs))
                MetadataCell(title: "GPS", value: formatDouble(item.sidecar?.firstGpsTimestampMs))
                MetadataCell(title: "Gyro 偏移", value: formatDouble(item.sidecar?.gyroTimestampAdjustMs))
            }
        }
    }
}

private struct GyroVisualization: View {
    let item: MultiCameraInspectionItem

    private var values: [(String, Double)] {
        let sample = item.sidecar?.gyro?.lastSample ?? item.sidecar?.gyro?.firstSample
        guard let sample = sample else { return [] }
        return [
            ("Gx", sample.gravityX),
            ("Gy", sample.gravityY),
            ("Gz", sample.gravityZ),
            ("Rx", sample.rotationX),
            ("Ry", sample.rotationY),
            ("Rz", sample.rotationZ)
        ]
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionTitle(title: "IMU 预览", subtitle: item.hasGyro ? "末端样本幅值" : "暂无 IMU 数据")

            if values.isEmpty {
                Text("未发现可视化样本")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(MusicColors.secondaryText)
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(MusicColors.controlFill)
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            } else {
                VStack(spacing: 8) {
                    ForEach(values, id: \.0) { label, value in
                        HStack(spacing: 8) {
                            Text(label)
                                .font(.system(size: 12, weight: .bold, design: .monospaced))
                                .foregroundColor(MusicColors.secondaryText)
                                .frame(width: 28, alignment: .leading)

                            GeometryReader { proxy in
                                let ratio = min(abs(value) / 10.0, 1.0)
                                ZStack(alignment: .leading) {
                                    Capsule()
                                        .fill(MusicColors.controlFill)
                                    Capsule()
                                        .fill(value >= 0 ? MusicColors.accent : MusicColors.sensorAccent)
                                        .frame(width: max(proxy.size.width * ratio, 4))
                                }
                            }
                            .frame(height: 10)

                            Text(String(format: "%.2f", value))
                                .font(.system(size: 12, weight: .bold, design: .monospaced))
                                .foregroundColor(MusicColors.primaryText)
                                .frame(width: 58, alignment: .trailing)
                        }
                    }
                }
                .padding(12)
                .background(MusicColors.surface)
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            }
        }
    }
}

private struct InspectionBadge: View {
    let title: String
    let isReady: Bool

    var body: some View {
        Text(title)
            .font(.system(size: 10, weight: .bold))
            .foregroundColor(isReady ? MusicColors.green : MusicColors.secondaryText)
            .padding(.horizontal, 7)
            .padding(.vertical, 4)
            .background((isReady ? MusicColors.green : MusicColors.secondaryText).opacity(0.12))
            .clipShape(Capsule())
    }
}

private struct HealthPill: View {
    let title: String
    let value: String
    let isReady: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.system(size: 11, weight: .bold))
                .foregroundColor(MusicColors.secondaryText)
                .lineLimit(1)

            Text(value)
                .font(.system(size: 14, weight: .heavy))
                .foregroundColor(isReady ? MusicColors.green : MusicColors.orange)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(MusicColors.surface)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

private struct MetadataCell: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.system(size: 11, weight: .bold))
                .foregroundColor(MusicColors.secondaryText)
            Text(value)
                .font(.system(size: 13, weight: .bold))
                .foregroundColor(MusicColors.primaryText)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(MusicColors.surface)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

private struct PathLine: View {
    let title: String
    let path: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(title)
                .font(.system(size: 12, weight: .bold))
                .foregroundColor(MusicColors.secondaryText)
                .frame(width: 40, alignment: .leading)
            Text(shortPath(path))
                .font(.system(size: 12, weight: .semibold, design: .monospaced))
                .foregroundColor(MusicColors.primaryText)
                .lineLimit(1)
                .truncationMode(.middle)
        }
        .padding(10)
        .background(MusicColors.surface)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private func shortPath(_ path: String) -> String {
        if let range = path.range(of: "/Documents/") {
            return String(path[range.upperBound...])
        }
        return path
    }
}

private struct EmptyOperation: View {
    let feature: WorkbenchFeature
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .center, spacing: 18) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(value)
                        .font(.system(size: 58, weight: .heavy, design: .rounded))
                        .foregroundColor(MusicColors.primaryText)

                    Text(feature.title)
                        .font(.system(size: 18, weight: .bold))
                        .foregroundColor(MusicColors.secondaryText)
                }

                Spacer()

                Image(systemName: feature.icon)
                    .font(.system(size: 54, weight: .bold))
                    .foregroundColor(MusicColors.accent)
                    .frame(width: 108, height: 108)
                    .background(MusicColors.accentSoft)
                    .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            }

            Text("暂未接入")
                .font(.system(size: 17, weight: .bold))
                .foregroundColor(MusicColors.secondaryText)
                .padding(.vertical, 14)
                .frame(maxWidth: .infinity)
                .background(MusicColors.controlFill)
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        }
        .padding(22)
        .background(MusicColors.surface)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(MusicColors.border, lineWidth: 1)
        )
    }
}

enum CollectionStatus {
    case idle
    case running
    case paused

    var title: String {
        switch self {
        case .idle:
            return "待启动"
        case .running:
            return "采集中"
        case .paused:
            return "已暂停"
        }
    }

    var tint: Color {
        switch self {
        case .idle:
            return MusicColors.secondaryText
        case .running:
            return MusicColors.green
        case .paused:
            return MusicColors.orange
        }
    }
}

private struct StatusBadge: View {
    let status: CollectionStatus

    var body: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(status.tint)
                .frame(width: 10, height: 10)

            Text(status.title)
                .font(.system(size: 14, weight: .bold))
                .foregroundColor(status.tint)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(status.tint.opacity(0.12))
        .clipShape(Capsule())
    }
}

private struct ActionButtonLabel: View {
    let title: String
    let icon: String
    let foreground: Color
    let background: Color

    var body: some View {
        Label(title, systemImage: icon)
            .font(.system(size: 17, weight: .bold))
            .lineLimit(1)
            .minimumScaleFactor(0.75)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 16)
            .foregroundColor(foreground)
            .background(background)
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .stroke(MusicColors.buttonStroke, lineWidth: 1)
            )
    }
}

private struct SegmentCountPanel: View {
    let completedCount: Int
    let currentSegmentIndex: Int
    let isRunning: Bool

    private var statusText: String {
        if isRunning {
            return "当前第 \(currentSegmentIndex) 段进行中，点“分段结束”后计入次数"
        }

        return "下一次启动将记录第 \(completedCount + 1) 段"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center, spacing: 16) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("已完成分段次数")
                        .font(.system(size: 14, weight: .bold))
                        .foregroundColor(MusicColors.secondaryText)

                    Text("启动 + 结束算 1 次")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundColor(MusicColors.mutedText)
                }

                Spacer(minLength: 8)

                Text("\(completedCount)")
                    .font(.system(size: 52, weight: .heavy, design: .rounded))
                    .foregroundColor(MusicColors.accent)
                    .monospacedDigit()
                    .minimumScaleFactor(0.65)

                Text("次")
                    .font(.system(size: 18, weight: .heavy))
                    .foregroundColor(MusicColors.accent)
                    .padding(.top, 18)
            }

            Text(statusText)
                .font(.system(size: 12, weight: .bold))
                .foregroundColor(isRunning ? MusicColors.green : MusicColors.secondaryText)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(MusicColors.surface)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(isRunning ? MusicColors.green : MusicColors.border, lineWidth: isRunning ? 2 : 1)
        )
        .padding(.top, 2)
    }
}

private struct InstantTimestampButton<Label: View>: View {
    let disabled: Bool
    let action: (CollectionInputTimestamp) -> Void
    private let label: () -> Label

    init(
        disabled: Bool,
        action: @escaping (CollectionInputTimestamp) -> Void,
        @ViewBuilder label: @escaping () -> Label
    ) {
        self.disabled = disabled
        self.action = action
        self.label = label
    }

    var body: some View {
        label()
            .contentShape(Rectangle())
            .overlay(
                TouchDownTimestampOverlay(
                    disabled: disabled,
                    onTouchDown: action
                )
            )
            .accessibilityAddTraits(.isButton)
            .accessibilityAction {
                guard disabled == false else {
                    return
                }
                action(CollectionInputTimestamp.now())
            }
    }
}

private struct TouchDownTimestampOverlay: UIViewRepresentable {
    let disabled: Bool
    let onTouchDown: (CollectionInputTimestamp) -> Void

    func makeUIView(context: Context) -> TouchDownTimestampView {
        let view = TouchDownTimestampView()
        view.backgroundColor = .clear
        view.isMultipleTouchEnabled = false
        view.isExclusiveTouch = true
        view.onTouchDown = onTouchDown
        view.disabled = disabled
        return view
    }

    func updateUIView(_ uiView: TouchDownTimestampView, context: Context) {
        uiView.disabled = disabled
        uiView.onTouchDown = onTouchDown
    }
}

private enum PedalControlMode: String, CaseIterable, Identifiable {
    case slamCalibration
    case segmentCollection

    var id: String { rawValue }

    var title: String {
        switch self {
        case .slamCalibration:
            return "SLAM标定"
        case .segmentCollection:
            return "分段采集"
        }
    }

    var shortPressHint: String {
        switch self {
        case .slamCalibration:
            return "添加SLAM"
        case .segmentCollection:
            return "分段启停"
        }
    }
}

private enum HardwareKeyCommand {
    case pedalShortPress(CollectionInputTimestamp)
    case pedalLongPress(CollectionInputTimestamp)
    case startTotal
    case endTotal
    case startSegment
    case endSegment
}

private struct HardwareKeyCommandBridge: UIViewControllerRepresentable {
    let onCommand: (HardwareKeyCommand) -> Void

    func makeUIViewController(context: Context) -> HardwareKeyCommandViewController {
        let controller = HardwareKeyCommandViewController()
        controller.onCommand = onCommand
        return controller
    }

    func updateUIViewController(_ uiViewController: HardwareKeyCommandViewController, context: Context) {
        uiViewController.onCommand = onCommand
        uiViewController.becomeFirstResponderSoon()
    }
}

private final class HardwareKeyCommandViewController: UIViewController {
    var onCommand: ((HardwareKeyCommand) -> Void)?
    private let pedalLongPressThreshold: TimeInterval = 1.5
    private let pedalShortPressCooldown: TimeInterval = 0.08
    private var pedalPressBeganAt: TimeInterval?
    private var lastPedalShortPressAt: TimeInterval = 0
    private var pedalLongPressWorkItem: DispatchWorkItem?
    private var pedalLongPressFired = false

    override var canBecomeFirstResponder: Bool {
        true
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        becomeFirstResponderSoon()
    }

    override var keyCommands: [UIKeyCommand]? {
        [
            makeCommand(input: "1", action: #selector(startTotal), title: "1：总启动"),
            makeCommand(input: "2", action: #selector(endTotal), title: "2：总结束"),
            makeCommand(input: "3", action: #selector(startSegment), title: "3：分段启动"),
            makeCommand(input: "4", action: #selector(endSegment), title: "4：分段结束"),
        ]
    }

    func becomeFirstResponderSoon() {
        DispatchQueue.main.async { [weak self] in
            self?.becomeFirstResponder()
        }
    }

    private func makeCommand(input: String, action: Selector, title: String) -> UIKeyCommand {
        let command = UIKeyCommand(input: input, modifierFlags: [], action: action)
        command.discoverabilityTitle = title
        return command
    }

    override func pressesBegan(_ presses: Set<UIPress>, with event: UIPressesEvent?) {
        if presses.contains(where: isPedalPress) {
            beginPedalPress()
            return
        }
        super.pressesBegan(presses, with: event)
    }

    override func pressesEnded(_ presses: Set<UIPress>, with event: UIPressesEvent?) {
        if presses.contains(where: isPedalPress) {
            endPedalPress(cancelled: false)
            return
        }
        super.pressesEnded(presses, with: event)
    }

    override func pressesCancelled(_ presses: Set<UIPress>, with event: UIPressesEvent?) {
        if presses.contains(where: isPedalPress) {
            endPedalPress(cancelled: true)
            return
        }
        super.pressesCancelled(presses, with: event)
    }

    private func isPedalPress(_ press: UIPress) -> Bool {
        press.key?.charactersIgnoringModifiers == " "
    }

    private func beginPedalPress() {
        guard pedalPressBeganAt == nil else {
            return
        }

        pedalPressBeganAt = ProcessInfo.processInfo.systemUptime
        pedalLongPressFired = false
        let workItem = DispatchWorkItem { [weak self] in
            guard let self,
                  let pressBeganAt = self.pedalPressBeganAt,
                  self.pedalLongPressFired == false else {
                return
            }
            self.pedalLongPressFired = true
            self.onCommand?(.pedalLongPress(.hardwareKeyDown(pressTimestamp: pressBeganAt)))
        }
        pedalLongPressWorkItem = workItem
        let delay = DispatchTimeInterval.milliseconds(Int(pedalLongPressThreshold * 1000))
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: workItem)
    }

    private func endPedalPress(cancelled: Bool) {
        let fired = pedalLongPressFired
        let pressBeganAt = pedalPressBeganAt ?? ProcessInfo.processInfo.systemUptime
        pedalPressBeganAt = nil
        pedalLongPressWorkItem?.cancel()
        pedalLongPressWorkItem = nil
        pedalLongPressFired = false

        guard cancelled == false, fired == false else {
            return
        }
        let now = ProcessInfo.processInfo.systemUptime
        guard now - lastPedalShortPressAt >= pedalShortPressCooldown else {
            return
        }
        lastPedalShortPressAt = now
        onCommand?(.pedalShortPress(.hardwareKeyDown(pressTimestamp: pressBeganAt)))
    }

    @objc private func startTotal() {
        onCommand?(.startTotal)
    }

    @objc private func endTotal() {
        onCommand?(.endTotal)
    }

    @objc private func startSegment() {
        onCommand?(.startSegment)
    }

    @objc private func endSegment() {
        onCommand?(.endSegment)
    }
}

private final class TouchDownTimestampView: UIView {
    var disabled = false
    var onTouchDown: ((CollectionInputTimestamp) -> Void)?
    private var hasFiredForCurrentTouch = false

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        guard disabled == false, hasFiredForCurrentTouch == false, let touch = touches.first else {
            return
        }

        hasFiredForCurrentTouch = true
        onTouchDown?(CollectionInputTimestamp.touchBegan(touchTimestamp: touch.timestamp))
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {
        hasFiredForCurrentTouch = false
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) {
        hasFiredForCurrentTouch = false
    }
}

private struct SmallMetric: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.system(size: 12, weight: .bold))
                .foregroundColor(MusicColors.secondaryText)
                .lineLimit(1)

            Text(value)
                .font(.system(size: 17, weight: .heavy, design: .rounded))
                .foregroundColor(MusicColors.primaryText)
                .lineLimit(1)
                .minimumScaleFactor(0.68)
        }
        .padding(12)
        .frame(maxWidth: .infinity, minHeight: 68, alignment: .leading)
        .background(MusicColors.metricFill)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .stroke(MusicColors.border.opacity(0.75), lineWidth: 1)
        )
    }
}

enum MusicColors {
    static let background = Color(red: 0.945, green: 0.960, blue: 0.978)
    static let sidebar = Color(red: 0.925, green: 0.943, blue: 0.965)
    static let surface = Color(red: 0.990, green: 0.993, blue: 0.997)
    static let primaryText = Color(red: 0.055, green: 0.075, blue: 0.100)
    static let secondaryText = Color(red: 0.305, green: 0.360, blue: 0.425)
    static let mutedText = Color(red: 0.520, green: 0.570, blue: 0.630)
    static let border = Color(red: 0.800, green: 0.850, blue: 0.900)
    static let accent = Color(red: 0.035, green: 0.420, blue: 0.760)
    static let sensorAccent = Color(red: 0.000, green: 0.610, blue: 0.660)
    static let hotPink = sensorAccent
    static let accentSoft = Color(red: 0.870, green: 0.930, blue: 0.985)
    static let controlFill = Color(red: 0.925, green: 0.950, blue: 0.975)
    static let metricFill = Color(red: 0.955, green: 0.972, blue: 0.988)
    static let buttonStroke = Color.white.opacity(0.35)
    static let green = Color(red: 0.000, green: 0.500, blue: 0.360)
    static let orange = Color(red: 0.780, green: 0.330, blue: 0.070)
}

private struct MultiCameraCaptureContainer: UIViewControllerRepresentable {
    var onClose: (() -> Void)?

    func makeCoordinator() -> Coordinator {
        Coordinator(onClose: onClose)
    }

    func makeUIViewController(context: Context) -> UINavigationController {
        let controller = MultiCameraCaptureViewController()
        if onClose != nil {
            controller.navigationItem.rightBarButtonItem = UIBarButtonItem(
                title: "关闭",
                style: .done,
                target: context.coordinator,
                action: #selector(Coordinator.close)
            )
        }

        return UINavigationController(rootViewController: controller)
    }

    func updateUIViewController(_ uiViewController: UINavigationController, context: Context) {
        context.coordinator.onClose = onClose
    }

    final class Coordinator: NSObject {
        var onClose: (() -> Void)?

        init(onClose: (() -> Void)?) {
            self.onClose = onClose
        }

        @objc func close() {
            onClose?()
        }
    }
}
