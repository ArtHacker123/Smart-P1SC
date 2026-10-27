import base64
import json
import socket
import time
import urllib.error
import urllib.request

import pytest

from smart_p1_sc.config import HttpConfig
from smart_p1_sc.http_server import LocalApiServer
from smart_p1_sc.models import DecisionResult, Frame, utcnow


class FakeRuntime:
    def __init__(self):
        self.last_list = None
        self.decision_result = DecisionResult(
            success=True, code="ok", message="ok", incident_id="inc-1"
        )
        self.label_result = (True, "ok")
        self.frame = Frame(seq=1, monotonic_ts=0.0, wall_ts=utcnow(), jpeg=b"\xff\xd8test\xff\xd9")
        self.detail = {"incident": {"id": "inc-1", "short_code": "7F3A"}, "events": [], "labels": []}

    def status(self):
        return {"daemon": "running", "printer": {"gcode_state": "RUNNING"}}

    def list_incidents(self, open_only, limit):
        self.last_list = (open_only, limit)
        return [{"id": "inc-1", "short_code": "7F3A"}]

    def get_incident_detail(self, reference):
        if reference == "MISSING":
            return None
        return self.detail

    def decide(self, reference, action, actor, note, short_code=None, idempotency_key=None):
        return self.decision_result

    def label(self, reference, label, actor, note):
        return self.label_result

    def latest_frame(self):
        return self.frame

    def read_incident_frame(self, reference, index):
        if reference == "MISSING":
            return None
        return "frame-{}".format(index).encode("ascii")


def request(method, url, token=None, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.read(), response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get_content_type()


@pytest.fixture()
def api():
    runtime = FakeRuntime()
    server = LocalApiServer(
        runtime,
        HttpConfig(host="127.0.0.1", port=0),
        token="secret",
        rate_limit_per_minute=100,
    )
    server.start()
    base = "http://127.0.0.1:{}".format(server.port)
    yield runtime, base
    server.stop()


def test_health_does_not_require_token(api):
    _, base = api
    status, body, _ = request("GET", base + "/health")
    assert status == 200
    assert json.loads(body) == {"ok": True}


def test_status_page_and_basic_auth(api):
    _, base = api
    req = urllib.request.Request(base + "/")
    try:
        urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
        assert "Basic" in exc.headers.get("WWW-Authenticate", "")
    else:
        raise AssertionError("expected 401")

    credentials = base64.b64encode(b"operator:secret").decode("ascii")
    req = urllib.request.Request(
        base + "/", headers={"Authorization": "Basic " + credentials}
    )
    with urllib.request.urlopen(req, timeout=5) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "text/html"
        body = response.read().decode("utf-8")
    assert "Smart P1SC" in body
    assert "/stream.mjpg" in body


def test_status_requires_token(api):
    _, base = api
    status, _, _ = request("GET", base + "/status")
    assert status == 401
    status, body, content_type = request("GET", base + "/status", token="secret")
    assert status == 200
    assert content_type == "application/json"
    assert json.loads(body)["printer"]["gcode_state"] == "RUNNING"


def test_incidents_query_and_detail(api):
    runtime, base = api
    status, body, _ = request("GET", base + "/incidents?open=1&limit=5", token="secret")
    assert status == 200
    assert runtime.last_list == (True, 5)
    assert json.loads(body)["incidents"][0]["short_code"] == "7F3A"

    status, body, _ = request("GET", base + "/incidents/7F3A", token="secret")
    assert status == 200
    assert json.loads(body)["incident"]["short_code"] == "7F3A"

    status, _, _ = request("GET", base + "/incidents/MISSING", token="secret")
    assert status == 404


def test_incident_frame(api):
    _, base = api
    status, body, content_type = request(
        "GET", base + "/incidents/7F3A/frames/2", token="secret"
    )
    assert status == 200
    assert content_type == "image/jpeg"
    assert body == b"frame-2"
    assert request("GET", base + "/incidents/7F3A/frames/x", token="secret")[0] == 400
    assert request("GET", base + "/incidents/MISSING/frames/0", token="secret")[0] == 404


def test_snapshot_returns_jpeg(api):
    _, base = api
    status, body, content_type = request("GET", base + "/snapshot", token="secret")
    assert status == 200
    assert content_type == "image/jpeg"
    assert body == b"\xff\xd8test\xff\xd9"


def test_mjpeg_stream_replays_latest_frame(api):
    _, base = api
    host, port = base.replace("http://", "").split(":")
    with socket.create_connection((host, int(port)), timeout=2) as sock:
        sock.sendall(
            (
                "GET /stream.mjpg HTTP/1.1\r\n"
                "Host: {}\r\n"
                "Authorization: Bearer secret\r\n"
                "\r\n"
            ).format(host).encode("ascii")
        )
        data = b""
        deadline = 2.0
        started = time.monotonic()
        while b"--frame" not in data and time.monotonic() - started < deadline:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    assert b"multipart/x-mixed-replace" in data
    assert b"--frame" in data
    assert b"Content-Type: image/jpeg" in data


def test_decide_success_and_validation(api):
    runtime, base = api
    status, body, _ = request(
        "POST",
        base + "/incidents/7F3A/decide",
        token="secret",
        payload={"action": "resume", "actor": "test"},
    )
    assert status == 200
    assert json.loads(body)["code"] == "ok"

    status, _, _ = request(
        "POST", base + "/incidents/7F3A/decide", token="secret", payload={"action": "explode"}
    )
    assert status == 400


def test_decide_status_mapping(api):
    runtime, base = api
    runtime.decision_result = DecisionResult(
        success=False, code="already_resolved", message="done", incident_id="inc-1"
    )
    status, _, _ = request(
        "POST", base + "/incidents/7F3A/decide", token="secret", payload={"action": "resume"}
    )
    assert status == 409

    runtime.decision_result = DecisionResult(
        success=False, code="command_failed", message="boom", incident_id="inc-1"
    )
    status, _, _ = request(
        "POST", base + "/incidents/7F3A/decide", token="secret", payload={"action": "stop"}
    )
    assert status == 502

    runtime.decision_result = DecisionResult(
        success=False, code="incident_not_found", message="missing", incident_id="nope"
    )
    status, _, _ = request(
        "POST", base + "/incidents/nope/decide", token="secret", payload={"action": "stop"}
    )
    assert status == 404


def test_decision_rate_limit():
    server = LocalApiServer(
        FakeRuntime(),
        HttpConfig(host="127.0.0.1", port=0),
        token="secret",
        rate_limit_per_minute=2,
    )
    server.start()
    base = "http://127.0.0.1:{}".format(server.port)
    try:
        payload = {"action": "stop"}
        statuses = [
            request("POST", base + "/incidents/7F3A/decide", token="secret", payload=payload)[0]
            for _ in range(3)
        ]
        assert statuses == [200, 200, 429]
    finally:
        server.stop()


def test_label_validation_and_missing(api):
    runtime, base = api
    runtime.label_result = (False, "invalid_label")
    status, body, _ = request(
        "POST", base + "/incidents/7F3A/label", token="secret", payload={"label": "bad"}
    )
    assert status == 400
    assert json.loads(body)["code"] == "invalid_label"

    runtime.label_result = (False, "incident_not_found")
    status, _, _ = request(
        "POST", base + "/incidents/MISSING/label", token="secret", payload={"label": "unknown"}
    )
    assert status == 404

    runtime.label_result = (True, "ok")
    status, _, _ = request(
        "POST", base + "/incidents/7F3A/label", token="secret", payload={"label": "true_positive"}
    )
    assert status == 200


def test_unknown_paths(api):
    _, base = api
    assert request("GET", base + "/nope", token="secret")[0] == 404
    assert request("POST", base + "/nope", token="secret", payload={})[0] == 404


def test_server_requires_token():
    with pytest.raises(ValueError):
        LocalApiServer(FakeRuntime(), HttpConfig(), token="")
