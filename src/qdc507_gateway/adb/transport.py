from __future__ import annotations

import struct
import time
from typing import Callable, Optional

from qdc507_gateway.usb.owner import DeviceOwnerLock

from .protocol import (
    ADBFrame,
    ADBProtocolError,
    CLSE,
    CNXN,
    OKAY,
    OPEN,
    SYNC_DATA,
    SYNC_DONE,
    SYNC_FAIL,
    SYNC_OKAY,
    SYNC_RECV,
    SYNC_SEND,
    WRTE,
    decode_frame,
    encode_frame,
)


class ADBTransport:
    """Protocol transport over already-opened bulk read/write callables."""

    def __init__(
        self,
        write: Callable[[bytes], None],
        read: Callable[[int, int], bytes],
        owner: Optional[DeviceOwnerLock] = None,
    ):
        self.write = write
        self.read = read
        self.owner = owner
        self._open = owner is None

    def open(self) -> None:
        if self._open:
            return
        assert self.owner is not None
        self.owner.acquire()
        self._open = True

    def close(self) -> None:
        if not self._open:
            return
        self._open = False
        if self.owner is not None:
            self.owner.release()

    def send(self, frame: ADBFrame) -> None:
        if not self._open:
            raise ADBProtocolError("ADB transport is not open")
        # The QDC507's legacy adbd expects the USB bulk transfer boundaries
        # used by the reference adb client: header first, payload second.
        # Sending one combined transfer is legal for a byte-stream transport,
        # but this firmware silently ignores the combined form.
        encoded = encode_frame(frame)
        self.write(encoded[:24])
        if frame.payload:
            self.write(encoded[24:])

    def receive(self, timeout_ms: int = 2000) -> ADBFrame:
        if not self._open:
            raise ADBProtocolError("ADB transport is not open")
        deadline = time.monotonic() + timeout_ms / 1000
        header = bytearray()
        while len(header) < 24:
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                raise TimeoutError("ADB header timeout")
            chunk = self.read(24 - len(header), max(1, remaining))
            if chunk:
                header.extend(chunk)
        length = struct.unpack_from("<I", header, 12)[0]
        if length > 1_048_576:
            raise ADBProtocolError("ADB payload exceeds safety limit")
        payload = bytearray()
        while len(payload) < length:
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                raise TimeoutError("ADB payload timeout")
            chunk = self.read(length - len(payload), max(1, remaining))
            if chunk:
                payload.extend(chunk)
        return decode_frame(bytes(header) + bytes(payload))


class ADBClient:
    """Minimal direct ADB host client over an ADBTransport.

    It implements the device protocol needed by the module runtime and keeps
    all USB ownership in the transport supplied by the caller.
    """

    def __init__(self, transport: ADBTransport, max_payload: int = 4096):
        self.transport = transport
        self.max_payload = max_payload
        self.local_id = 1
        self.remote_id: Optional[int] = None
        self.connected = False
        self._pending_frames: list[ADBFrame] = []

    def connect(
        self,
        banner: bytes = b"host::features=shell_v2,cmd,stat\0",
        timeout_ms: int = 3000,
    ) -> None:
        self.transport.open()
        self.transport.send(ADBFrame(CNXN, 0x01000001, self.max_payload, banner))
        for _ in range(16):
            frame = self.transport.receive(timeout_ms)
            if frame.command == CNXN:
                if frame.arg1 > 0:
                    self.max_payload = min(self.max_payload, frame.arg1)
                self.connected = True
                return
            # A legacy adbd can leave stream frames queued across a USB
            # handle close.  Drain and acknowledge those before accepting the
            # new connection banner.
            if frame.command == WRTE:
                self.transport.send(ADBFrame(OKAY, self.local_id, frame.arg0))
                continue
            if frame.command == CLSE:
                self.transport.send(ADBFrame(CLSE, self.local_id, frame.arg0))
                continue
        raise ADBProtocolError("ADB did not accept CNXN")

    def close(self) -> None:
        self.connected = False
        self.remote_id = None
        self._pending_frames.clear()
        self.transport.close()

    def _receive_frame(self, timeout_ms: int = 2000) -> ADBFrame:
        if self._pending_frames:
            return self._pending_frames.pop(0)
        return self.transport.receive(timeout_ms)

    def _require_connected(self) -> None:
        if not self.connected:
            raise ADBProtocolError("ADB session is not connected")

    def _open_service(self, destination: str, timeout_ms: int = 3000) -> None:
        self._require_connected()
        if not destination or "\0" in destination:
            raise ValueError("invalid ADB service")
        self.local_id += 1
        self.transport.send(ADBFrame(OPEN, self.local_id, 0, (destination + "\0").encode()))
        for _ in range(16):
            frame = self._receive_frame(timeout_ms)
            if frame.command == OKAY and frame.arg1 == self.local_id:
                self.remote_id = frame.arg0
                return
            if frame.command == WRTE and frame.arg1 == self.local_id:
                # The QDC507's legacy adbd may send the first stream data
                # frame directly after OPEN, without an intermediate OKAY.
                self.remote_id = frame.arg0
                self._pending_frames.append(frame)
                return
            if frame.command in {OKAY, WRTE, CLSE}:
                continue
            raise ADBProtocolError("ADB service OPEN was rejected")
        raise ADBProtocolError("ADB service OPEN timed out")

    def _send_stream_data(self, data: bytes, timeout_ms: int = 3000) -> None:
        if self.remote_id is None:
            raise ADBProtocolError("ADB stream is not open")
        self.transport.send(ADBFrame(WRTE, self.local_id, self.remote_id, data))
        for _ in range(16):
            frame = self._receive_frame(timeout_ms)
            if (
                frame.command == OKAY
                and frame.arg0 == self.remote_id
                and frame.arg1 == self.local_id
            ):
                return
            if frame.command in {OKAY, WRTE, CLSE}:
                continue
            raise ADBProtocolError("ADB stream write was not acknowledged")
        raise ADBProtocolError("ADB stream write acknowledgement timed out")

    def _close_stream(self, timeout_ms: int = 3000) -> None:
        if self.remote_id is None:
            return
        self.transport.send(ADBFrame(CLSE, self.local_id, self.remote_id))
        self.remote_id = None

    def _read_stream(self, timeout_ms: int = 3000) -> list[bytes]:
        if self.remote_id is None:
            raise ADBProtocolError("ADB stream is not open")
        output = []
        while True:
            frame = self._receive_frame(timeout_ms)
            if frame.command == WRTE:
                if frame.arg1 != self.local_id:
                    continue
                output.append(frame.payload)
                self.transport.send(ADBFrame(OKAY, self.local_id, frame.arg0))
                continue
            if frame.command == CLSE:
                if frame.arg1 != self.local_id:
                    continue
                self.transport.send(ADBFrame(CLSE, self.local_id, frame.arg0))
                self.remote_id = None
                return output
            if frame.command == OKAY:
                continue
            raise ADBProtocolError("unexpected ADB stream frame")

    def shell(self, command: str, timeout: float = 10.0) -> str:
        if not command or "\0" in command:
            raise ValueError("invalid ADB shell command")
        self._open_service("shell:" + command, int(timeout * 1000))
        try:
            return b"".join(self._read_stream(int(timeout * 1000))).decode("utf-8", "replace")
        finally:
            self._close_stream()

    @staticmethod
    def _sync_packet(command: int, payload: bytes = b"") -> bytes:
        return struct.pack("<II", command, len(payload)) + payload

    def push(self, data: bytes, remote_path: str, mode: int = 0o700) -> None:
        if not remote_path or "\0" in remote_path:
            raise ValueError("invalid remote path")
        # The 8-byte SYNC DATA header is part of the ADB WRTE payload. Using
        # max_payload bytes for the file body produced max_payload + 8 byte
        # WRTE frames; this QDC507 adbd accepts some of them and then stops
        # acknowledging a larger push. Keep every WRTE within the negotiated
        # CNXN limit.
        chunk_size = min(64 * 1024, self.max_payload - 8)
        if chunk_size <= 0:
            raise ADBProtocolError("ADB max payload is too small for sync")
        self._open_service("sync:")
        try:
            send = f"{remote_path},{mode:o}".encode()
            send_packet = struct.pack("<II", SYNC_SEND, len(send)) + send
            if len(send_packet) > self.max_payload:
                raise ValueError("remote path is too long for the negotiated ADB payload")
            self._send_stream_data(send_packet)
            for index in range(0, len(data), chunk_size):
                chunk = data[index:index + chunk_size]
                self._send_stream_data(self._sync_packet(SYNC_DATA, chunk))
            self._send_stream_data(struct.pack("<II", SYNC_DONE, 0))
            self._read_sync_result()
        finally:
            self._close_stream()

    def pull(self, remote_path: str) -> bytes:
        if not remote_path or "\0" in remote_path:
            raise ValueError("invalid remote path")
        self._open_service("sync:")
        try:
            self._send_stream_data(struct.pack("<II", SYNC_RECV, len(remote_path)) + remote_path.encode())
            return self._read_sync_data()
        finally:
            self._close_stream()

    def _read_sync_result(self) -> None:
        packet_buffer = bytearray()
        if self.remote_id is None:
            raise ADBProtocolError("ADB sync stream is not open")
        while True:
            frame = self._receive_frame(10_000)
            if frame.command == WRTE:
                if frame.arg1 != self.local_id:
                    continue
                packet_buffer.extend(frame.payload)
                self.transport.send(ADBFrame(OKAY, self.local_id, frame.arg0))
                while len(packet_buffer) >= 8:
                    command, length = struct.unpack_from("<II", packet_buffer, 0)
                    if length > 1_048_576:
                        raise ADBProtocolError("ADB sync result exceeds safety limit")
                    if len(packet_buffer) < 8 + length:
                        break
                    body = bytes(packet_buffer[8:8 + length])
                    del packet_buffer[:8 + length]
                    if command == SYNC_OKAY:
                        # This legacy adbd waits for the host CLSE rather than
                        # sending one of its own after the result packet.
                        return
                    if command == SYNC_FAIL:
                        reason = body.decode("utf-8", "replace")
                        raise ADBProtocolError("ADB sync push failed: " + reason)
                    raise ADBProtocolError("ADB sync push returned an unexpected result")
                continue
            if frame.command == CLSE:
                if frame.arg1 != self.local_id:
                    continue
                self.remote_id = None
                raise ADBProtocolError("ADB sync push closed before result")
            raise ADBProtocolError("unexpected ADB sync push frame")

    def _read_sync_data(self) -> bytes:
        output = bytearray()
        packet_buffer = bytearray()
        if self.remote_id is None:
            raise ADBProtocolError("ADB sync stream is not open")
        while True:
            frame = self._receive_frame(10_000)
            if frame.command == WRTE:
                if frame.arg1 != self.local_id:
                    continue
                packet_buffer.extend(frame.payload)
                self.transport.send(ADBFrame(OKAY, self.local_id, frame.arg0))
                while len(packet_buffer) >= 8:
                    command, length = struct.unpack_from("<II", packet_buffer, 0)
                    if length > 1_048_576:
                        raise ADBProtocolError("ADB sync payload exceeds safety limit")
                    if len(packet_buffer) < 8 + length:
                        break
                    body = bytes(packet_buffer[8:8 + length])
                    del packet_buffer[:8 + length]
                    if command == SYNC_DATA:
                        output.extend(body)
                    elif command == SYNC_DONE:
                        return bytes(output)
                    elif command == SYNC_FAIL:
                        raise ADBProtocolError("ADB sync pull failed")
                    else:
                        raise ADBProtocolError("unexpected ADB sync packet")
                continue
            if frame.command == CLSE:
                if frame.arg1 != self.local_id:
                    continue
                self.remote_id = None
                raise ADBProtocolError("ADB sync stream closed before DONE")
            raise ADBProtocolError("unexpected ADB sync frame")


class LibUSBADBClient(ADBClient):
    """ADBClient bound to a descriptor-selected libusb bulk interface."""

    def __init__(
        self,
        handle,
        interface,
        owner: Optional[DeviceOwnerLock] = None,
        close_handle: bool = True,
    ):
        from qdc507_gateway.modem.transport import LibUSBBulkTransport

        bulk = LibUSBBulkTransport(handle, interface, owner=owner, close_handle=close_handle)
        # LibUSBBulkTransport is the owner of the interface lease. Passing the
        # same lock to ADBTransport would acquire it a second time in connect().
        super().__init__(ADBTransport(bulk.write, bulk.read, owner=None))
        self.bulk = bulk

    def connect(self, banner: bytes = b"host::qdc507-gateway\0", timeout_ms: int = 3000) -> None:
        self.bulk.open()
        try:
            super().connect(banner, timeout_ms)
        except Exception:
            self.bulk.close()
            raise

    def close(self) -> None:
        self.connected = False
        self.remote_id = None
        self.bulk.close()


def shell_service(command: str) -> bytes:
    if not command or "\0" in command:
        raise ValueError("invalid ADB shell command")
    return ("shell:" + command).encode("utf-8")
