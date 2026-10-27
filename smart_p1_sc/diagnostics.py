from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .camera import capture_snapshot
from .config import AppConfig, ConfigError, load_config
from .mqtt import PrinterMqttClient


def _existing_writable_parent(path: str) -> bool:
    current = Path(path).expanduser().resolve()
    if current.exists() and current.is_file():
        current = current.parent
    while not current.exists() and current != current.parent:
        current = current.parent
    return current.exists() and os.access(str(current), os.W_OK)


def doctor(config: AppConfig) -> Dict[str, Any]:
    checks: Dict[str, Any] = {}
    issues: List[Dict[str, str]] = []

    def record(name: str, ok: bool, detail: str = "", critical: bool = True) -> None:
        checks[name] = {"ok": ok, "detail": detail}
        if not ok:
            issues.append(
                {
                    "check": name,
                    "detail": detail,
                    "severity": "error" if critical else "warning",
                }
            )

    record("printer.host", bool(config.printer.host), config.printer.host or "未配置")
    record("printer.serial", bool(config.printer.serial), config.printer.serial or "未配置")
    access_code = config.printer.access_code()
    record(
        "printer.access_code",
        bool(access_code),
        "已配置" if access_code else "请设置 {} 或 access_code_file".format(
            config.printer.access_code_env
        ),
    )
    record("printer.lan_only", config.printer.lan_only, "LAN Only 模式下才能远程控制")
    api_token = config.http.api_token()
    record(
        "http.api_token",
        bool(api_token),
        "已配置" if api_token else "请设置 {}".format(config.http.token_env),
    )
    openclaw_token = config.openclaw.token()
    record(
        "openclaw.token",
        bool(openclaw_token),
        "已配置" if openclaw_token else "未配置（部分 OpenClaw 部署需要）",
        critical=False,
    )
    record(
        "storage.database",
        _existing_writable_parent(config.storage.database_path),
        config.storage.database_path,
    )
    record(
        "storage.media",
        _existing_writable_parent(config.storage.media_dir),
        config.storage.media_dir,
    )
    record(
        "detection.model_path",
        Path(config.detection.model_path).expanduser().exists()
        if config.detection.model_path
        else False,
        config.detection.model_path or "未配置",
        critical=False,
    )

    errors = [item for item in issues if item["severity"] == "error"]
    return {
        "ok": not errors,
        "checks": checks,
        "issues": issues,
    }


def probe_camera(config: AppConfig) -> Dict[str, Any]:
    started = time.monotonic()
    try:
        access_code = config.printer.require_access_code()
        jpeg = capture_snapshot(
            config.printer.host, access_code, config.camera
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": "{}: {}".format(type(exc).__name__, exc),
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    looks_like_jpeg = (
        len(jpeg) > 100 and jpeg[:2] == b"\xff\xd8" and jpeg[-2:] == b"\xff\xd9"
    )
    return {
        "ok": looks_like_jpeg,
        "bytes": len(jpeg),
        "jpeg": looks_like_jpeg,
        "duration_seconds": round(time.monotonic() - started, 3),
        "error": None if looks_like_jpeg else "帧不是完整 JPEG",
    }


def probe_mqtt(config: AppConfig, timeout: float = 15.0) -> Dict[str, Any]:
    started = time.monotonic()
    try:
        access_code = config.printer.require_access_code()
        client = PrinterMqttClient(
            config.printer.host,
            config.printer.serial,
            access_code,
            config.mqtt,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": "{}: {}".format(type(exc).__name__, exc),
            "duration_seconds": round(time.monotonic() - started, 3),
        }

    client.start()
    try:
        if not client.wait_until_connected(timeout):
            return {
                "ok": False,
                "error": "MQTT 连接超时（LAN Only 模式？）",
                "duration_seconds": round(time.monotonic() - started, 3),
            }
        deadline = time.monotonic() + timeout
        snapshot = None
        while time.monotonic() < deadline:
            snapshot = client.latest_snapshot()
            if snapshot is not None:
                break
            time.sleep(0.2)
        if snapshot is None:
            return {
                "ok": False,
                "error": "MQTT 已连接但未收到 report 遥测",
                "duration_seconds": round(time.monotonic() - started, 3),
            }
        return {
            "ok": True,
            "snapshot": snapshot.to_dict(),
            "duration_seconds": round(time.monotonic() - started, 3),
            "error": None,
        }
    finally:
        client.stop()


def run_probe(config: AppConfig, timeout: float = 15.0) -> Dict[str, Any]:
    config_result = doctor(config)
    camera_result = probe_camera(config)
    mqtt_result = probe_mqtt(config, timeout=timeout)
    return {
        "ok": bool(
            config_result["ok"] and camera_result["ok"] and mqtt_result["ok"]
        ),
        "config": config_result,
        "camera": camera_result,
        "mqtt": mqtt_result,
    }


def _print_text(name: str, result: Dict[str, Any]) -> None:
    print("{}: {}".format(name, "ok" if result.get("ok") else "FAILED"))
    if result.get("error"):
        print("  error: {}".format(result["error"]))
    checks = result.get("checks")
    if isinstance(checks, dict):
        for check_name, check in checks.items():
            status = "ok" if check.get("ok") else "FAIL"
            print("  {}: {} ({})".format(check_name, status, check.get("detail", "")))
    if "bytes" in result:
        print("  bytes: {}".format(result["bytes"]))
    if "snapshot" in result:
        printer = result["snapshot"]
        print("  gcode_state: {}".format(printer.get("gcode_state")))
        print("  nozzle_temp: {}".format(printer.get("nozzle_temp")))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="smart-p1sc-probe",
        description="P1SC 安装前硬件与配置检查（请在 daemon 停止时运行）",
    )
    parser.add_argument("--config", help="配置文件路径（默认 config.json）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="检查配置与目录")
    probe = sub.add_parser("probe", help="检查摄像头与 MQTT")
    probe.add_argument("--timeout", type=float, default=15.0, help="单项超时秒数")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print("error: config: {}".format(exc), file=sys.stderr)
        return 2

    if args.command == "doctor":
        result = doctor(config)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            _print_text("doctor", result)
        return 0 if result["ok"] else 2

    result = run_probe(config, timeout=args.timeout)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        _print_text("probe", result)
        _print_text("config", result["config"])
        _print_text("camera", result["camera"])
        _print_text("mqtt", result["mqtt"])
    if result["ok"]:
        return 0
    if not result["config"]["ok"]:
        return 2
    return 4


if __name__ == "__main__":
    sys.exit(main())
