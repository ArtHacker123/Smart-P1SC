import json

import pytest

from smart_p1_sc import diagnostics
from smart_p1_sc.config import AppConfig
from smart_p1_sc.models import GcodeState, PrinterSnapshot


@pytest.fixture()
def config(tmp_path, monkeypatch):
    cfg = AppConfig()
    cfg.printer.host = "printer.local"
    cfg.printer.serial = "SERIAL"
    cfg.printer.access_code_env = "TEST_ACCESS_CODE"
    cfg.http.token_env = "TEST_API_TOKEN"
    cfg.openclaw.token_env = "TEST_OPENCLAW_TOKEN"
    cfg.storage.database_path = str(tmp_path / "data" / "db.sqlite3")
    cfg.storage.media_dir = str(tmp_path / "data" / "media")
    cfg.detection.model_path = str(tmp_path / "missing.onnx")
    monkeypatch.setenv("TEST_ACCESS_CODE", "12345678")
    monkeypatch.setenv("TEST_API_TOKEN", "api-token")
    monkeypatch.setenv("TEST_OPENCLAW_TOKEN", "openclaw-token")
    return cfg


def test_doctor_ok_with_warning_for_missing_model(config):
    result = diagnostics.doctor(config)
    assert result["ok"] is True
    assert result["checks"]["printer.host"]["ok"] is True
    assert result["checks"]["http.api_token"]["ok"] is True
    assert result["checks"]["detection.model_path"]["ok"] is False
    assert result["issues"][0]["severity"] == "warning"


def test_doctor_reports_missing_secrets(tmp_path, monkeypatch):
    cfg = AppConfig()
    cfg.storage.database_path = str(tmp_path / "db.sqlite3")
    cfg.storage.media_dir = str(tmp_path / "media")
    cfg.http.token_env = "MISSING_API_TOKEN"
    cfg.printer.access_code_env = "MISSING_ACCESS_CODE"
    monkeypatch.delenv("MISSING_API_TOKEN", raising=False)
    monkeypatch.delenv("MISSING_ACCESS_CODE", raising=False)
    result = diagnostics.doctor(cfg)
    assert result["ok"] is False
    failed = {item["check"] for item in result["issues"] if item["severity"] == "error"}
    assert {"printer.host", "printer.serial", "printer.access_code", "http.api_token"} <= failed


def test_probe_camera_ok(config, monkeypatch):
    monkeypatch.setattr(
        diagnostics, "capture_snapshot", lambda host, code, cfg: b"\xff\xd8" + b"x" * 200 + b"\xff\xd9"
    )
    result = diagnostics.probe_camera(config)
    assert result["ok"] is True
    assert result["jpeg"] is True
    assert result["bytes"] == 204


def test_probe_camera_invalid_frame(config, monkeypatch):
    monkeypatch.setattr(diagnostics, "capture_snapshot", lambda host, code, cfg: b"not-a-jpeg")
    result = diagnostics.probe_camera(config)
    assert result["ok"] is False
    assert "JPEG" in result["error"]


def test_probe_camera_error(config, monkeypatch):
    def boom(host, code, cfg):
        raise OSError("connection refused")

    monkeypatch.setattr(diagnostics, "capture_snapshot", boom)
    result = diagnostics.probe_camera(config)
    assert result["ok"] is False
    assert "connection refused" in result["error"]


class FakeMqtt:
    snapshot = None
    connected = True

    def __init__(self, host, serial, code, cfg):
        self.host = host

    def start(self):
        pass

    def stop(self):
        pass

    def wait_until_connected(self, timeout):
        return self.connected

    def latest_snapshot(self):
        return self.snapshot


def test_probe_mqtt_ok(config, monkeypatch):
    FakeMqtt.snapshot = PrinterSnapshot(gcode_state=GcodeState.RUNNING, nozzle_temp=210.0)
    FakeMqtt.connected = True
    monkeypatch.setattr(diagnostics, "PrinterMqttClient", FakeMqtt)
    result = diagnostics.probe_mqtt(config, timeout=0.5)
    assert result["ok"] is True
    assert result["snapshot"]["gcode_state"] == "RUNNING"


def test_probe_mqtt_connect_timeout(config, monkeypatch):
    FakeMqtt.snapshot = None
    FakeMqtt.connected = False
    monkeypatch.setattr(diagnostics, "PrinterMqttClient", FakeMqtt)
    result = diagnostics.probe_mqtt(config, timeout=0.05)
    assert result["ok"] is False
    assert "超时" in result["error"]


def test_probe_mqtt_no_report(config, monkeypatch):
    FakeMqtt.snapshot = None
    FakeMqtt.connected = True
    monkeypatch.setattr(diagnostics, "PrinterMqttClient", FakeMqtt)
    result = diagnostics.probe_mqtt(config, timeout=0.05)
    assert result["ok"] is False
    assert "report" in result["error"]


def test_main_doctor(config, tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config.to_dict()), encoding="utf-8")
    code = diagnostics.main(["--config", str(path), "--json", "doctor"])
    out = capsys.readouterr().out
    assert code == 0
    assert json.loads(out)["ok"] is True


def test_main_missing_config(tmp_path, capsys):
    code = diagnostics.main(["--config", str(tmp_path / "nope.json"), "doctor"])
    assert code == 2
    assert "config" in capsys.readouterr().err
