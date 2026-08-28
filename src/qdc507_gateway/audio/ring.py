from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class PCMFrame:
    data: bytes
    sample_rate: int = 8000
    channels: int = 1
    sample_width: int = 2
    captured_at: float = field(default_factory=time.monotonic)


class RingBuffer:
    def __init__(self, capacity: int):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._items = []
        self._lock = threading.Lock()
        self.dropped = 0
        self.xruns = 0
        self.frames_in = 0
        self.frames_out = 0
        self.nonzero_samples = 0
        self.max_latency_ms = 0.0
        self._started_at = time.monotonic()
        self._first_frame_at: Optional[float] = None
        self._first_nonzero_at: Optional[float] = None
        self._sample_rates: set[int] = set()
        self._min_frame_bytes: Optional[int] = None
        self._max_frame_bytes: Optional[int] = None

    def put(self, frame: PCMFrame) -> None:
        with self._lock:
            now = time.monotonic()
            if self._first_frame_at is None:
                self._first_frame_at = now
            if len(self._items) >= self.capacity:
                self._items.pop(0)
                self.dropped += 1
            self._items.append(frame)
            self.frames_in += 1
            frame_bytes = len(frame.data)
            self._sample_rates.add(frame.sample_rate)
            self._min_frame_bytes = (
                frame_bytes
                if self._min_frame_bytes is None
                else min(self._min_frame_bytes, frame_bytes)
            )
            self._max_frame_bytes = (
                frame_bytes
                if self._max_frame_bytes is None
                else max(self._max_frame_bytes, frame_bytes)
            )
            width = max(1, frame.sample_width * frame.channels)
            nonzero = sum(
                1 for index in range(0, len(frame.data), width)
                if frame.data[index:index + width] != b"\0" * width
            )
            self.nonzero_samples += nonzero
            if nonzero and self._first_nonzero_at is None:
                self._first_nonzero_at = now

    def get(self) -> Optional[PCMFrame]:
        with self._lock:
            if not self._items:
                return None
            frame = self._items.pop(0)
            self.frames_out += 1
            self.max_latency_ms = max(self.max_latency_ms, max(0.0, time.monotonic() - frame.captured_at) * 1000)
            return frame

    def record_xrun(self) -> None:
        with self._lock:
            self.xruns += 1

    def clear(self) -> None:
        """Drop queued audio while retaining lifetime diagnostics."""
        with self._lock:
            self._items.clear()

    def reset(self) -> None:
        """Start a fresh bounded-audio accounting interval."""
        with self._lock:
            self._items.clear()
            self.dropped = 0
            self.xruns = 0
            self.frames_in = 0
            self.frames_out = 0
            self.nonzero_samples = 0
            self.max_latency_ms = 0.0
            self._started_at = time.monotonic()
            self._first_frame_at = None
            self._first_nonzero_at = None
            self._sample_rates.clear()
            self._min_frame_bytes = None
            self._max_frame_bytes = None

    def stats(self) -> dict[str, float | int | list[int] | None]:
        with self._lock:
            return {
                "dropped": self.dropped,
                "xruns": self.xruns,
                "frames_in": self.frames_in,
                "frames_out": self.frames_out,
                "nonzero_samples": self.nonzero_samples,
                "max_latency_ms": self.max_latency_ms,
                "first_frame_ms": None if self._first_frame_at is None else round(
                    max(0.0, self._first_frame_at - self._started_at) * 1000,
                    3,
                ),
                "first_nonzero_ms": None if self._first_nonzero_at is None else round(
                    max(0.0, self._first_nonzero_at - self._started_at) * 1000,
                    3,
                ),
                "sample_rates": sorted(self._sample_rates),
                "min_frame_bytes": self._min_frame_bytes,
                "max_frame_bytes": self._max_frame_bytes,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
