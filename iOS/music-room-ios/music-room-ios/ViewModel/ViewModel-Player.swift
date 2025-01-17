//
//  ViewModel-Player.swift
//  music-room-ios
//
//  Created by Nikita Arutyunov on 13.07.2022.
//

import Foundation
import SwiftUI
import MediaPlayer
import AVFoundation

extension ViewModel {
    func playCurrentTrack() {
        guard
            let currentTrackFile = currentTrackFile,
            let currentTrackURL = URL(string: currentTrackFile.file)
        else {
            return
        }
        
        guard
            (player.currentItem?.asset as? AVURLAsset)?.url != currentTrackURL
        else {
            return player.play()
        }
        
        let urlAsset = AVURLAsset(
            url: currentTrackURL,
            options: [
                AVURLAssetPreferPreciseDurationAndTimingKey: true,
            ]
        )
        
        let playerItem = AVPlayerItem(asset: urlAsset)
        
        do {
            let audioSession = AVAudioSession.sharedInstance()
            
            try audioSession.setCategory(
                .playback,
                mode: .default,
                policy: .longFormAudio,
                options: []
            )
            
            try audioSession.setActive(true)
        } catch {
            debugPrint(error.localizedDescription)
        }
        
        if let progress = currentPlayerContent?.progress {
            let progress = NSDecimalNumber(decimal: progress).doubleValue
            let timeScale = CMTimeScale(44100)
            let time = CMTime(seconds: progress, preferredTimescale: timeScale)
            
            guard time < playerItem.duration else { return }
            
            player.replaceCurrentItem(with: playerItem)
            
            updateNowPlayingInfo()
            
            player.automaticallyWaitsToMinimizeStalling = false
            
            playerItemStatusObserver = player.currentItem?.observe(\.status) {
                [unowned self] (playerItem, _) in
                
                guard
                    playerItem.status == .readyToPlay
                else {
                    if let error = playerItem.error {
                        debugPrint(error)
                    }
                    
                    return
                }
                
                @MainActor
                func seek() {
                    guard
                        let currentSessionTrackProgress = currentPlayerContent?.progress
                    else {
                        return player.play()
                    }
                    
                    Task.detached { [unowned self] in
                        let progress = NSDecimalNumber(decimal: currentSessionTrackProgress).doubleValue
                        let timeScale = CMTimeScale(44100)
                        let time = CMTime(seconds: progress, preferredTimescale: timeScale)
                        
                        let isSeeked = await self.player.seek(to: time, toleranceBefore: .zero, toleranceAfter: .zero)
                        
                        guard
                            isSeeked
                        else {
                            Task.detached { @MainActor [unowned self] in
                                self.seek()
                            }
                            
                            return
                        }
                        
                        await player.play()
                    }
                }
                
                seek()
            }
        } else {
            player.replaceCurrentItem(with: playerItem)
            
            player.play()
        }
        
        let progress = (self.currentPlayerContent?.progress as? NSDecimalNumber)
        
        if let playerProgressTimeObserver = playerProgressTimeObserver {
            player.removeTimeObserver(playerProgressTimeObserver)
        }
        
        if let playerSyncTimeObserver = playerSyncTimeObserver {
            player.removeTimeObserver(playerSyncTimeObserver)
        }
        
        playerProgressTimeObserver = player.addPeriodicTimeObserver(
            forInterval: CMTime(seconds: 1, preferredTimescale: CMTimeScale(44100)),
            queue: playerQueue
        ) { [weak self] (cmTime) in
            guard
                let self = self
            else {
                return
            }
            
            self.playerObserveCounter += 1
            
            let value = cmTime.seconds
            let total = (self.currentTrackFile?.duration as? NSDecimalNumber)
            
            self.player.currentItem?.preferredForwardBufferDuration = .infinity

            let buffers = self.player.currentItem?.loadedTimeRanges.map { timeRange in
                let startSeconds = timeRange.timeRangeValue.start.seconds
                let durationSeconds = timeRange.timeRangeValue.duration.seconds

                return TrackProgress.Buffer(start: startSeconds, duration: durationSeconds)
            }

            if !isTrackingProgress, !isLoadingProgress {
                Task.detached { @MainActor [weak self] in
                    guard let self else { return }
                    
                    animatingProgressSlider.toggle()

                    trackProgress = TrackProgress(
                        value: value,
                        total: total?.doubleValue,
                        buffers: buffers
                    )
                }
            }

            if (progress?.intValue ?? 0) >= (total?.intValue ?? 0) || Int(value) >= (total?.intValue ?? 0) {
                if let playerProgressTimeObserver = self.playerProgressTimeObserver {
                    self.player.removeTimeObserver(playerProgressTimeObserver)

                    self.playerProgressTimeObserver = nil
                }

                if let playerSyncTimeObserver = self.playerSyncTimeObserver {
                    self.player.removeTimeObserver(playerSyncTimeObserver)

                    self.playerSyncTimeObserver = nil
                }

                Task {
                    try await self.forward()
                }
            }
            
            if self.playerObserveCounter >= 5 {
                self.playerObserveCounter = 0
                
                guard
                    let sessionID = self.playerSession?.id
                else {
                    return
                }
                
                let value = cmTime.seconds
                
                guard
                    !self.isTrackingProgress
                else {
                    return
                }
                
                Task { [unowned self] in
                    if let playerWebSocket = self.playerWebSocket {
                        try await playerWebSocket.send(
                            PlayerMessage(
                                event: .syncTrack,
                                payload: .syncTrack(
                                    player_session_id: sessionID,
                                    progress: Int(value)
                                )
                            )
                        )
                    } else if let eventWebSocket = self.eventWebSocket {
                        try await eventWebSocket.send(
                            EventMessage(
                                event: .syncTrack,
                                payload: .syncTrack(
                                    player_session_id: sessionID,
                                    progress: Int(value)
                                )
                            )
                        )
                    }
                }
            }
        }
    }
    
    func pauseCurrentTrack() {
        Task {
            try await pauseCurrentTrack()
        }
    }
    
    func pauseCurrentTrack() async throws {
        player.pause()
        
        guard
            let sessionID = self.playerSession?.id
        else {
            return
        }
        
        let value: Int = {
            let seconds = player.currentTime().seconds
            
            guard seconds.isFinite else {
                return 0
            }
            
            return Int(seconds)
        }()
        
        if let playerWebSocket {
            try await playerWebSocket.send(
                PlayerMessage(
                    event: .syncTrack,
                    payload: .syncTrack(
                        player_session_id: sessionID,
                        progress: value
                    )
                )
            )
        } else if let eventWebSocket {
            try await eventWebSocket.send(
                EventMessage(
                    event: .syncTrack,
                    payload: .syncTrack(
                        player_session_id: sessionID,
                        progress: value
                    )
                )
            )
        }
    }
    
    func updateNowPlayingInfo() {
        var nowPlayingInfo = [String: Any]()
        
        nowPlayingInfo[MPNowPlayingInfoPropertyMediaType] = MPNowPlayingInfoMediaType.audio.rawValue
        nowPlayingInfo[MPNowPlayingInfoPropertyIsLiveStream] = false
        nowPlayingInfo[MPNowPlayingInfoPropertyPlaybackRate] = player.rate
        nowPlayingInfo[MPMediaItemPropertyTitle] = currentPlayerContent?.title ?? defaultTitle
        nowPlayingInfo[MPMediaItemPropertyArtist] = currentPlayerContent?.artist
        nowPlayingInfo[MPMediaItemPropertyPlaybackDuration] = NSDecimalNumber(decimal: currentTrackFile?.duration ?? 0)
        nowPlayingInfo[MPNowPlayingInfoPropertyElapsedPlaybackTime] = player.currentTime().seconds
        
        if let trackName = currentPlayerContent?.name {
            nowPlayingInfo[MPMediaItemPropertyArtwork] = MPMediaItemArtwork(
                boundsSize: CGSize(width: 1000, height: 1000),
                requestHandler: { boundsSize in
                    self.cachedArtworkImage(trackName) ?? UIImage()
                }
            )
        }
        
        let nowPlayingInfoCenter = MPNowPlayingInfoCenter.default()
        
        nowPlayingInfoCenter.nowPlayingInfo = nowPlayingInfo
        
        if #available(macOS 10, *) {
            nowPlayingInfoCenter.playbackState = player.rate == 0 ? .paused : .playing
        }
    }
    
    func updateNowPlayingElapsedPlaybackTime(_ time: Double?) {
        let nowPlayingInfoCenter = MPNowPlayingInfoCenter.default()
        
        let kElapsedPlaybackTime = MPNowPlayingInfoPropertyElapsedPlaybackTime
        
        nowPlayingInfoCenter.nowPlayingInfo?[kElapsedPlaybackTime] = time
    }
}
