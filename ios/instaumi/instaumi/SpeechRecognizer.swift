import Foundation
import Speech
import AVFoundation
import Combine

final class SpeechRecognizer: ObservableObject {
    @Published var isListening = false
    @Published var recognizedText = ""
    @Published var detectedCommand: VoiceCommand? = nil

    enum VoiceCommand: Equatable {
        case startTotal
        case endTotal
        case startClockSync
        case endClockSync
        case startSegment
        case endSegment
        case start
        case pause
        case finish
    }

    private let speechRecognizer = SFSpeechRecognizer(locale: Locale(identifier: "zh-CN"))!
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private let audioEngine = AVAudioEngine()

    func requestAuthorization(completion: @escaping (Bool) -> Void) {
        SFSpeechRecognizer.requestAuthorization { status in
            DispatchQueue.main.async {
                completion(status == .authorized)
            }
        }
    }

    private var lastProcessedIndex = 0

    func startListening() {
        lastProcessedIndex = 0
        recognizedText = ""
        // ... rest of startListening logic
        guard !audioEngine.isRunning else { return }

        // Setup audio session
        let audioSession = AVAudioSession.sharedInstance()
        do {
            try audioSession.setCategory(.record, mode: .measurement, options: .duckOthers)
            try audioSession.setActive(true, options: .notifyOthersOnDeactivation)
        } catch {
            print("Failed to setup audio session: \(error)")
            return
        }

        recognitionRequest = SFSpeechAudioBufferRecognitionRequest()
        guard let recognitionRequest = recognitionRequest else { return }

        recognitionRequest.shouldReportPartialResults = true

        let inputNode = audioEngine.inputNode
        let recordingFormat = inputNode.outputFormat(forBus: 0)
        inputNode.installTap(onBus: 0, bufferSize: 1024, format: recordingFormat) { buffer, _ in
            self.recognitionRequest?.append(buffer)
        }

        audioEngine.prepare()
        do {
            try audioEngine.start()
            isListening = true
        } catch {
            print("AudioEngine failed to start: \(error)")
            return
        }

        recognitionTask = speechRecognizer.recognitionTask(with: recognitionRequest) { [weak self] result, error in
            guard let self = self else { return }
            var isFinal = false

            if let result = result {
                let text = result.bestTranscription.formattedString
                DispatchQueue.main.async {
                    self.recognizedText = text
                    self.processTextForCommands(text)
                }
                isFinal = result.isFinal
            }

            if error != nil || isFinal {
                self.audioEngine.stop()
                inputNode.removeTap(onBus: 0)
                self.recognitionRequest = nil
                self.recognitionTask = nil
                DispatchQueue.main.async {
                    self.isListening = false
                }
            }
        }
    }

    func stopListening() {
        audioEngine.stop()
        recognitionRequest?.endAudio()
        isListening = false
    }

    private func processTextForCommands(_ text: String) {
        let newText = String(text.dropFirst(lastProcessedIndex))
            .lowercased()
            .replacingOccurrences(of: " ", with: "")
        if newText.isEmpty { return }

        var triggered = false
        if containsStart(newText) && (newText.contains("总") || newText.contains("采集") || newText.contains("拍摄") || newText.contains("录像")) {
            detectedCommand = .startTotal
            triggered = true
        } else if containsEnd(newText) && (newText.contains("总") || newText.contains("采集") || newText.contains("拍摄") || newText.contains("录像")) {
            detectedCommand = .endTotal
            triggered = true
        } else if containsStart(newText) && (newText.contains("对齐") || newText.contains("同步") || newText.contains("校准")) {
            detectedCommand = .startClockSync
            triggered = true
        } else if containsEnd(newText) && (newText.contains("对齐") || newText.contains("同步") || newText.contains("校准")) {
            detectedCommand = .endClockSync
            triggered = true
        } else if containsStart(newText) && (newText.contains("分段") || newText.contains("片段") || newText.contains("段落")) {
            detectedCommand = .startSegment
            triggered = true
        } else if containsEnd(newText) && (newText.contains("分段") || newText.contains("片段") || newText.contains("段落")) {
            detectedCommand = .endSegment
            triggered = true
        } else if containsStart(newText) {
            detectedCommand = .start
            triggered = true
        } else if newText.contains("暂停") {
            detectedCommand = .pause
            triggered = true
        } else if containsEnd(newText) {
            detectedCommand = .finish
            triggered = true
        }

        if triggered {
            lastProcessedIndex = text.count
            resetCommandAfterDelay()
        }
    }

    private func containsStart(_ text: String) -> Bool {
        text.contains("开始") || text.contains("启动") || text.contains("开启")
    }

    private func containsEnd(_ text: String) -> Bool {
        text.contains("结束") || text.contains("停止") || text.contains("完成") || text.contains("关闭")
    }

    private func resetCommandAfterDelay() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
            self.detectedCommand = nil
        }
    }
}
