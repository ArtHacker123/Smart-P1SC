from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid."""


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ConfigError("配置文件不存在: {}".format(path))
    except json.JSONDecodeError as exc:
        raise ConfigError("配置文件不是合法 JSON: {} ({})".format(path, exc))
    if not isinstance(data, dict):
        raise ConfigError("配置文件顶层必须是 JSON 对象")
    return data


def _section(data: Dict[str, Any], name: str) -> Dict[str, Any]:
    value = data.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("配置节 {} 必须是对象".format(name))
    return value


@dataclass
class PrinterConfig:
    host: str = ""
    serial: str = ""
    access_code_env: str = "SMART_P1SC_ACCESS_CODE"
    access_code_file: Optional[str] = None
    lan_only: bool = True

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PrinterConfig":
        return cls(
            host=str(data.get("host", "")).strip(),
            serial=str(data.get("serial", "")).strip(),
            access_code_env=str(data.get("access_code_env", "SMART_P1SC_ACCESS_CODE")),
            access_code_file=data.get("access_code_file"),
            lan_only=bool(data.get("lan_only", True)),
        )

    def access_code(self) -> Optional[str]:
        value = os.environ.get(self.access_code_env)
        if value:
            return value.strip()
        if self.access_code_file:
            path = Path(self.access_code_file).expanduser()
            if path.exists():
                return path.read_text(encoding="utf-8").strip()
        return None

    def require_access_code(self) -> str:
        value = self.access_code()
        if not value:
            raise ConfigError(
                "缺少 Access Code：请设置环境变量 {} 或配置 access_code_file".format(
                    self.access_code_env
                )
            )
        return value


@dataclass
class CameraConfig:
    port: int = 6000
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 15.0
    reconnect_min_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0
    frame_timeout_seconds: float = 10.0
    degraded_after_seconds: float = 15.0
    stall_notify_seconds: float = 60.0
    ring_buffer_frames: int = 120

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CameraConfig":
        return cls(
            port=int(data.get("port", 6000)),
            connect_timeout_seconds=float(data.get("connect_timeout_seconds", 10.0)),
            read_timeout_seconds=float(data.get("read_timeout_seconds", 15.0)),
            reconnect_min_seconds=float(data.get("reconnect_min_seconds", 1.0)),
            reconnect_max_seconds=float(data.get("reconnect_max_seconds", 30.0)),
            frame_timeout_seconds=float(data.get("frame_timeout_seconds", 10.0)),
            degraded_after_seconds=float(data.get("degraded_after_seconds", 15.0)),
            stall_notify_seconds=float(data.get("stall_notify_seconds", 60.0)),
            ring_buffer_frames=int(data.get("ring_buffer_frames", 120)),
        )


@dataclass
class MqttConfig:
    port: int = 8883
    keepalive_seconds: int = 60
    command_timeout_seconds: float = 10.0
    command_retries: int = 3
    topic_prefix: str = "device"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MqttConfig":
        return cls(
            port=int(data.get("port", 8883)),
            keepalive_seconds=int(data.get("keepalive_seconds", 60)),
            command_timeout_seconds=float(data.get("command_timeout_seconds", 10.0)),
            command_retries=int(data.get("command_retries", 3)),
            topic_prefix=str(data.get("topic_prefix", "device")),
        )


@dataclass
class DetectionConfig:
    enabled: bool = True
    backend: str = "null"
    candidate_threshold: float = 0.35
    confirm_threshold: float = 0.6
    window_frames: int = 10
    min_positive_frames: int = 3
    spatial_iou_threshold: float = 0.2
    suppression_seconds: float = 300.0
    first_layer_max: int = 1
    model_path: str = "models/spaghetti.onnx"
    model_version: str = "stub-v0"
    p1_threshold: float = 0.8
    p2_threshold: float = 0.6
    first_layer_min_area_ratio: float = 0.15
    suppression_growth_ratio: float = 0.3
    suppression_confidence_delta: float = 0.15
    min_nozzle_temp_c: float = 150.0
    input_width: int = 640
    input_height: int = 640
    input_layout: str = "nchw"
    channel_order: str = "rgb"
    normalize: bool = True
    resize_mode: str = "letterbox"
    output_format: str = "auto"
    class_count: int = 0
    class_map: Dict[str, str] = field(default_factory=dict)
    score_threshold: float = 0.25
    nms_iou_threshold: float = 0.45
    class_agnostic_nms: bool = False
    max_detections: int = 100
    box_coordinates: str = "auto"
    providers: List[str] = field(
        default_factory=lambda: ["CPUExecutionProvider"]
    )

    def __post_init__(self) -> None:
        if self.backend not in ("null", "auto", "onnx"):
            raise ConfigError("unsupported detection.backend: {}".format(self.backend))
        if self.input_layout not in ("nchw", "nhwc"):
            raise ConfigError(
                "unsupported detection.input_layout: {}".format(self.input_layout)
            )
        if self.channel_order not in ("rgb", "bgr"):
            raise ConfigError(
                "unsupported detection.channel_order: {}".format(self.channel_order)
            )
        if self.resize_mode not in ("letterbox", "stretch"):
            raise ConfigError(
                "unsupported detection.resize_mode: {}".format(self.resize_mode)
            )
        if self.output_format not in (
            "auto",
            "xyxy_score_class",
            "cxcywh_object_class",
            "cxcywh_class",
        ):
            raise ConfigError(
                "unsupported detection.output_format: {}".format(self.output_format)
            )
        if self.box_coordinates not in ("auto", "normalized", "pixels"):
            raise ConfigError(
                "unsupported detection.box_coordinates: {}".format(
                    self.box_coordinates
                )
            )
        if self.input_width <= 0 or self.input_height <= 0:
            raise ConfigError("detection input dimensions must be positive")
        if not self.class_map and self.backend == "onnx":
            raise ConfigError("detection.class_map is required for the onnx backend")
        if isinstance(self.class_map, dict):
            for key, value in self.class_map.items():
                try:
                    int(key)
                except (TypeError, ValueError):
                    raise ConfigError(
                        "detection.class_map key is not an integer: {}".format(key)
                    )
                if not isinstance(value, str):
                    raise ConfigError(
                        "detection.class_map value must be a string: {}".format(value)
                    )
        else:
            raise ConfigError("detection.class_map must be an object")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DetectionConfig":
        raw_class_map = data.get("class_map", {})
        if not isinstance(raw_class_map, dict):
            raise ConfigError("detection.class_map must be an object")
        class_map = {
            str(key): str(value) for key, value in raw_class_map.items()
        }
        raw_providers = data.get("providers", ["CPUExecutionProvider"])
        if not isinstance(raw_providers, list) or not raw_providers:
            raise ConfigError("detection.providers must be a non-empty list")
        return cls(
            enabled=bool(data.get("enabled", True)),
            backend=str(data.get("backend", "null")).lower(),
            candidate_threshold=float(data.get("candidate_threshold", 0.35)),
            confirm_threshold=float(data.get("confirm_threshold", 0.6)),
            window_frames=int(data.get("window_frames", 10)),
            min_positive_frames=int(data.get("min_positive_frames", 3)),
            spatial_iou_threshold=float(data.get("spatial_iou_threshold", 0.2)),
            suppression_seconds=float(data.get("suppression_seconds", 300.0)),
            first_layer_max=int(data.get("first_layer_max", 1)),
            model_path=str(data.get("model_path", "models/spaghetti.onnx")),
            model_version=str(data.get("model_version", "stub-v0")),
            p1_threshold=float(data.get("p1_threshold", 0.8)),
            p2_threshold=float(data.get("p2_threshold", 0.6)),
            first_layer_min_area_ratio=float(
                data.get("first_layer_min_area_ratio", 0.15)
            ),
            suppression_growth_ratio=float(data.get("suppression_growth_ratio", 0.3)),
            suppression_confidence_delta=float(
                data.get("suppression_confidence_delta", 0.15)
            ),
            min_nozzle_temp_c=float(data.get("min_nozzle_temp_c", 150.0)),
            input_width=int(data.get("input_width", 640)),
            input_height=int(data.get("input_height", 640)),
            input_layout=str(data.get("input_layout", "nchw")).lower(),
            channel_order=str(data.get("channel_order", "rgb")).lower(),
            normalize=bool(data.get("normalize", True)),
            resize_mode=str(data.get("resize_mode", "letterbox")).lower(),
            output_format=str(data.get("output_format", "auto")).lower(),
            class_count=int(data.get("class_count", 0)),
            class_map=class_map,
            score_threshold=float(data.get("score_threshold", 0.25)),
            nms_iou_threshold=float(data.get("nms_iou_threshold", 0.45)),
            class_agnostic_nms=bool(data.get("class_agnostic_nms", False)),
            max_detections=int(data.get("max_detections", 100)),
            box_coordinates=str(data.get("box_coordinates", "auto")).lower(),
            providers=[str(value) for value in raw_providers],
        )


@dataclass
class HoldConfig:
    nozzle_temperature_c: int = 150
    keep_bed_temperature: bool = True
    max_wait_minutes: float = 60.0
    reminder_minutes: List[float] = field(default_factory=lambda: [10.0, 50.0])
    fail_safe_stop_on_pause_failure: bool = True

    def __post_init__(self) -> None:
        self.reminder_minutes = [float(item) for item in self.reminder_minutes]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HoldConfig":
        reminders = data.get("reminder_minutes", [10, 50])
        if not isinstance(reminders, list):
            raise ConfigError("hold.reminder_minutes 必须是数组")
        return cls(
            nozzle_temperature_c=int(data.get("nozzle_temperature_c", 150)),
            keep_bed_temperature=bool(data.get("keep_bed_temperature", True)),
            max_wait_minutes=float(data.get("max_wait_minutes", 60.0)),
            reminder_minutes=[float(item) for item in reminders],
            fail_safe_stop_on_pause_failure=bool(
                data.get("fail_safe_stop_on_pause_failure", True)
            ),
        )


@dataclass
class StorageConfig:
    database_path: str = "data/smart_p1sc.sqlite3"
    media_dir: str = "data/media"
    retention_days: int = 30
    normal_retention_days: int = 7
    min_free_disk_mb: int = 512
    filmstrip_before: int = 10
    filmstrip_after: int = 10

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StorageConfig":
        return cls(
            database_path=str(data.get("database_path", "data/smart_p1sc.sqlite3")),
            media_dir=str(data.get("media_dir", "data/media")),
            retention_days=int(data.get("retention_days", 30)),
            normal_retention_days=int(data.get("normal_retention_days", 7)),
            min_free_disk_mb=int(data.get("min_free_disk_mb", 512)),
            filmstrip_before=int(data.get("filmstrip_before", 10)),
            filmstrip_after=int(data.get("filmstrip_after", 10)),
        )


@dataclass
class HttpConfig:
    host: str = "127.0.0.1"
    port: int = 8787
    token_env: str = "SMART_P1SC_API_TOKEN"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HttpConfig":
        return cls(
            host=str(data.get("host", "127.0.0.1")),
            port=int(data.get("port", 8787)),
            token_env=str(data.get("token_env", "SMART_P1SC_API_TOKEN")),
        )

    def api_token(self) -> Optional[str]:
        value = os.environ.get(self.token_env)
        return value.strip() if value else None


@dataclass
class OpenClawConfig:
    hook_url: str = "http://127.0.0.1:18789/hooks/print-anomaly"
    token_env: str = "SMART_P1SC_OPENCLAW_TOKEN"
    timeout_seconds: float = 10.0
    retries: int = 3
    feishu_target: str = "user:me"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OpenClawConfig":
        return cls(
            hook_url=str(data.get("hook_url", "http://127.0.0.1:18789/hooks/print-anomaly")),
            token_env=str(data.get("token_env", "SMART_P1SC_OPENCLAW_TOKEN")),
            timeout_seconds=float(data.get("timeout_seconds", 10.0)),
            retries=int(data.get("retries", 3)),
            feishu_target=str(data.get("feishu_target", "user:me")),
        )

    def token(self) -> Optional[str]:
        value = os.environ.get(self.token_env)
        return value.strip() if value else None


@dataclass
class LoggingConfig:
    level: str = "INFO"
    json: bool = True
    file_path: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LoggingConfig":
        file_path = data.get("file_path")
        return cls(
            level=str(data.get("level", "INFO")).upper(),
            json=bool(data.get("json", True)),
            file_path=str(file_path) if file_path else None,
        )


@dataclass
class AppConfig:
    printer: PrinterConfig = field(default_factory=PrinterConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    hold: HoldConfig = field(default_factory=HoldConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    openclaw: OpenClawConfig = field(default_factory=OpenClawConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        return cls(
            printer=PrinterConfig.from_dict(_section(data, "printer")),
            camera=CameraConfig.from_dict(_section(data, "camera")),
            mqtt=MqttConfig.from_dict(_section(data, "mqtt")),
            detection=DetectionConfig.from_dict(_section(data, "detection")),
            hold=HoldConfig.from_dict(_section(data, "hold")),
            storage=StorageConfig.from_dict(_section(data, "storage")),
            http=HttpConfig.from_dict(_section(data, "http")),
            openclaw=OpenClawConfig.from_dict(_section(data, "openclaw")),
            logging=LoggingConfig.from_dict(_section(data, "logging")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "printer": {
                "host": self.printer.host,
                "serial": self.printer.serial,
                "access_code_env": self.printer.access_code_env,
                "access_code_file": self.printer.access_code_file,
                "lan_only": self.printer.lan_only,
            },
            "camera": self.camera.__dict__,
            "mqtt": self.mqtt.__dict__,
            "detection": self.detection.__dict__,
            "hold": self.hold.__dict__,
            "storage": self.storage.__dict__,
            "http": self.http.__dict__,
            "openclaw": self.openclaw.__dict__,
            "logging": self.logging.__dict__,
        }

    def require_printer(self) -> None:
        if not self.printer.host:
            raise ConfigError("缺少 printer.host")
        if not self.printer.serial:
            raise ConfigError("缺少 printer.serial")
        self.printer.require_access_code()


def default_config_path() -> Path:
    env_path = os.environ.get("SMART_P1SC_CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    return Path("config.json")


def load_config(path: Optional[str] = None) -> AppConfig:
    config_path = Path(path).expanduser() if path else default_config_path()
    if not config_path.exists():
        raise ConfigError(
            "找不到配置文件 {}；请复制 config.example.json 为 config.json 并填写打印机信息".format(
                config_path
            )
        )
    return AppConfig.from_dict(_read_json(config_path))
