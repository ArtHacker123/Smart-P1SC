from __future__ import annotations

import logging
import socket
import ssl
import struct
import threading
import time
from collections import deque
from datetime import datetime
from typing import Callable, Deque, Dict, Generator, List, Optional

from .config import CameraConfig
from .models import Frame, utcnow

LOGGER = logging.getLogger(__name__)

AUTH_USERNAME = "bblp"
AUTH_PACKET_SIZE = 80
FRAME_HEADER_SIZE = 16
MAX_FRAME_BYTES = 12 * 1024 * 1024
JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


class CameraError(Exception):
    pass


class CameraConnectionError(CameraError):
    pass


class CameraAuthError(CameraError):
    pass


class CameraProtocolError(CameraError):
    pass


class CameraTimeoutError(CameraError):
    pass


def build_auth_packet(access_code: str, username: str = AUTH_USERNAME) -> bytes:
    try:
        user_bytes = username.encode("ascii")
        code_bytes = access_code.encode("ascii")
    except UnicodeEncodeError as exc:
        raise CameraAuthError("摄像头用户名或 Access Code 必须为 ASCII") from exc
    if len(user_bytes) > 32:
        raise CameraAuthError("摄像头用户名不能超过 32 字节")
    if not code_bytes:
        raise CameraAuthError("摄像头 Access Code 不能为空")
    if len(code_bytes) > 32:
        raise CameraAuthError("摄像头 Access Code 不能超过 32 字节")

    packet = struct.pack("<IIII", 0x40, 0x3000, 0, 0)
    packet += user_bytes.ljust(32, b"\x00")
    packet += code_bytes.ljust(32, b"\x00")
    if len(packet) != AUTH_PACKET_SIZE:
        raise CameraProtocolError("摄像头认证包长度错误")
    return packet


def parse_frame_header(header: bytes) -> int:
    if len(header) != FRAME_HEADER_SIZE:
        raise CameraProtocolError("摄像头帧头长度错误")
    size = struct.unpack("<I", header[0:4])[0]
    if size == 0:
        return 0
    if size < 2 or size > MAX_FRAME_BYTES:
        raise CameraProtocolError("摄像头帧长度不合理: {}".format(size))
    return size


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: List[bytes] = []
    remaining = size
    while remaining > 0:
        try:
            chunk = sock.recv(min(remaining, 65536))
        except socket.timeout as exc:
            raise CameraTimeoutError("读取摄像头数据超时") from exc
        if not chunk:
            raise CameraConnectionError("摄像头连接已关闭")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def default_tls_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class CameraClient:
    def __init__(
        self,
        host: str,
        access_code: str,
        config: CameraConfig,
        tls_context_factory: Optional[Callable[[], ssl.SSLContext]] = None,
        connector: Optional[Callable[..., socket.socket]] = None,
    ) -> None:
        self.host = host
        self.access_code = access_code
        self.config = config
        self._tls_context_factory = tls_context_factory or default_tls_context
        self._connector = connector or socket.create_connection
        self._sock: Optional[ssl.SSLSocket] = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        self.close()
        try:
            raw = self._connector(
                (self.host, self.config.port), timeout=self.config.connect_timeout_seconds
            )
        except socket.timeout as exc:
            raise CameraTimeoutError("连接摄像头超时: {}".format(self.host)) from exc
        except OSError as exc:
            raise CameraConnectionError("连接摄像头失败: {}".format(exc)) from exc

        tls: Optional[ssl.SSLSocket] = None
        try:
            context = self._tls_context_factory()
            tls = context.wrap_socket(raw, server_hostname=self.host)
            tls.settimeout(self.config.read_timeout_seconds)
            tls.sendall(build_auth_packet(self.access_code))
        except CameraError:
            if tls is not None:
                tls.close()
            else:
                raw.close()
            raise
        except ssl.SSLError as exc:
            if tls is not None:
                tls.close()
            else:
                raw.close()
            raise CameraConnectionError("摄像头 TLS 握手失败: {}".format(exc)) from exc
        except OSError as exc:
            if tls is not None:
                tls.close()
            else:
                raw.close()
            raise CameraConnectionError("摄像头认证失败: {}".format(exc)) from exc
        self._sock = tls

    def read_frame(self) -> bytes:
        if self._sock is None:
            raise CameraConnectionError("摄像头尚未连接")
        for _ in range(10):
            header = recv_exact(self._sock, FRAME_HEADER_SIZE)
            size = parse_frame_header(header)
            if size == 0:
                continue
            data = recv_exact(self._sock, size)
            if not data.startswith(JPEG_SOI) or not data.endswith(JPEG_EOI):
                raise CameraProtocolError("摄像头帧不是完整 JPEG")
            return data
        raise CameraProtocolError("摄像头连续返回空帧")

    def close(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self) -> "CameraClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class FrameRingBuffer:
    def __init__(self, maxlen: int = 120) -> None:
        if maxlen < 1:
            raise ValueError("maxlen 必须大于 0")
        self._frames: Deque[Frame] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, frame: Frame) -> None:
        with self._lock:
            self._frames.append(frame)

    def latest(self) -> Optional[Frame]:
        with self._lock:
            return self._frames[-1] if self._frames else None

    def snapshot(self, last_n: Optional[int] = None) -> List[Frame]:
        with self._lock:
            frames = list(self._frames)
        if last_n is not None:
            return frames[-last_n:] if last_n > 0 else []
        return frames

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)


class CameraStream:
    def __init__(
        self,
        host: str,
        access_code: str,
        config: CameraConfig,
        ring_buffer: Optional[FrameRingBuffer] = None,
        client_factory: Optional[Callable[[], CameraClient]] = None,
        on_frame: Optional[Callable[[Frame], None]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.host = host
        self.access_code = access_code
        self.config = config
        self.ring = (
            ring_buffer if ring_buffer is not None else FrameRingBuffer(config.ring_buffer_frames)
        )
        self._client_factory = client_factory or (
            lambda: CameraClient(host, access_code, config)
        )
        self._on_frame = on_frame
        self.logger = logger or LOGGER

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._lock = threading.Lock()
        self._client: Optional[CameraClient] = None

        self._seq = 0
        self._frames_received = 0
        self._reconnect_count = 0
        self._last_error: Optional[str] = None
        self._last_frame_at: Optional[float] = None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="camera-stream", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._connected.clear()
        with self._lock:
            client = self._client
        if client is not None:
            client.close()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        self._thread = None

    def wait_until_connected(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    def stats(self) -> Dict[str, object]:
        with self._lock:
            return {
                "connected": self._connected.is_set(),
                "frames_received": self._frames_received,
                "reconnects": self._reconnect_count,
                "last_error": self._last_error,
                "last_frame_at": self._last_frame_at,
            }

    def _run(self) -> None:
        backoff = self.config.reconnect_min_seconds
        while not self._stop.is_set():
            client: Optional[CameraClient] = None
            try:
                client = self._client_factory()
                client.connect()
                with self._lock:
                    self._client = client
                    self._last_error = None
                self._connected.set()
                self.logger.info(
                    "camera_connected", extra={"host": self.host, "port": self.config.port}
                )
                backoff = self.config.reconnect_min_seconds

                while not self._stop.is_set():
                    jpeg = client.read_frame()
                    frame = Frame(
                        seq=self._seq,
                        monotonic_ts=time.monotonic(),
                        wall_ts=utcnow(),
                        jpeg=jpeg,
                    )
                    self._seq += 1
                    with self._lock:
                        self._frames_received += 1
                        self._last_frame_at = time.time()
                    self.ring.append(frame)
                    if self._on_frame is not None:
                        try:
                            self._on_frame(frame)
                        except Exception:
                            self.logger.exception("camera_frame_callback_failed")
            except Exception as exc:
                if self._stop.is_set():
                    break
                with self._lock:
                    self._last_error = "{}: {}".format(type(exc).__name__, exc)
                    self._reconnect_count += 1
                self._connected.clear()
                self.logger.warning(
                    "camera_disconnected",
                    extra={
                        "host": self.host,
                        "error": self._last_error,
                        "retry_in_seconds": backoff,
                    },
                )
                self._stop.wait(backoff)
                backoff = min(backoff * 2, self.config.reconnect_max_seconds)
            finally:
                self._connected.clear()
                with self._lock:
                    if self._client is client:
                        self._client = None
                if client is not None:
                    client.close()

        self.logger.info("camera_stream_stopped", extra={"host": self.host})


def capture_snapshot(
    host: str,
    access_code: str,
    config: CameraConfig,
    client_factory: Optional[Callable[[], CameraClient]] = None,
) -> bytes:
    factory = client_factory or (lambda: CameraClient(host, access_code, config))
    client = factory()
    try:
        client.connect()
        return client.read_frame()
    finally:
        client.close()


def iter_frames(
    host: str,
    access_code: str,
    config: CameraConfig,
    client_factory: Optional[Callable[[], CameraClient]] = None,
) -> Generator[bytes, None, None]:
    factory = client_factory or (lambda: CameraClient(host, access_code, config))
    client = factory()
    try:
        client.connect()
        while True:
            yield client.read_frame()
    finally:
        client.close()
