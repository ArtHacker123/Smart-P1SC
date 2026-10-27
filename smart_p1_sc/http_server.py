from __future__ import annotations

import base64
import hmac
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Protocol, Tuple
from urllib.parse import parse_qs, urlparse

from .config import HttpConfig
from .models import DecisionResult, Frame

LOGGER = logging.getLogger(__name__)

MAX_REQUEST_BODY = 64 * 1024


class ApiRuntime(Protocol):
    def status(self) -> Dict[str, Any]:
        ...

    def list_incidents(self, open_only: bool, limit: int) -> List[Dict[str, Any]]:
        ...

    def get_incident_detail(self, reference: str) -> Optional[Dict[str, Any]]:
        ...

    def decide(
        self,
        reference: str,
        action: str,
        actor: str,
        note: Optional[str],
        short_code: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> DecisionResult:
        ...

    def label(
        self, reference: str, label: str, actor: str, note: Optional[str]
    ) -> Tuple[bool, str]:
        ...

    def latest_frame(self) -> Optional[Frame]:
        ...

    def read_incident_frame(self, reference: str, index: int) -> Optional[bytes]:
        ...


class RateLimiter:
    def __init__(
        self,
        max_events: int = 30,
        window_seconds: float = 60.0,
        clock: Optional[Any] = None,
    ) -> None:
        self.max_events = max(1, int(max_events))
        self.window_seconds = float(window_seconds)
        self._clock = clock or time.monotonic
        self._events: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._clock()
        cutoff = now - self.window_seconds
        with self._lock:
            events = [timestamp for timestamp in self._events.get(key, []) if timestamp > cutoff]
            if len(events) >= self.max_events:
                self._events[key] = events
                return False
            events.append(now)
            self._events[key] = events
            return True


STATUS_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Smart P1SC</title>
<style>
body { font-family: system-ui, sans-serif; margin: 0; padding: 20px; background: #111; color: #eee; }
h1 { font-size: 20px; }
h2 { font-size: 16px; margin-top: 24px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
.card { background: #1c1c1c; border: 1px solid #333; border-radius: 8px; padding: 12px; }
.label { color: #999; font-size: 12px; }
.value { font-size: 18px; margin-top: 4px; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 8px; border-bottom: 1px solid #333; font-size: 13px; }
button { margin-right: 6px; padding: 5px 10px; border-radius: 5px; border: 1px solid #555; background: #2a2a2a; color: #eee; cursor: pointer; }
button:hover { background: #3a3a3a; }
img { max-width: 100%; border-radius: 8px; border: 1px solid #333; }
td img { width: 80px; border: 0; border-radius: 4px; }
.muted { color: #888; }
</style>
</head>
<body>
<h1>Smart P1SC</h1>
<div class="grid">
  <div class="card"><div class="label">打印机</div><div class="value" id="printer">-</div></div>
  <div class="card"><div class="label">摄像头</div><div class="value" id="camera">-</div></div>
  <div class="card"><div class="label">开放事件</div><div class="value" id="open-count">-</div></div>
  <div class="card"><div class="label">检测器</div><div class="value" id="detector">-</div></div>
  <div class="card"><div class="label">磁盘剩余</div><div class="value" id="storage">-</div></div>
  <div class="card"><div class="label">待发通知</div><div class="value" id="notifications">-</div></div>
</div>
<h2>活动事件</h2>
<table>
<thead><tr><th>短码</th><th>类型</th><th>严重度</th><th>状态</th><th>置信度</th><th>剩余保持</th><th>胶片</th><th>动作</th></tr></thead>
<tbody id="incidents"><tr><td colspan="8" class="muted">加载中...</td></tr></tbody>
</table>
<h2>实时画面</h2>
<img src="/stream.mjpg" alt="实时画面">
<script>
function text(id, value) { document.getElementById(id).textContent = value; }
function minutesLeft(deadline) {
  if (!deadline) return '-';
  const diff = new Date(deadline).getTime() - Date.now();
  if (diff <= 0) return '已到期';
  return Math.ceil(diff / 60000) + ' 分钟';
}
async function load() {
  try {
    const status = await (await fetch('/status')).json();
    const printer = status.printer || {};
    text('printer', printer.gcode_state || 'UNKNOWN');
    const camera = status.camera || {};
    text('camera', (camera.health || 'UNKNOWN') + (camera.frame_age_seconds == null ? '' : ' / ' + camera.frame_age_seconds + 's'));
    text('open-count', String(status.open_incidents || 0));
    const detection = status.detection || {};
    text('detector', detection.enabled ? (detection.model_version || 'enabled') : 'disabled');
    const storage = status.storage || {};
    text('storage', storage.free_mb == null ? '未知' : storage.free_mb + ' MB' + (storage.low ? ' / 低' : ''));
    const notifications = status.notifications || {};
    text('notifications', String(notifications.unsent == null ? 0 : notifications.unsent));
    const data = await (await fetch('/incidents?open=1&limit=20')).json();
    const rows = data.incidents || [];
    const tbody = document.getElementById('incidents');
    tbody.replaceChildren();
    if (!rows.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 8; td.className = 'muted'; td.textContent = '没有开放事件';
      tr.appendChild(td); tbody.appendChild(tr); return;
    }
    for (const item of rows) {
      const tr = document.createElement('tr');
      for (const value of [item.short_code, item.type, item.severity, item.state,
                           Math.round((item.confidence || 0) * 100) + '%',
                           minutesLeft(item.hold_deadline)]) {
        const td = document.createElement('td'); td.textContent = String(value); tr.appendChild(td);
      }
      const frameTd = document.createElement('td');
      const preview = document.createElement('img');
      preview.src = '/incidents/' + encodeURIComponent(item.short_code) + '/frames/0';
      preview.alt = '胶片';
      preview.onerror = () => { preview.style.display = 'none'; };
      frameTd.appendChild(preview);
      tr.appendChild(frameTd);
      const actionTd = document.createElement('td');
      if (item.state === 'HOLDING') {
        for (const action of ['resume', 'stop', 'false_positive']) {
          const button = document.createElement('button');
          button.textContent = action === 'resume' ? '继续' : action === 'stop' ? '停止' : '误报';
          button.onclick = () => decide(item.short_code, action);
          actionTd.appendChild(button);
        }
      }
      tr.appendChild(actionTd); tbody.appendChild(tr);
    }
  } catch (error) {
    text('printer', '连接失败');
  }
}
async function decide(reference, action) {
  if (!confirm('确认对 ' + reference + ' 执行 ' + action + '？')) return;
  const response = await fetch('/incidents/' + encodeURIComponent(reference) + '/decide', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: action, actor: 'web'})
  });
  const result = await response.json();
  if (!response.ok) alert(result.message || result.code || '操作失败');
  load();
}
load();
setInterval(load, 2000);
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    runtime: ApiRuntime
    token: str
    logger: logging.Logger
    limiter: RateLimiter

    server_version = "smart-p1sc/0.1"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        self.logger.debug(
            "http_request",
            extra={
                "client": self.client_address[0],
                "method": getattr(self, "command", ""),
                "path": getattr(self, "path", ""),
                "message": format % args,
            },
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if parts == ["health"]:
            self._send_json(200, {"ok": True})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"}, www_authenticate=True)
            return
        if parts in ([], ["index.html"]):
            self._send_bytes(
                200,
                STATUS_PAGE.encode("utf-8"),
                "text/html; charset=utf-8",
                extra_headers={
                    "Content-Security-Policy": (
                        "default-src 'self'; img-src 'self' data:; "
                        "style-src 'unsafe-inline'; script-src 'unsafe-inline'"
                    )
                },
            )
            return
        if parts == ["status"]:
            self._send_json(200, self.runtime.status())
            return
        if parts == ["incidents"]:
            query = parse_qs(parsed.query)
            open_only = str(query.get("open", ["0"])[0]).lower() in ("1", "true", "yes")
            try:
                limit = int(query.get("limit", ["50"])[0])
            except ValueError:
                self._send_json(400, {"error": "invalid limit"})
                return
            limit = max(1, min(limit, 200))
            incidents = self.runtime.list_incidents(open_only=open_only, limit=limit)
            self._send_json(200, {"incidents": incidents})
            return
        if len(parts) == 2 and parts[0] == "incidents":
            detail = self.runtime.get_incident_detail(parts[1])
            if detail is None:
                self._send_json(404, {"error": "incident_not_found"})
            else:
                self._send_json(200, detail)
            return
        if (
            len(parts) == 4
            and parts[0] == "incidents"
            and parts[2] == "frames"
        ):
            try:
                index = int(parts[3])
            except ValueError:
                self._send_json(400, {"error": "invalid_frame_index"})
                return
            body = self.runtime.read_incident_frame(parts[1], index)
            if body is None:
                self._send_json(404, {"error": "frame_not_found"})
            else:
                self._send_bytes(200, body, "image/jpeg")
            return
        if parts == ["snapshot"]:
            frame = self.runtime.latest_frame()
            if frame is None:
                self._send_json(404, {"error": "no_frame"})
            else:
                self._send_bytes(200, frame.jpeg, "image/jpeg")
            return
        if parts in (["stream"], ["stream.mjpg"]):
            self._stream_mjpeg()
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) == 3 and parts[0] == "incidents" and parts[2] == "decide":
            self._handle_decide(parts[1])
            return
        if len(parts) == 3 and parts[0] == "incidents" and parts[2] == "label":
            self._handle_label(parts[1])
            return
        self._send_json(404, {"error": "not_found"})

    def _stream_mjpeg(self) -> None:
        frame = self.runtime.latest_frame()
        if frame is None:
            self._send_json(404, {"error": "no_frame"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        last_seq = -1
        try:
            while True:
                current = self.runtime.latest_frame()
                if current is not None and current.seq != last_seq:
                    header = (
                        "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {}\r\n\r\n".format(
                            len(current.jpeg)
                        )
                    ).encode("ascii")
                    self.wfile.write(header)
                    self.wfile.write(current.jpeg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                    last_seq = current.seq
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _handle_decide(self, reference: str) -> None:
        if not self.limiter.allow(self.client_address[0]):
            self._send_json(429, {"error": "rate_limited"})
            return
        payload = self._read_json()
        if payload is None:
            return
        action = str(payload.get("action", "")).strip().lower()
        if action not in ("resume", "stop", "false_positive"):
            self._send_json(400, {"error": "invalid_action"})
            return
        actor = str(payload.get("actor", "http"))[:64]
        note_raw = payload.get("note")
        note = str(note_raw)[:500] if note_raw is not None else None
        short_code_raw = payload.get("short_code")
        short_code = str(short_code_raw)[:16] if short_code_raw else None
        key_raw = payload.get("idempotency_key")
        idempotency_key = str(key_raw)[:128] if key_raw else None
        result = self.runtime.decide(
            reference,
            action,
            actor,
            note,
            short_code=short_code,
            idempotency_key=idempotency_key,
        )
        status = _status_for_decision(result)
        self._send_json(status, result.to_dict())

    def _handle_label(self, reference: str) -> None:
        payload = self._read_json()
        if payload is None:
            return
        label = str(payload.get("label", "")).strip()
        actor = str(payload.get("actor", "http"))[:64]
        note_raw = payload.get("note")
        note = str(note_raw)[:500] if note_raw is not None else None
        success, code = self.runtime.label(reference, label, actor, note)
        if success:
            self._send_json(200, {"ok": True, "code": code})
            return
        status = 404 if code == "incident_not_found" else 400
        self._send_json(status, {"ok": False, "code": code})

    def _read_json(self) -> Optional[Dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "invalid_content_length"})
            return None
        if length <= 0 or length > MAX_REQUEST_BODY:
            self._send_json(400, {"error": "invalid_body"})
            return None
        body = self.rfile.read(length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid_json"})
            return None
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "invalid_json"})
            return None
        return payload

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        token = ""
        lowered = header.lower()
        if lowered.startswith("bearer "):
            token = header[7:].strip()
        elif lowered.startswith("basic "):
            try:
                decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
                _, _, password = decoded.partition(":")
                token = password
            except (ValueError, UnicodeDecodeError):
                token = ""
        if not token:
            token = self.headers.get("X-API-Token", "").strip()
        return bool(token) and hmac.compare_digest(token, self.token)

    def _send_json(
        self,
        status: int,
        payload: Dict[str, Any],
        www_authenticate: bool = False,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        extra = None
        if www_authenticate:
            extra = {"WWW-Authenticate": 'Basic realm="smart-p1sc"'}
        self._send_bytes(
            status,
            body,
            "application/json; charset=utf-8",
            extra_headers=extra,
        )

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _status_for_decision(result: DecisionResult) -> int:
    if result.success:
        return 200
    if result.code == "incident_not_found":
        return 404
    if result.code in ("invalid_state", "already_resolved", "idempotency_conflict"):
        return 409
    if result.code == "short_code_mismatch":
        return 400
    if result.code == "command_failed":
        return 502
    return 400


class LocalApiServer:
    def __init__(
        self,
        runtime: ApiRuntime,
        config: HttpConfig,
        token: str,
        logger: Optional[logging.Logger] = None,
        rate_limit_per_minute: int = 30,
    ) -> None:
        if not token:
            raise ValueError("本地 HTTP 接口必须配置 token")
        self.runtime = runtime
        self.config = config
        self.token = token
        self.logger = logger or LOGGER
        self.limiter = RateLimiter(max_events=rate_limit_per_minute, window_seconds=60.0)
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> Optional[int]:
        if self._server is None:
            return None
        return int(self._server.server_address[1])

    def start(self) -> None:
        if self._server is not None:
            return
        handler = type(
            "SmartP1ScHandler",
            (_Handler,),
            {
                "runtime": self.runtime,
                "token": self.token,
                "logger": self.logger,
                "limiter": self.limiter,
            },
        )
        server = ThreadingHTTPServer((self.config.host, self.config.port), handler)
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever, name="local-api", daemon=True
        )
        self._thread.start()
        self.logger.info(
            "http_server_started",
            extra={"host": self.config.host, "port": self.port},
        )

    def stop(self, timeout: float = 5.0) -> None:
        server = self._server
        self._server = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self.logger.info("http_server_stopped")

    def __enter__(self) -> "LocalApiServer":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()
