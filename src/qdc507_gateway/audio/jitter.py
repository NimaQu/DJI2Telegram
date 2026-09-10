"""Bounded client playback buffering, clocked by ALSA rather than packet arrival."""

from __future__ import annotations

import threading

from .ring import PCMFrame, RingBuffer


class PlaybackJitterBuffer(RingBuffer):
    def __init__(self, capacity: int = 20, target_frames: int = 3, max_target_frames: int = 6):
        super().__init__(capacity)
        if not 1 <= target_frames <= max_target_frames <= capacity:
            raise ValueError("invalid playback buffer thresholds")
        self.initial_target = target_frames
        self.max_target = max_target_frames
        self._playout_lock = threading.RLock()
        self._reset_playout()

    def _reset_playout(self):
        self.target_frames = self.initial_target
        self.playing = False
        self.ever_played = False
        self.underruns = 0
        self.startup_silence_periods = 0
        self.rebuffer_silence_periods = 0

    def get(self) -> PCMFrame | None:
        with self._playout_lock:
            if self.playing and len(self) == 0:
                self.underruns += 1
                self.target_frames = min(self.max_target, self.target_frames + 1)
                self.playing = False
            if not self.playing:
                if len(self) < self.target_frames:
                    if self.ever_played:
                        self.rebuffer_silence_periods += 1
                    else:
                        self.startup_silence_periods += 1
                    return None
                self.playing = True
                self.ever_played = True
            return super().get()

    def reset(self):
        with self._playout_lock:
            super().reset()
            self._reset_playout()

    def stats(self):
        with self._playout_lock:
            return {
                **super().stats(),
                "queued_frames": len(self),
                "target_ms": self.target_frames * 20,
                "capacity_ms": self.capacity * 20,
                "underruns": self.underruns,
                "startup_silence_periods": self.startup_silence_periods,
                "rebuffer_silence_periods": self.rebuffer_silence_periods,
            }
