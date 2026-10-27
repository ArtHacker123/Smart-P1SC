import socket
import struct
import threading
import time

import pytest

from smart_p1_sc.camera import (
    AUTH_PACKET_SIZE,
    CameraAuthError,
    CameraClient,
    CameraConnectionError,
    CameraProtocolError,
    CameraStream,
    FrameRingBuffer,
    build_auth_packet,
    parse_frame_header,
    recv_exact,
)
from smart_p1_sc.config import CameraConfig
from smart_p1_sc.models import Frame, utcnow


def jpeg_payload(marker=b"x"):
    return b"\xff\xd8" + marker * 16 + b"\xff\xd9"


class FakeSocket:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def recv(self, size):
        if not self.chunks:
            return b""
        return self.chunks.pop(0)


def test_build_auth_packet_layout():
    packet = build_auth_packet("12345678")
    assert len(packet) == AUTH_PACKET_SIZE
    assert struct.unpack("<IIII", packet[:16]) == (0x40, 0x3000, 0, 0)
    assert packet[16:48].rstrip(b"\x00") == b"bblp"
    assert packet[48:80].rstrip(b"\x00") == b"12345678"


def test_build_auth_packet_rejects_long_or_empty_code():
    with pytest.raises(CameraAuthError):
        build_auth_packet("x" * 33)
    with pytest.raises(CameraAuthError):
        build_auth_packet("")


def test_parse_frame_header():
    header = struct.pack("<I", 1234) + b"\x00" * 12
    assert parse_frame_header(header) == 1234
    assert parse_frame_header(b"\x00" * 16) == 0
    with pytest.raises(CameraProtocolError):
        parse_frame_header(b"\x00" * 15)
    with pytest.raises(CameraProtocolError):
        parse_frame_header(struct.pack("<I", 999999999) + b"\x00" * 12)


def test_recv_exact_handles_chunks():
    assert recv_exact(FakeSocket([b"ab", b"cd", b"ef"]), 6) == b"abcdef"


def test_recv_exact_eof_raises():
    with pytest.raises(CameraConnectionError):
        recv_exact(FakeSocket([]), 1)


def test_read_frame_skips_empty_then_returns_jpeg():
    payload = jpeg_payload()
    zero_header = b"\x00" * 16
    header = struct.pack("<I", len(payload)) + b"\x00" * 12
    client = CameraClient("127.0.0.1", "12345678", CameraConfig())
    client._sock = FakeSocket([zero_header, header[:5], header[5:], payload[:10], payload[10:]])
    assert client.read_frame() == payload


def test_read_frame_rejects_incomplete_jpeg():
    payload = b"\xff\xd8" + b"x" * 8
    header = struct.pack("<I", len(payload)) + b"\x00" * 12
    client = CameraClient("127.0.0.1", "12345678", CameraConfig())
    client._sock = FakeSocket([header, payload])
    with pytest.raises(CameraProtocolError):
        client.read_frame()


def test_ring_buffer_keeps_latest():
    ring = FrameRingBuffer(maxlen=3)
    for seq in range(5):
        ring.append(Frame(seq=seq, monotonic_ts=0.0, wall_ts=utcnow(), jpeg=jpeg_payload()))
    assert len(ring) == 3
    assert ring.latest().seq == 4
    assert [frame.seq for frame in ring.snapshot(2)] == [3, 4]


class FakeClient:
    def __init__(self):
        self.frames = [jpeg_payload()]
        self.connected = False
        self.closed = threading.Event()

    def connect(self):
        self.connected = True

    def read_frame(self):
        if self.frames:
            return self.frames.pop(0)
        self.closed.wait(2.0)
        raise CameraConnectionError("closed")

    def close(self):
        self.closed.set()


def test_camera_stream_delivers_frames_and_stops():
    fake = FakeClient()
    config = CameraConfig(reconnect_min_seconds=0.01, reconnect_max_seconds=0.05)
    ring = FrameRingBuffer(maxlen=10)
    stream = CameraStream("127.0.0.1", "12345678", config, ring_buffer=ring, client_factory=lambda: fake)
    stream.start()
    deadline = time.time() + 2.0
    while time.time() < deadline and len(ring) == 0:
        time.sleep(0.01)
    assert len(ring) == 1
    assert stream.connected is True
    stream.stop()
    assert stream.connected is False
    assert stream.stats()["frames_received"] >= 1
