from __future__ import annotations

import re
import struct
import time
from dataclasses import dataclass
from pathlib import Path

from .ring import PCMFrame


class ALSAUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class AlsaPCMEndpoint:
    card: int
    device: int
    direction: str

    @property
    def name(self) -> str:
        return f"hw:{self.card},{self.device}"


def _is_qdc507_ancestry(path: Path) -> bool:
    for ancestor in (path, *path.parents):
        try:
            vendor = (ancestor / "idVendor").read_text(encoding="ascii").strip().lower()
            product = (ancestor / "idProduct").read_text(encoding="ascii").strip().lower()
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            continue
        if vendor == "2c7c" and product == "0125":
            return True
    return False


def find_qdc507_pcm_devices(sysfs_root: str | Path = "/sys") -> tuple[AlsaPCMEndpoint, ...]:
    """Find PCM endpoints whose sound card descends from QDC507 USB sysfs."""
    sound_root = Path(sysfs_root) / "class" / "sound"
    result = []
    for card in sorted(sound_root.glob("card[0-9]*")):
        resolved = card.resolve()
        if not _is_qdc507_ancestry(resolved):
            continue
        for pcm in sorted(card.glob("pcmC*D*[cp]")):
            match = re.fullmatch(r"pcmC(\d+)D(\d+)([cp])", pcm.name)
            if match is None:
                continue
            result.append(AlsaPCMEndpoint(
                int(match.group(1)), int(match.group(2)),
                "capture" if match.group(3) == "c" else "playback",
            ))
    return tuple(result)


def resample_pcm16_mono(data: bytes, source_rate: int, target_rate: int) -> bytes:
    """Linear PCM16 mono conversion for the NTgCalls/ALSA boundary."""
    if source_rate <= 0 or target_rate <= 0 or len(data) % 2:
        raise ValueError("invalid PCM16 input or sample rate")
    if source_rate == target_rate:
        return data
    sample_count = len(data) // 2
    if sample_count == 0:
        return b""
    source = [struct.unpack_from("<h", data, index * 2)[0] for index in range(sample_count)]
    output_count = max(1, round(sample_count * target_rate / source_rate))
    output = bytearray()
    for index in range(output_count):
        position = index * source_rate / target_rate
        left = min(sample_count - 1, int(position))
        right = min(sample_count - 1, left + 1)
        fraction = position - left
        value = round(source[left] + (source[right] - source[left]) * fraction)
        output.extend(struct.pack("<h", max(-32768, min(32767, value))))
    return bytes(output)


class AlsaPCMDevice:
    """Runtime ALSA adapter; importing pyalsaaudio is deferred until live use."""

    def __init__(self, device: str, playback_device: str | None = None, period_frames: int = 160):
        try:
            import alsaaudio  # type: ignore
        except ImportError as exc:
            raise ALSAUnavailable("pyalsaaudio is required for live UAC audio") from exc
        self._alsa = alsaaudio
        self.capture_device = device
        self.playback_device = playback_device or device
        self.period_frames = period_frames
        self.capture = None
        self.playback = None
        self.xruns = 0
        self.frames_read = 0
        self.frames_written = 0
        self.nonzero_samples = 0
        self.silence_periods = 0
        self._opened_at: float | None = None
        self._first_capture_at: float | None = None
        self._first_nonzero_at: float | None = None

    def open(self) -> None:
        try:
            self.capture = self._alsa.PCM(
                self._alsa.PCM_CAPTURE, self._alsa.PCM_NORMAL, self.capture_device,
                channels=1, rate=8000, format=self._alsa.PCM_FORMAT_S16_LE,
                periodsize=self.period_frames,
            )
            self.playback = self._alsa.PCM(
                self._alsa.PCM_PLAYBACK, self._alsa.PCM_NORMAL, self.playback_device,
                channels=1, rate=8000, format=self._alsa.PCM_FORMAT_S16_LE,
                periodsize=self.period_frames,
            )
            self._opened_at = time.monotonic()
        except Exception:
            self.close()
            raise

    def read(self) -> PCMFrame:
        if self.capture is None:
            raise ALSAUnavailable("ALSA device is not open")
        length, data = self.capture.read()
        if length < 0:
            self.xruns += 1
            raise ALSAUnavailable("ALSA capture xrun")
        raw = bytes(data)
        now = time.monotonic()
        if self._first_capture_at is None:
            self._first_capture_at = now
        self.frames_read += length
        nonzero = sum(1 for index in range(0, len(raw), 2) if raw[index:index + 2] != b"\0\0")
        self.nonzero_samples += nonzero
        if nonzero and self._first_nonzero_at is None:
            self._first_nonzero_at = now
        return PCMFrame(raw, 8000, 1, 2)

    def write(self, frame: PCMFrame) -> None:
        if self.playback is None:
            raise ALSAUnavailable("ALSA device is not open")
        if frame.channels != 1 or frame.sample_width != 2:
            raise ALSAUnavailable("only PCM16 mono audio is supported")
        data = resample_pcm16_mono(frame.data, frame.sample_rate, 8000)
        written = self.playback.write(data)
        if isinstance(written, int) and written < 0:
            self.xruns += 1
            raise ALSAUnavailable("ALSA playback xrun")
        self.frames_written += len(data) // 2

    def write_silence(self) -> None:
        """Keep the UAC playback clock running while the remote leg is idle."""
        self.write(PCMFrame(b"\0" * (self.period_frames * 2), 8000, 1, 2))
        self.silence_periods += 1

    def stats(self) -> dict[str, int | float | None]:
        opened_at = self._opened_at
        return {
            "xruns": self.xruns,
            "frames_read": self.frames_read,
            "frames_written": self.frames_written,
            "nonzero_samples": self.nonzero_samples,
            "silence_periods": self.silence_periods,
            "first_capture_ms": None if opened_at is None or self._first_capture_at is None else round(
                max(0.0, self._first_capture_at - opened_at) * 1000,
                3,
            ),
            "first_nonzero_ms": None if opened_at is None or self._first_nonzero_at is None else round(
                max(0.0, self._first_nonzero_at - opened_at) * 1000,
                3,
            ),
        }

    def close(self) -> None:
        first_error = None
        for stream in (self.capture, self.playback):
            if stream is not None:
                try:
                    stream.close()
                except Exception as exc:
                    first_error = first_error or exc
        self.capture = self.playback = None
        if first_error is not None:
            raise first_error
