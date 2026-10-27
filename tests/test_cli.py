import json

import pytest

from smart_p1_sc.cli import main
from smart_p1_sc.config import HttpConfig
from smart_p1_sc.http_server import LocalApiServer
from smart_p1_sc.models import DecisionResult, Frame, utcnow


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.decision_result = DecisionResult(
            success=True, code="ok", message="已继续打印", incident_id="inc-1"
        )
        self.label_result = (True, "ok")
        self.frame = Frame(seq=1, monotonic_ts=0.0, wall_ts=utcnow(), jpeg=b"jpeg-bytes")

    def status(self):
        return {
            "daemon": "running",
            "printer": {"gcode_state": "RUNNING", "progress_percent": 42},
        }

    def list_incidents(self, open_only, limit):
        self.calls.append(("list", open_only, limit))
        return [{"short_code": "7F3A", "severity": "P1", "state": "HOLDING", "kind": "spaghetti", "confidence": 0.9}]

    def get_incident_detail(self, reference):
        if reference == "MISSING":
            return None
        return {
            "incident": {"short_code": "7F3A", "severity": "P1", "state": "HOLDING", "kind": "spaghetti", "confidence": 0.9},
            "events": [{"type": "detected"}],
            "labels": [],
        }

    def decide(self, reference, action, actor, note, short_code=None, idempotency_key=None):
        self.calls.append(
            ("decide", reference, action, actor, note, short_code, idempotency_key)
        )
        return self.decision_result

    def label(self, reference, label, actor, note):
        self.calls.append(("label", reference, label, actor, note))
        success, code = self.label_result
        return success, code

    def latest_frame(self):
        return self.frame

    def read_incident_frame(self, reference, index):
        return self.frame.jpeg


@pytest.fixture()
def cli(tmp_path, monkeypatch):
    runtime = FakeRuntime()
    server = LocalApiServer(runtime, HttpConfig(host="127.0.0.1", port=0), token="secret")
    server.start()
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "http": {
                    "host": "127.0.0.1",
                    "port": server.port,
                    "token_env": "TEST_API_TOKEN",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_API_TOKEN", "secret")
    yield runtime, str(config_path), tmp_path
    server.stop()


def run(capsys, *args):
    code = main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_status_json(cli, capsys):
    _, config, _ = cli
    code, out, _ = run(capsys, "--config", config, "--json", "status")
    assert code == 0
    data = json.loads(out)
    assert data["printer"]["gcode_state"] == "RUNNING"


def test_status_human(cli, capsys):
    _, config, _ = cli
    code, out, _ = run(capsys, "--config", config, "status")
    assert code == 0
    assert "daemon: running" in out
    assert "gcode_state: RUNNING" in out


def test_health(cli, capsys):
    _, config, _ = cli
    code, out, _ = run(capsys, "--config", config, "health")
    assert code == 0
    assert out.strip() == "ok"


def test_incidents_json_and_open_flag(cli, capsys):
    runtime, config, _ = cli
    code, out, _ = run(capsys, "--config", config, "--json", "incidents", "--open", "--limit", "5")
    assert code == 0
    assert runtime.calls[-1] == ("list", True, 5)
    assert json.loads(out)["incidents"][0]["short_code"] == "7F3A"


def test_incident_show_and_missing(cli, capsys):
    _, config, _ = cli
    code, out, _ = run(capsys, "--config", config, "--json", "incident", "show", "7F3A")
    assert code == 0
    assert json.loads(out)["incident"]["short_code"] == "7F3A"

    code, _, err = run(capsys, "--config", config, "incident", "show", "MISSING")
    assert code == 1
    assert "incident_not_found" in err


def test_incident_decide(cli, capsys):
    runtime, config, _ = cli
    code, out, _ = run(
        capsys,
        "--config",
        config,
        "--json",
        "incident",
        "decide",
        "7F3A",
        "--action",
        "resume",
        "--code",
        "7F3A",
    )
    assert code == 0
    assert json.loads(out)["message"] == "已继续打印"
    assert runtime.calls[-1][:3] == ("decide", "7F3A", "resume")
    assert runtime.calls[-1][5] == "7F3A"
    assert runtime.calls[-1][6]


def test_incident_decide_conflict(cli, capsys):
    runtime, config, _ = cli
    runtime.decision_result = DecisionResult(
        success=False, code="already_resolved", message="done", incident_id="inc-1"
    )
    code, _, err = run(
        capsys,
        "--config",
        config,
        "incident",
        "decide",
        "7F3A",
        "--action",
        "stop",
        "--code",
        "7F3A",
    )
    assert code == 4
    assert "already_resolved" in err


def test_incident_label_invalid(cli, capsys):
    runtime, config, _ = cli
    runtime.label_result = (False, "invalid_label")
    code, _, err = run(
        capsys, "--config", config, "incident", "label", "7F3A", "--label", "unknown"
    )
    assert code == 2
    assert "invalid_label" in err


def test_snapshot_saves_file(cli, capsys):
    _, config, tmp_path = cli
    output = tmp_path / "nested" / "snap.jpg"
    code, out, _ = run(
        capsys, "--config", config, "--json", "snapshot", "--output", str(output)
    )
    assert code == 0
    assert json.loads(out)["bytes"] == len(b"jpeg-bytes")
    assert output.read_bytes() == b"jpeg-bytes"


def test_missing_config_returns_invalid(tmp_path, capsys):
    code, _, err = run(capsys, "--config", str(tmp_path / "nope.json"), "status")
    assert code == 2
    assert "config" in err


def test_missing_token_returns_auth(cli, capsys, monkeypatch):
    _, config, _ = cli
    monkeypatch.delenv("TEST_API_TOKEN")
    code, _, err = run(capsys, "--config", config, "status")
    assert code == 5
    assert "missing_token" in err


def test_wrong_token_returns_auth(cli, capsys, monkeypatch):
    _, config, _ = cli
    monkeypatch.setenv("TEST_API_TOKEN", "wrong")
    code, _, err = run(capsys, "--config", config, "status")
    assert code == 5
    assert "unauthorized" in err


def test_daemon_unreachable(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"http": {"host": "127.0.0.1", "port": 1, "token_env": "TEST_API_TOKEN"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_API_TOKEN", "secret")
    code, _, err = run(capsys, "--config", str(config_path), "status")
    assert code == 3
    assert "daemon_unreachable" in err
