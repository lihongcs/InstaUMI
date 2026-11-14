import AVFoundation
import INSCoreMedia
import SwiftUI
import UIKit

struct InsvPlayerView: UIViewRepresentable {
    let url: URL
    let playbackRate: Double
    let isPlaying: Bool
    let seekPositionMs: Int64
    let seekSerial: Int

    func makeUIView(context: Context) -> InstaMediaPlayerContainerView {
        InstaMediaPlayerContainerView(
            url: url,
            playbackRate: playbackRate,
            isPlaying: isPlaying,
            seekPositionMs: seekPositionMs,
            seekSerial: seekSerial
        )
    }

    func updateUIView(_ uiView: InstaMediaPlayerContainerView, context: Context) {
        uiView.update(
            url: url,
            playbackRate: playbackRate,
            isPlaying: isPlaying,
            seekPositionMs: seekPositionMs,
            seekSerial: seekSerial
        )
    }
}

final class InstaMediaPlayerContainerView: UIView {
    private var url: URL
    private var playbackRate: Double
    private var isPlaying: Bool
    private var seekPositionMs: Int64
    private var seekSerial: Int
    private var lastAppliedSeekSerial: Int
    private var sdkPlayer: INSPlayer?
    private var sdkRenderView: INSRenderView?
    private var avPlayer: AVPlayer?
    private var avPlayerLayer: AVPlayerLayer?
    private var hasStartedPlayback = false

    private let messageLabel: UILabel = {
        let label = UILabel()
        label.textColor = .white
        label.font = .systemFont(ofSize: 13, weight: .semibold)
        label.textAlignment = .center
        label.numberOfLines = 0
        label.isHidden = true
        return label
    }()

    init(url: URL, playbackRate: Double, isPlaying: Bool, seekPositionMs: Int64, seekSerial: Int) {
        self.url = url
        self.playbackRate = playbackRate
        self.isPlaying = isPlaying
        self.seekPositionMs = seekPositionMs
        self.seekSerial = seekSerial
        self.lastAppliedSeekSerial = seekSerial
        super.init(frame: .zero)
        backgroundColor = .black
        addSubview(messageLabel)
    }

    required init?(coder: NSCoder) {
        return nil
    }

    deinit {
        teardown()
    }

    func update(url: URL, playbackRate: Double, isPlaying: Bool, seekPositionMs: Int64, seekSerial: Int) {
        let urlDidChange = self.url != url
        self.url = url
        self.playbackRate = playbackRate
        self.isPlaying = isPlaying
        self.seekPositionMs = seekPositionMs
        self.seekSerial = seekSerial

        if urlDidChange {
            teardown()
            lastAppliedSeekSerial = seekSerial
            setNeedsLayout()
            return
        }

        if seekSerial != lastAppliedSeekSerial {
            seek(to: seekPositionMs)
            lastAppliedSeekSerial = seekSerial
        }

        applyPlaybackState()
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        messageLabel.frame = bounds.insetBy(dx: 16, dy: 16)
        sdkRenderView?.frame = bounds
        avPlayerLayer?.frame = bounds

        guard bounds.width > 4, bounds.height > 4, hasStartedPlayback == false else {
            return
        }
        startPlayback()
    }

    private func startPlayback() {
        hasStartedPlayback = true
        clearMessage()

        let ext = url.pathExtension.lowercased()
        if ["mp4", "mov", "m4v", "lrv"].contains(ext) {
            startSystemPlayback()
        } else if ["insv"].contains(ext) {
            startInstaPlayback()
        } else {
            showMessage("当前格式暂不支持直接播放：\(url.lastPathComponent)")
        }
    }

    private func startSystemPlayback() {
        let item = AVPlayerItem(url: url)
        item.audioTimePitchAlgorithm = .timeDomain
        let player = AVPlayer(playerItem: item)
        player.automaticallyWaitsToMinimizeStalling = false
        let layer = AVPlayerLayer(player: player)
        layer.videoGravity = .resizeAspect
        layer.frame = bounds
        self.layer.addSublayer(layer)

        avPlayer = player
        avPlayerLayer = layer
        applyPlaybackState()
    }

    private func startInstaPlayback() {
        // 配置音频会话，防止 INSPlayer 内部 AudioQueue 初始化时卡死主线程
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback, options: [.mixWithOthers])
        try? AVAudioSession.sharedInstance().setActive(true)

        let parser = INSVideoInfoParser(url: url)
        guard parser.open() else {
            showMessage("无法打开影石视频：\(url.lastPathComponent)")
            return
        }

        let renderView = INSRenderView(frame: bounds, renderType: parser.supposedRenderType)
        renderView.backgroundColor = .black
        renderView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        insertSubview(renderView, belowSubview: messageLabel)
        renderView.startDisplayLink()

        let player = INSPlayer()
        player.setPlayerType(false)
        player.displayDelegate = renderView
        player.setDataSource(url)
        player.prepare()
        player.setPlayRate(normalizedPlaybackRate)

        sdkRenderView = renderView
        sdkPlayer = player

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
            self.applyPlaybackState()
        }
    }

    private var normalizedPlaybackRate: Double {
        min(max(playbackRate, 0.25), 8.0)
    }

    private func applyPlaybackState() {
        let rate = Float(normalizedPlaybackRate)

        if let player = avPlayer {
            if isPlaying {
                player.playImmediately(atRate: rate)
            } else {
                player.pause()
            }
        }

        if let player = sdkPlayer {
            player.setPlayRate(normalizedPlaybackRate)
            if isPlaying {
                player.play()
            } else {
                player.pause()
            }
        }
    }

    private func seek(to positionMs: Int64) {
        let safePosition = max(positionMs, 0)

        if let player = avPlayer {
            let time = CMTime(value: safePosition, timescale: 1000)
            player.seek(to: time, toleranceBefore: .zero, toleranceAfter: .zero) { [weak self] _ in
                self?.applyPlaybackState()
            }
        }

        if let player = sdkPlayer {
            player.seek(safePosition, toKey: true)
            applyPlaybackState()
        }
    }

    private func teardown() {
        sdkPlayer?.shutdown()
        sdkPlayer = nil
        sdkRenderView?.stopDisplayLink()
        sdkRenderView?.destroyRender()
        sdkRenderView?.removeFromSuperview()
        sdkRenderView = nil

        avPlayer?.pause()
        avPlayer = nil
        avPlayerLayer?.removeFromSuperlayer()
        avPlayerLayer = nil

        hasStartedPlayback = false
        clearMessage()
    }

    private func showMessage(_ message: String) {
        messageLabel.text = message
        messageLabel.isHidden = false
        bringSubviewToFront(messageLabel)
    }

    private func clearMessage() {
        messageLabel.text = nil
        messageLabel.isHidden = true
    }
}
