import json

import pytest

from smart_p1_sc.config import AppConfig, ConfigError, load_config


def write_config(tmp_path, payload):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_config_defaults_and_values(tmp_path, monkeypatch):
    monkeypatch.delenv("SMART_P1SC_ACCESS_CODE", raising=False)
    path = write_config(
        tmp_path,
        {
            "printer": {
                "host": "192.168.1.10",
                "serial": "01P00A000000001",
                "access_code_env": "TEST_P1_ACCESS_CODE",
            },
            "hold": {"max_wait_minutes": 45, "reminder_minutes": [5, 40]},
            "detection": {"confirm_threshold": 0.7},
        },
    )
    config = load_config(str(path))
    assert config.printer.host == "192.168.1.10"
    assert config.printer.serial == "01P00A000000001"
    assert config.hold.max_wait_minutes == 45.0
    assert config.hold.reminder_minutes == [5.0, 40.0]
    assert config.detection.confirm_threshold == 0.7
    assert config.camera.port == 6000


def test_access_code_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_P1_ACCESS_CODE", "12345678")
    path = write_config(
        tmp_path,
        {
            "printer": {
                "host": "192.168.1.10",
                "serial": "01P00A000000001",
                "access_code_env": "TEST_P1_ACCESS_CODE",
            }
        },
    )
    config = load_config(str(path))
    assert config.printer.access_code() == "12345678"
    config.require_printer()


def test_access_code_from_file(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_P1_ACCESS_CODE", raising=False)
    secret = tmp_path / "access_code.txt"
    secret.write_text("87654321\n", encoding="utf-8")
    path = write_config(
        tmp_path,
        {
            "printer": {
                "host": "192.168.1.10",
                "serial": "01P00A000000001",
                "access_code_env": "TEST_P1_ACCESS_CODE",
                "access_code_file": str(secret),
            }
        },
    )
    config = load_config(str(path))
    assert config.printer.access_code() == "87654321"


def test_missing_access_code_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_P1_ACCESS_CODE", raising=False)
    path = write_config(
        tmp_path,
        {
            "printer": {
                "host": "192.168.1.10",
                "serial": "01P00A000000001",
                "access_code_env": "TEST_P1_ACCESS_CODE",
            }
        },
    )
    config = load_config(str(path))
    with pytest.raises(ConfigError):
        config.require_printer()


def test_missing_config_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "nope.json"))


def test_invalid_section_raises(tmp_path):
    path = write_config(tmp_path, {"camera": []})
    with pytest.raises(ConfigError):
        load_config(str(path))


def test_to_dict_does_not_include_access_code(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_P1_ACCESS_CODE", "supersecret")
    path = write_config(
        tmp_path,
        {
            "printer": {
                "host": "192.168.1.10",
                "serial": "01P00A000000001",
                "access_code_env": "TEST_P1_ACCESS_CODE",
            }
        },
    )
    config = load_config(str(path))
    assert "supersecret" not in json.dumps(config.to_dict())


def test_app_config_defaults_are_valid():
    config = AppConfig()
    assert config.hold.max_wait_minutes == 60.0
    assert config.detection.enabled is True
    assert config.detection.backend == "null"
    assert config.detection.resize_mode == "letterbox"
    assert config.detection.providers == ["CPUExecutionProvider"]


def test_load_config_onnx_detection_settings(tmp_path):
    path = write_config(
        tmp_path,
        {
            "detection": {
                "backend": "onnx",
                "class_count": 3,
                "class_map": {
                    "0": "spaghetti",
                    "1": "first_layer_detachment",
                    "2": "structural_anomaly",
                },
                "input_width": 320,
                "input_layout": "nhwc",
                "providers": ["CUDAExecutionProvider", "CPUExecutionProvider"],
            }
        },
    )
    detection = load_config(str(path)).detection
    assert detection.backend == "onnx"
    assert detection.class_count == 3
    assert detection.class_map["1"] == "first_layer_detachment"
    assert detection.input_width == 320
    assert detection.input_layout == "nhwc"
    assert detection.providers == [
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]


def test_invalid_detection_backend_raises(tmp_path):
    path = write_config(tmp_path, {"detection": {"backend": "torch"}})
    with pytest.raises(ConfigError):
        load_config(str(path))


def test_invalid_resize_mode_raises(tmp_path):
    path = write_config(tmp_path, {"detection": {"resize_mode": "crop"}})
    with pytest.raises(ConfigError):
        load_config(str(path))
