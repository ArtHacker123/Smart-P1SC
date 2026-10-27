from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import HoldConfig, OpenClawConfig
from .models import Incident

LOGGER = logging.getLogger(__name__)

TYPE_LABELS = {
    "spaghetti": "炒面/拉丝",
    "first_layer_detachment": "首层大面积脱落",
    "structural_anomaly": "结构异常候选",
}

OUTCOME_LABELS = {
    "resumed": "用户已继续打印",
    "user_stopped": "用户已停止打印",
    "auto_stopped": "超时自动停止",
    "false_positive": "误报，已继续打印",
    "control_failed": "控制失败",
    "pause_failed_stopped": "暂停失败，已执行安全停止",
}

Transport = Callable[[str, bytes, Dict[str, str], float], Tuple[int, bytes]]


class NotificationError(Exception):
    pass


def incident_notification_key(
    incident: Incident, event_type: str, extra: Optional[Dict[str, Any]] = None
) -> str:
    key = "{}:{}".format(incident.id, event_type)
    if event_type == "reminder" and (extra or {}).get("minutes") is not None:
        key += ":{}".format((extra or {})["minutes"])
    return key


def system_notification_key(event_type: str) -> str:
    return "system:{}:{}".format(event_type, int(time.time() // 60))


@dataclass(frozen=True)
class ReplyCommand:
    action: Optional[str]
    short_code: Optional[str]
    raw: str
    error: Optional[str] = None


def _type_label(kind: str) -> str:
    return TYPE_LABELS.get(kind, kind)


def _actions_for(short_code: str, event_type: str) -> List[str]:
    if event_type == "resolved":
        return []
    if event_type == "observation":
        return ["查看 {}".format(short_code)]
    return [
        "继续 {}".format(short_code),
        "停止 {}".format(short_code),
        "误报 {}".format(short_code),
        "查看 {}".format(short_code),
    ]


def format_incident_text(
    incident: Incident,
    event_type: str,
    hold_config: Optional[HoldConfig] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    extra = extra or {}
    hold_config = hold_config or HoldConfig()
    lines: List[str] = []

    if event_type == "holding":
        lines.append("检测到打印异常，已自动暂停。")
    elif event_type == "observation":
        lines.append("检测到可疑打印异常，继续观察（未自动暂停）。")
    elif event_type == "reminder":
        lines.append("打印仍处于暂停等待状态，请尽快回复。")
    elif event_type == "resolved":
        outcome = OUTCOME_LABELS.get(incident.outcome or "", incident.outcome or incident.state.value)
        lines.append("打印异常事件已结束：{}。".format(outcome))
    elif event_type == "error":
        lines.append("打印异常处理失败，需要立即人工检查。")
    else:
        lines.append("打印异常事件更新。")

    lines.append("短码: {}".format(incident.short_code))
    lines.append(
        "类型: {} ({})".format(_type_label(incident.kind), incident.severity.value)
    )
    if incident.layer_num is not None:
        if incident.total_layers:
            lines.append("层号: {}/{}".format(incident.layer_num, incident.total_layers))
        else:
            lines.append("层号: {}".format(incident.layer_num))
    lines.append("置信度: {:.0%}".format(incident.confidence))

    if event_type in ("holding", "reminder"):
        lines.append(
            "保持: 喷嘴 {}°C，热床保持".format(hold_config.nozzle_temperature_c)
        )
        remaining = extra.get("remaining_minutes")
        if remaining is not None:
            lines.append("自动停止倒计时: {:.0f} 分钟".format(max(0.0, float(remaining))))
        else:
            lines.append(
                "自动停止: {} 分钟无回复后".format(int(hold_config.max_wait_minutes))
            )
    if event_type == "reminder":
        lines.append(
            "已等待: {:.0f} 分钟".format(float(extra.get("elapsed_minutes", 0)))
        )
    if incident.area_growth is not None:
        lines.append("区域增长: {:+.0%}".format(incident.area_growth))
    if extra.get("message"):
        lines.append(str(extra["message"]))

    actions = _actions_for(incident.short_code, event_type)
    if actions:
        lines.append("回复: " + " / ".join(actions))
    return "\n".join(lines)


def _default_transport(
    url: str, body: bytes, headers: Dict[str, str], timeout: float
) -> Tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()
    except urllib.error.URLError as exc:
        raise NotificationError(str(exc)) from exc


class OpenClawNotifier:
    def __init__(
        self,
        config: OpenClawConfig,
        hold_config: HoldConfig,
        transport: Optional[Transport] = None,
        sleep: Optional[Callable[[float], None]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self.hold_config = hold_config
        self._transport = transport or _default_transport
        self._sleep = sleep or time.sleep
        self.logger = logger or LOGGER

    def notify_event(
        self,
        event_type: str,
        incident: Incident,
        extra: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> bool:
        extra = extra or {}
        text = format_incident_text(incident, event_type, self.hold_config, extra)
        payload: Dict[str, Any] = {
            "event": event_type,
            "incident": incident.to_dict(),
            "text": text,
            "actions": _actions_for(incident.short_code, event_type),
            "filmstrip_dir": incident.filmstrip_dir,
            "source": "smart-p1sc",
        }
        if extra:
            payload["details"] = extra
        key = idempotency_key or incident_notification_key(incident, event_type, extra)
        return self._post(payload, key, event_type, incident.id)

    def notify_system(
        self,
        event_type: str,
        text: str,
        details: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> bool:
        payload: Dict[str, Any] = {
            "event": event_type,
            "text": text,
            "actions": [],
            "source": "smart-p1sc",
            "details": details or {},
        }
        key = idempotency_key or system_notification_key(event_type)
        return self._post(payload, key, event_type, "system")

    def _post(
        self,
        payload: Dict[str, Any],
        idempotency_key: str,
        event_type: str,
        incident_id: str,
    ) -> bool:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "X-Idempotency-Key": idempotency_key,
            "User-Agent": "smart-p1sc/0.1",
        }
        token = self.config.token()
        if token:
            headers["Authorization"] = "Bearer {}".format(token)

        attempts = max(1, int(self.config.retries))
        last_error: Optional[str] = None
        for attempt in range(1, attempts + 1):
            try:
                status, response_body = self._transport(
                    self.config.hook_url,
                    body,
                    headers,
                    self.config.timeout_seconds,
                )
            except Exception as exc:
                last_error = "{}: {}".format(type(exc).__name__, exc)
            else:
                if 200 <= status < 300:
                    self.logger.info(
                        "openclaw_notification_sent",
                        extra={
                            "event": event_type,
                            "incident_id": incident_id,
                            "attempt": attempt,
                            "status": status,
                        },
                    )
                    return True
                last_error = "http {}: {}".format(
                    status, response_body[:200].decode("utf-8", "replace")
                )
                if 400 <= status < 500 and status != 429:
                    self.logger.error(
                        "openclaw_notification_rejected",
                        extra={
                            "event": event_type,
                            "incident_id": incident_id,
                            "status": status,
                            "error": last_error,
                        },
                    )
                    return False

            self.logger.warning(
                "openclaw_notification_failed",
                extra={
                    "event": event_type,
                    "incident_id": incident_id,
                    "attempt": attempt,
                    "error": last_error,
                },
            )
            if attempt < attempts:
                self._sleep(min(2 ** (attempt - 1), 10))

        self.logger.error(
            "openclaw_notification_exhausted",
            extra={
                "event": event_type,
                "incident_id": incident_id,
                "error": last_error,
            },
        )
        return False


_RESUME_WORDS = ("继续", "恢复", "resume", "go")
_STOP_WORDS = ("停止", "停下", "stop", "abort", "cancel")
_FALSE_POSITIVE_WORDS = ("误报", "false positive", "false_positive", "fp")
_VIEW_WORDS = ("查看", "状态", "view", "show", "status")


def parse_reply(
    text: str, open_incidents: Optional[List[Incident]] = None
) -> ReplyCommand:
    raw = text or ""
    lowered = raw.strip().lower()
    action: Optional[str] = None
    if any(word in lowered for word in _FALSE_POSITIVE_WORDS):
        action = "false_positive"
    elif any(word in lowered for word in _RESUME_WORDS):
        action = "resume"
    elif any(word in lowered for word in _STOP_WORDS):
        action = "stop"
    elif any(word in lowered for word in _VIEW_WORDS):
        action = "view"

    codes = re.findall(r"\b[0-9A-F]{4}\b", raw)
    if not codes:
        codes = [
            code
            for code in re.findall(r"\b[0-9a-f]{4}\b", raw)
            if any(character.isdigit() for character in code)
        ]
    if len(codes) > 1:
        return ReplyCommand(action=action, short_code=None, raw=raw, error="multiple_codes")
    short_code = codes[0].upper() if codes else None

    if action is None:
        return ReplyCommand(action=None, short_code=short_code, raw=raw, error="unknown_action")

    if short_code is None:
        if open_incidents is None:
            return ReplyCommand(action=action, short_code=None, raw=raw, error="missing_short_code")
        if len(open_incidents) == 0:
            return ReplyCommand(action=action, short_code=None, raw=raw, error="no_open_incident")
        if len(open_incidents) > 1:
            return ReplyCommand(
                action=action, short_code=None, raw=raw, error="multiple_open_incidents"
            )
        short_code = open_incidents[0].short_code

    return ReplyCommand(action=action, short_code=short_code, raw=raw)
