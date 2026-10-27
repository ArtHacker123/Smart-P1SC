from __future__ import annotations

import itertools
import json
import logging
import ssl
import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterator, Optional, Set, Tuple

from .config import MqttConfig
from .models import (
    CommandResult,
    GcodeState,
    PrinterSnapshot,
)

try:
    import paho.mqtt.client as paho_mqtt
except ImportError:  # pragma: no cover - exercised only when dependency is absent
    paho_mqtt = None  # type: ignore

LOGGER = logging.getLogger(__name__)


class MqttError(Exception):
    pass


class MqttConnectionError(MqttError):
    pass


class MqttCommandError(MqttError):
    pass


def parse_ack(payload: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    section = payload.get("print")
    if not isinstance(section, dict):
        section = payload.get("pushing")
    if not isinstance(section, dict):
        return None
    sequence_id = section.get("sequence_id")
    result = section.get("result")
    if sequence_id is None or result is None:
        return None
    return str(sequence_id), str(result).lower()


def _new_paho_client(client_id: str) -> Any:
    if paho_mqtt is None:
        raise MqttConnectionError("paho-mqtt 未安装：pip install paho-mqtt")
    try:
        from paho.mqtt.enums import CallbackAPIVersion

        return paho_mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION1, client_id=client_id
        )
    except ImportError:
        return paho_mqtt.Client(client_id=client_id)


class PrinterMqttClient:
    def __init__(
        self,
        host: str,
        serial: str,
        access_code: str,
        config: MqttConfig,
        client_factory: Optional[Callable[[str], Any]] = None,
        on_snapshot: Optional[Callable[[PrinterSnapshot], None]] = None,
        on_raw: Optional[Callable[[Dict[str, Any]], None]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.host = host
        self.serial = serial
        self.access_code = access_code
        self.config = config
        self._client_factory = client_factory or _new_paho_client
        self._on_snapshot = on_snapshot
        self._on_raw = on_raw
        self.logger = logger or LOGGER

        self.report_topic = "{}/{}/report".format(config.topic_prefix, serial)
        self.request_topic = "{}/{}/request".format(config.topic_prefix, serial)

        self._client: Optional[Any] = None
        self._snapshot: Optional[PrinterSnapshot] = None
        self._acks: Dict[str, str] = {}
        self._condition = threading.Condition()
        self._connected = threading.Event()
        self._stopping = False

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        if self._client is not None:
            return
        client_id = "smart-p1sc-{}".format(uuid.uuid4().hex[:12])
        client = self._client_factory(client_id)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.username_pw_set("bblp", self.access_code)
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)
        client.connect_async(self.host, self.config.port, self.config.keepalive_seconds)
        self._client = client
        self._stopping = False
        client.loop_start()
        self.logger.info(
            "mqtt_connecting",
            extra={"host": self.host, "port": self.config.port, "serial": self.serial},
        )

    def stop(self) -> None:
        self._stopping = True
        self._connected.clear()
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.loop_stop()
            except Exception:
                self.logger.debug("mqtt_loop_stop_failed", exc_info=True)
            try:
                client.disconnect()
            except Exception:
                self.logger.debug("mqtt_disconnect_failed", exc_info=True)
        with self._condition:
            self._condition.notify_all()
        self.logger.info("mqtt_stopped", extra={"serial": self.serial})

    def wait_until_connected(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    def latest_snapshot(self) -> Optional[PrinterSnapshot]:
        with self._condition:
            return self._snapshot

    def wait_for_state(
        self, states: Set[GcodeState], timeout: float
    ) -> Optional[PrinterSnapshot]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                snapshot = self._snapshot
                if snapshot is not None and snapshot.gcode_state in states:
                    return snapshot
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return snapshot
                self._condition.wait(remaining)

    def wait_for_ack(self, sequence_id: str, timeout: float) -> Optional[str]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                ack = self._acks.get(sequence_id)
                if ack is not None:
                    return ack
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def wait_for_command(
        self, sequence_id: str, expected_states: Set[GcodeState], timeout: float
    ) -> Tuple[str, Optional[PrinterSnapshot]]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                snapshot = self._snapshot
                if snapshot is not None and snapshot.gcode_state in expected_states:
                    return "state", snapshot
                ack = self._acks.get(sequence_id)
                if ack in ("failed", "error", "fail"):
                    return "ack_failed", snapshot
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "timeout", snapshot
                self._condition.wait(remaining)

    def publish_command(
        self, command: str, sequence_id: str, param: Optional[str] = None
    ) -> None:
        client = self._client
        if client is None:
            raise MqttConnectionError("MQTT 尚未启动")
        section: Dict[str, Any] = {"sequence_id": sequence_id, "command": command}
        if param is not None:
            section["param"] = param
        payload = {"print": section}
        info = client.publish(self.request_topic, json.dumps(payload), qos=1)
        rc = getattr(info, "rc", 0)
        if rc not in (0, None):
            raise MqttCommandError("MQTT 发布命令失败: rc={}".format(rc))
        wait_for_publish = getattr(info, "wait_for_publish", None)
        if callable(wait_for_publish):
            try:
                wait_for_publish(timeout=self.config.command_timeout_seconds)
            except (ValueError, RuntimeError) as exc:
                raise MqttCommandError("MQTT 命令未确认发布: {}".format(exc)) from exc

    def _on_connect(self, client: Any, userdata: Any, flags: Any, rc: Any) -> None:
        try:
            rc_value = int(rc)
        except (TypeError, ValueError):
            rc_value = 1
        if rc_value == 0:
            self._connected.set()
            client.subscribe(self.report_topic, qos=1)
            self.logger.info(
                "mqtt_connected",
                extra={"host": self.host, "topic": self.report_topic},
            )
        else:
            self._connected.clear()
            self.logger.error("mqtt_connect_failed", extra={"rc": rc_value})

    def _on_disconnect(self, client: Any, userdata: Any, rc: Any) -> None:
        self._connected.clear()
        if not self._stopping:
            self.logger.warning("mqtt_disconnected", extra={"rc": rc})

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        payload_bytes = getattr(message, "payload", b"")
        if isinstance(payload_bytes, str):
            text = payload_bytes
        else:
            try:
                text = payload_bytes.decode("utf-8")
            except UnicodeDecodeError:
                self.logger.warning("mqtt_message_not_utf8")
                return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            self.logger.warning("mqtt_message_not_json", extra={"length": len(text)})
            return
        if not isinstance(payload, dict):
            return

        ack = parse_ack(payload)
        callback_snapshot: Optional[PrinterSnapshot] = None
        with self._condition:
            if ack is not None:
                self._acks[ack[0]] = ack[1]
            self._snapshot = PrinterSnapshot.from_report(payload, self._snapshot)
            callback_snapshot = self._snapshot
            self._condition.notify_all()

        if callback_snapshot is not None and self._on_snapshot is not None:
            try:
                self._on_snapshot(callback_snapshot)
            except Exception:
                self.logger.exception("mqtt_snapshot_callback_failed")
        if self._on_raw is not None:
            try:
                self._on_raw(payload)
            except Exception:
                self.logger.exception("mqtt_raw_callback_failed")


class PrinterCommandExecutor:
    def __init__(
        self,
        client: PrinterMqttClient,
        config: MqttConfig,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.client = client
        self.config = config
        self.logger = logger or LOGGER
        self._sequence: Iterator[int] = itertools.count(1)

    def pause(self) -> CommandResult:
        return self._execute("pause", {GcodeState.PAUSE}, require_active=True)

    def resume(self) -> CommandResult:
        return self._execute(
            "resume", {GcodeState.RUNNING, GcodeState.PREPARE}, require_active=True
        )

    def stop(self) -> CommandResult:
        return self._execute(
            "stop", {GcodeState.IDLE, GcodeState.FINISH, GcodeState.FAILED}, require_active=False
        )

    def hold(self, nozzle_temperature_c: int, bed_temperature_c: Optional[float] = None) -> CommandResult:
        lines = ["M104 S{}".format(int(nozzle_temperature_c))]
        if bed_temperature_c is not None and bed_temperature_c > 0:
            lines.append("M140 S{}".format(int(round(bed_temperature_c))))
        return self.send_gcode("\n".join(lines) + "\n", label="hold")

    def send_gcode(self, gcode: str, label: str = "gcode_line") -> CommandResult:
        before_snapshot = self.client.latest_snapshot()
        before_state = (
            before_snapshot.gcode_state if before_snapshot is not None else GcodeState.UNKNOWN
        )
        sequence_id = str(next(self._sequence))
        try:
            self.client.publish_command("gcode_line", sequence_id, param=gcode)
        except Exception as exc:
            error = "{}: {}".format(type(exc).__name__, exc)
            self.logger.warning("mqtt_gcode_publish_failed", extra={"error": error})
            return CommandResult(
                command=label,
                success=False,
                sequence_id=sequence_id,
                state_before=before_state,
                state_after=before_state,
                error=error,
                attempts=1,
            )

        ack = self.client.wait_for_ack(sequence_id, self.config.command_timeout_seconds)
        success = ack in ("success", "ok")
        return CommandResult(
            command=label,
            success=success,
            sequence_id=sequence_id,
            ack_result=ack,
            state_before=before_state,
            state_after=before_state,
            error=None if success else ("ack timeout" if ack is None else "ack {}".format(ack)),
            attempts=1,
        )

    def _execute(
        self, command: str, expected_states: Set[GcodeState], require_active: bool
    ) -> CommandResult:
        before_snapshot = self.client.latest_snapshot()
        before_state = (
            before_snapshot.gcode_state if before_snapshot is not None else GcodeState.UNKNOWN
        )
        if before_state in expected_states:
            return CommandResult(
                command=command,
                success=True,
                ack_result="already_in_state",
                state_before=before_state,
                state_after=before_state,
                attempts=0,
            )
        if require_active and before_state in (
            GcodeState.IDLE,
            GcodeState.FINISH,
            GcodeState.FAILED,
        ):
            return CommandResult(
                command=command,
                success=False,
                state_before=before_state,
                state_after=before_state,
                error="printer is not printing",
            )

        attempts = max(1, self.config.command_retries)
        last_error = "command failed"
        last_ack = None
        last_snapshot = before_snapshot

        for attempt in range(1, attempts + 1):
            sequence_id = str(next(self._sequence))
            try:
                self.client.publish_command(command, sequence_id)
            except Exception as exc:
                last_error = "{}: {}".format(type(exc).__name__, exc)
                self.logger.warning(
                    "mqtt_command_publish_failed",
                    extra={"command": command, "attempt": attempt, "error": last_error},
                )
                continue

            outcome, snapshot = self.client.wait_for_command(
                sequence_id, expected_states, self.config.command_timeout_seconds
            )
            last_snapshot = snapshot
            if outcome == "state":
                after_state = snapshot.gcode_state if snapshot is not None else GcodeState.UNKNOWN
                self.logger.info(
                    "mqtt_command_succeeded",
                    extra={"command": command, "attempt": attempt, "state": after_state.value},
                )
                return CommandResult(
                    command=command,
                    success=True,
                    sequence_id=sequence_id,
                    ack_result="success",
                    state_before=before_state,
                    state_after=after_state,
                    attempts=attempt,
                )

            last_ack = outcome
            last_error = "ack failed" if outcome == "ack_failed" else "state did not change"
            self.logger.warning(
                "mqtt_command_failed",
                extra={"command": command, "attempt": attempt, "error": last_error},
            )

        after_state = (
            last_snapshot.gcode_state if last_snapshot is not None else GcodeState.UNKNOWN
        )
        return CommandResult(
            command=command,
            success=False,
            ack_result=last_ack,
            state_before=before_state,
            state_after=after_state,
            error=last_error,
            attempts=attempts,
        )
