from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set

REDACTED = "***REDACTED***"

_STANDARD_KEYS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "message",
    "asctime",
}


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in {"access_code", "token", "api_token", "password", "secret", "authorization"}:
        return True
    return lowered.endswith(("_token", "_password", "_secret", "_access_code"))


class SecretRedactor:
    def __init__(self, secrets: Optional[Iterable[str]] = None) -> None:
        self._secrets: Set[str] = set()
        if secrets:
            for secret in secrets:
                self.register(secret)

    def register(self, value: Any) -> None:
        if value is None:
            return
        text = str(value).strip()
        if text:
            self._secrets.add(text)

    def redact_text(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        return text

    def redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, bytes):
            return "<bytes:{}>".format(len(value))
        if isinstance(value, dict):
            return {
                key: REDACTED if _is_sensitive_key(str(key)) else self.redact_value(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [self.redact_value(item) for item in value]
        return value


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": self.redactor.redact_text(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_KEYS or key.startswith("_"):
                continue
            payload[key] = (
                REDACTED if _is_sensitive_key(key) else self.redactor.redact_value(value)
            )
        if record.exc_info:
            payload["exception"] = self.redactor.redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self.redactor.redact_text(super().format(record))


def setup_logging(
    level: str = "INFO",
    json_output: bool = True,
    secrets: Optional[Iterable[str]] = None,
    file_path: Optional[str] = None,
) -> SecretRedactor:
    redactor = SecretRedactor(secrets)
    numeric_level = getattr(logging, str(level).upper(), None)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    root = logging.getLogger()
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)

    formatter = JsonFormatter(redactor) if json_output else TextFormatter(redactor)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root.addHandler(handler)

    if file_path:
        target = Path(file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            str(target), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    root.setLevel(numeric_level)

    logging.getLogger("paho").setLevel(logging.WARNING)
    return redactor


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
