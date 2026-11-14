import SwiftUI
import CoreImage.CIFilterBuiltins
import Combine

final class DisplayLinkTimer: ObservableObject {
    @Published var currentTimeMs: Int64 = 0
    private var displayLink: CADisplayLink?

    func start() {
        if displayLink == nil {
            displayLink = CADisplayLink(target: self, selector: #selector(update))
            // Request max possible frame rate (up to 120Hz on ProMotion displays)
            displayLink?.preferredFramesPerSecond = 120
            displayLink?.add(to: .main, forMode: .common)
        }
    }

    func stop() {
        displayLink?.invalidate()
        displayLink = nil
    }

    @objc private func update(link: CADisplayLink) {
        currentTimeMs = Int64(Date().timeIntervalSince1970 * 1000)
    }
}

struct ClockSyncOperation: View {
    @ObservedObject var cameraManager: MultiCameraCoordinator
    @ObservedObject var collectionSession: CollectionSessionController

    @StateObject private var timer = DisplayLinkTimer()
    @StateObject private var speechRecognizer = SpeechRecognizer()
    @State private var showingPermissionAlert = false
    @State private var actionMessage = "总采集启动后，可在这里记录时钟对齐启动/结束时间戳。"

    let context = CIContext()
    let filter = CIFilter.qrCodeGenerator()

    var body: some View {
        VStack(spacing: 20) {
            Text("多相机视觉时间同步 (Visual Sync)")
                .font(.system(size: 28, weight: .bold))
                .foregroundColor(MusicColors.primaryText)
                .padding(.top, 20)

            Text("请将所有相机的镜头对准此屏幕。\n底层的 CADisplayLink 会以最高刷新率（最高 120Hz）更新绝对 UNIX 毫秒时间戳。")
                .font(.system(size: 15))
                .foregroundColor(MusicColors.secondaryText)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 20)

            Spacer()

            // Giant Time Text for human/OCR fallback
            Text("\(timer.currentTimeMs)")
                .font(.system(size: 72, weight: .heavy, design: .monospaced))
                .foregroundColor(MusicColors.hotPink)
                .minimumScaleFactor(0.5)
                .lineLimit(1)
                .padding(.horizontal, 20)

            // Dynamic QR Code
            if let qrImage = generateQRCode(from: String(timer.currentTimeMs)) {
                Image(uiImage: qrImage)
                    .interpolation(.none)
                    .resizable()
                    .scaledToFit()
                    .frame(width: 320, height: 320)
                    .padding(24)
                    .background(Color.white)
                    .cornerRadius(16)
                    .shadow(color: Color.black.opacity(0.1), radius: 10, x: 0, y: 4)
            } else {
                Rectangle()
                    .fill(Color.gray.opacity(0.2))
                    .frame(width: 320, height: 320)
                    .cornerRadius(16)
            }

            Spacer()

            // Voice Assistant Toggle
            HStack(spacing: 6) {
                Image(systemName: speechRecognizer.isListening ? "mic.fill" : "mic.slash.fill")
                    .foregroundColor(speechRecognizer.isListening ? MusicColors.green : MusicColors.secondaryText)
                Text("语音控制")
                    .font(.system(size: 15, weight: .bold))
                    .foregroundColor(MusicColors.primaryText)
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
            .padding(.bottom, 4)

            if speechRecognizer.isListening {
                Text(speechRecognizer.recognizedText.isEmpty ? "🎙️ 正在聆听..." : "🎙️ \(speechRecognizer.recognizedText)")
                    .font(.system(size: 14, weight: .medium))
                    .foregroundColor(MusicColors.green)
                    .lineLimit(2)
                    .padding(.bottom, 10)
            }

            Text(actionMessage)
                .font(.system(size: 14, weight: .semibold))
                .foregroundColor(actionMessage.contains("请先") ? .red : MusicColors.secondaryText)
                .multilineTextAlignment(.center)

            HStack(spacing: 16) {
                Button {
                    startClockSync()
                } label: {
                    Text("对齐启动")
                        .font(.system(size: 18, weight: .bold))
                        .foregroundColor(.white)
                        .frame(maxWidth: .infinity)
                        .frame(height: 56)
                        .background(collectionSession.isTotalRunning && collectionSession.isClockSyncRunning == false ? MusicColors.hotPink : MusicColors.secondaryText)
                        .cornerRadius(12)
                }
                .disabled(collectionSession.isTotalRunning == false || collectionSession.isClockSyncRunning)

                Button {
                    endClockSync()
                } label: {
                    Text("对齐结束")
                        .font(.system(size: 18, weight: .bold))
                        .foregroundColor(MusicColors.accent)
                        .frame(maxWidth: .infinity)
                        .frame(height: 56)
                        .background(MusicColors.accentSoft)
                        .cornerRadius(12)
                }
                .disabled(collectionSession.isClockSyncRunning == false)
                .opacity(collectionSession.isClockSyncRunning ? 1 : 0.45)
            }
            .padding(.horizontal, 24)
            .padding(.bottom, 20)
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.white)
        .cornerRadius(16)
        .shadow(color: Color.black.opacity(0.04), radius: 8, x: 0, y: 4)
        .alert(isPresented: $showingPermissionAlert) {
            Alert(title: Text("需要权限"), message: Text("请在系统设置中允许 App 访问麦克风与语音识别。"), dismissButton: .default(Text("好的")))
        }
        .onChange(of: speechRecognizer.detectedCommand) { command in
            guard let command = command else { return }
            switch command {
            case .startClockSync:
                startClockSync()
            case .endClockSync:
                endClockSync()
            case .start:
                startClockSync()
            case .pause:
                endClockSync()
            case .finish:
                endClockSync()
            case .startTotal, .endTotal, .startSegment, .endSegment:
                actionMessage = "当前页面只处理时钟对齐语音：对齐启动、对齐结束"
            }
        }
        .onAppear {
            timer.start()
        }
        .onDisappear {
            timer.stop()
            speechRecognizer.stopListening()
        }
    }

    private func generateQRCode(from string: String) -> UIImage? {
        let data = Data(string.utf8)
        filter.setValue(data, forKey: "inputMessage")
        filter.setValue("L", forKey: "inputCorrectionLevel")

        guard let outputImage = filter.outputImage else { return nil }

        let transform = CGAffineTransform(scaleX: 10, y: 10)
        let scaledImage = outputImage.transformed(by: transform)

        guard let cgImage = context.createCGImage(scaledImage, from: scaledImage.extent) else { return nil }

        return UIImage(cgImage: cgImage)
    }

    private func startClockSync() {
        let result = collectionSession.startClockSync(connectedCameraCount: cameraManager.connectedCount)
        actionMessage = result.message
    }

    private func endClockSync() {
        let result = collectionSession.endClockSync(connectedCameraCount: cameraManager.connectedCount)
        actionMessage = result.message
    }
}
