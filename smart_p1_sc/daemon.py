from __future__ import annotations

import argparse
import copy
import logging
import os
import queue
import shutil
import signal
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .camera import CameraStream
from .config import AppConfig, ConfigError, load_config
from .db import Database
from .detector import (
    DetectionConfirmer,
    FrameAnalyzer,
    Detector,
    NullDetector,
)
from .http_server import ApiRuntime, LocalApiServer
from .incidents import ALLOWED_LABELS, IncidentManager
from .logging_setup import setup_logging
from .media import FilmstripStore
from .models import (
    CommandResult,
    DecisionAction,
    DecisionResult,
    Frame,
    GcodeState,
    Incident,
    PrinterSnapshot,
    utcnow,
)
from .mqtt import PrinterCommandExecutor, PrinterMqttClient
from .notifications import (
    OpenClawNotifier,
    incident_notification_key,
    system_notification_key,
)
from .onnx_detector import create_detector

LOGGER = logging.getLogger(__name__)


class DaemonAlreadyRunning(RuntimeError):
    pass


class SingleInstanceLock:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._file: Any = None

    def acquire(self) -> bool:
        if self._file is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(str(self.path), "a+b")
        try:
            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
        self._file = handle
        return True

    def release(self) -> None:
        handle = self._file
        self._file = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()


class _NotificationDispatcher:
    def __init__(
        self,
        notifier: Any,
        logger: logging.Logger,
        max_queue: int = 200,
        db: Optional[Database] = None,
        retry_seconds: float = 30.0,
    ) -> None:
        self.notifier = notifier
        self.logger = logger
        self.db = db
        self.retry_seconds = retry_seconds
        self._queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(
            maxsize=max_queue
        )
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._keys: set = set()
        self._pending = 0
        self._idle = threading.Event()
        self._idle.set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self.replay_pending(force=True)
        self._thread = threading.Thread(
            target=self._run, name="notifications", daemon=True
        )
        self._thread.start()

    def enqueue(
        self, event_type: str, incident: Incident, extra: Optional[Dict[str, Any]] = None
    ) -> None:
        extra = copy.deepcopy(extra or {})
        key = incident_notification_key(incident, event_type, extra)
        payload = {"incident": incident.to_dict(), "extra": extra}
        if not self._persist(key, "incident", event_type, incident.id, payload):
            return
        item = {
            "kind": "incident",
            "event_type": event_type,
            "incident": copy.deepcopy(incident),
            "extra": extra,
            "key": key,
        }
        self._put(item, event_type)

    def enqueue_system(
        self, event_type: str, text: str, details: Optional[Dict[str, Any]] = None
    ) -> None:
        details = copy.deepcopy(details or {})
        key = system_notification_key(event_type)
        payload = {"text": text, "details": details}
        if not self._persist(key, "system", event_type, None, payload):
            return
        item = {
            "kind": "system",
            "event_type": event_type,
            "text": text,
            "details": details,
            "key": key,
        }
        self._put(item, event_type)

    def _persist(
        self,
        key: str,
        kind: str,
        event_type: str,
        incident_id: Optional[str],
        payload: Dict[str, Any],
    ) -> bool:
        if self.db is None:
            return True
        try:
            return self.db.enqueue_notification(
                key, kind, event_type, incident_id, payload
            )
        except Exception:
            self.logger.exception(
                "notification_persist_failed", extra={"event": event_type}
            )
            return True

    def replay_pending(self, force: bool = False) -> None:
        if self.db is None:
            return
        try:
            rows = self.db.list_pending_notifications(
                limit=100,
                min_age_seconds=0.0 if force else self.retry_seconds,
            )
        except Exception:
            self.logger.exception("notification_replay_failed")
            return
        for row in rows:
            key = row["idempotency_key"]
            payload = row.get("payload") or {}
            if row["kind"] == "incident":
                try:
                    incident = Incident.from_dict(payload.get("incident") or {})
                except Exception:
                    self.logger.exception(
                        "notification_payload_invalid",
                        extra={"event": row.get("event_type")},
                    )
                    self.db.mark_notification_failed(key, "invalid_payload")
                    continue
                if not incident.id:
                    self.db.mark_notification_failed(key, "missing_incident")
                    continue
                item = {
                    "kind": "incident",
                    "event_type": row["event_type"],
                    "incident": incident,
                    "extra": payload.get("extra") or {},
                    "key": key,
                }
            else:
                item = {
                    "kind": "system",
                    "event_type": row["event_type"],
                    "text": str(payload.get("text", "")),
                    "details": payload.get("details") or {},
                    "key": key,
                }
            self._put(item, row["event_type"])

    def _put(self, item: Dict[str, Any], event_type: str) -> None:
        key = item["key"]
        with self._lock:
            if key in self._keys:
                return
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                self.logger.error(
                    "notification_queue_full", extra={"event": event_type}
                )
                return
            self._keys.add(key)
            self._pending += 1
            self._idle.clear()

    def flush(self, timeout: float = 5.0) -> bool:
        return self._idle.wait(timeout)

    def stop(self, timeout: float = 2.0) -> None:
        with self._lock:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        with self._lock:
            self._keys.clear()
            self._pending = 0
            self._idle.set()

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                self.replay_pending()
                continue
            if item is None:
                return
            key = item["key"]
            try:
                ok = self._deliver(item)
                if not ok:
                    self.logger.error(
                        "notification_delivery_failed",
                        extra={"event": item["event_type"]},
                    )
                    if self.db is not None:
                        self.db.mark_notification_failed(key, "delivery_failed")
            except Exception as exc:
                self.logger.exception("notification_dispatch_failed")
                if self.db is not None:
                    self.db.mark_notification_failed(key, str(exc))
            finally:
                with self._lock:
                    self._keys.discard(key)
                    self._pending = max(0, self._pending - 1)
                    if self._pending == 0:
                        self._idle.set()

    def _deliver(self, item: Dict[str, Any]) -> bool:
        key = item["key"]
        if item["kind"] == "incident":
            result = self.notifier.notify_event(
                item["event_type"],
                item["incident"],
                item["extra"],
                idempotency_key=key,
            )
        else:
            notify_system = getattr(self.notifier, "notify_system", None)
            if not callable(notify_system):
                self.logger.warning(
                    "notifier_missing_system_support",
                    extra={"event": item["event_type"]},
                )
                result = False
            else:
                result = notify_system(
                    item["event_type"],
                    item["text"],
                    item["details"],
                    idempotency_key=key,
                )
        if result is False:
            return False
        if self.db is not None:
            self.db.mark_notification_sent(key)
        return True


class Daemon:
    def __init__(
        self,
        config: AppConfig,
        detector: Optional[Detector] = None,
        camera: Optional[Any] = None,
        mqtt: Optional[Any] = None,
        executor: Optional[Any] = None,
        notifier: Optional[Any] = None,
        db: Optional[Database] = None,
        api_server: Optional[Any] = None,
        lock: Optional[Any] = None,
        logger: Optional[logging.Logger] = None,
        tick_interval: float = 0.2,
    ) -> None:
        self.config = config
        self.logger = logger or LOGGER
        self.tick_interval = tick_interval
        self._stop = threading.Event()

        self.db = db or Database(config.storage.database_path)
        self.camera = camera or CameraStream(
            config.printer.host,
            config.printer.require_access_code(),
            config.camera,
            logger=self.logger,
        )
        self.mqtt = mqtt or PrinterMqttClient(
            config.printer.host,
            config.printer.serial,
            config.printer.require_access_code(),
            config.mqtt,
            logger=self.logger,
        )
        self.executor = executor or PrinterCommandExecutor(self.mqtt, config.mqtt)

        self.confirmer = DetectionConfirmer(config.detection, logger=self.logger)
        self._owns_detector = detector is None
        self.detector = detector or create_detector(config.detection)
        self._model_signature = self._current_model_signature()
        self._model_error_signature: Optional[Tuple[int, int]] = None
        self._last_model_check_at = 0.0
        self.analyzer = FrameAnalyzer(self.detector, self.confirmer, logger=self.logger)
        self.store = FilmstripStore(config.storage.media_dir)
        self.notifier = notifier or OpenClawNotifier(
            config.openclaw, config.hold, logger=self.logger
        )
        self.dispatcher = _NotificationDispatcher(
            self.notifier, self.logger, db=self.db
        )

        self.incidents = IncidentManager(
            self.db,
            self.executor,
            config.hold,
            on_holding=self._on_holding,
            on_observation=self._on_observation,
            on_reminder=self._on_reminder,
            on_resolved=self._on_resolved,
            on_error=self._on_error,
            logger=self.logger,
        )

        if api_server is not None:
            self.api = api_server
        else:
            token = config.http.api_token()
            if not token:
                raise ConfigError(
                    "缺少本地 API token：请设置环境变量 {}".format(config.http.token_env)
                )
            self.api = LocalApiServer(
                self, config.http, token, logger=self.logger
            )

        default_lock = str(Path(config.storage.database_path).with_suffix(".lock"))
        self.lock = lock or SingleInstanceLock(default_lock)

        self._thread: Optional[threading.Thread] = None
        self._last_frame_seq = -1
        self._last_processed_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._started_at = utcnow()
        self._started_monotonic = time.monotonic()
        self._camera_health = "STARTING"
        self._camera_frame_age: Optional[float] = None
        self._stall_notified = False
        self._last_cleanup_at = 0.0
        self._last_disk_check_at = 0.0
        self._disk_low = False
        self._last_disk_alert_at = 0.0

    def start(self) -> None:
        if self._thread is not None:
            return
        if not self.lock.acquire():
            raise DaemonAlreadyRunning("已有 smart-p1scd 实例在运行")
        try:
            self.api.start()
            self.mqtt.start()
            self.dispatcher.start()
            self.camera.start()
        except Exception:
            self._stop_components()
            self.lock.release()
            raise
        self.cleanup_retention()
        self._thread = threading.Thread(target=self._loop, name="daemon", daemon=True)
        self._thread.start()
        self.logger.info("daemon_started")

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop.wait(0.5):
                pass
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(5.0)
        self._stop_components()
        self.logger.info("daemon_stopped")

    def _stop_components(self) -> None:
        for name, component in (
            ("camera", self.camera),
            ("mqtt", self.mqtt),
            ("api", self.api),
        ):
            try:
                component.stop()
            except Exception:
                self.logger.exception("daemon_component_stop_failed", extra={"component": name})
        try:
            self.dispatcher.stop()
        except Exception:
            self.logger.exception("notification_dispatcher_stop_failed")
        try:
            self.db.close()
        except Exception:
            self.logger.exception("database_close_failed")
        self.lock.release()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._step()
            except Exception as exc:
                self._last_error = "{}: {}".format(type(exc).__name__, exc)
                self.logger.exception("daemon_loop_error")
            now = time.monotonic()
            if now - self._last_cleanup_at >= 3600:
                self._last_cleanup_at = now
                self.cleanup_retention()
            self._stop.wait(self.tick_interval)

    def _step(self) -> None:
        snapshot = self.mqtt.latest_snapshot()
        self.incidents.tick()
        self._check_camera_health(snapshot)
        self._check_storage_health()
        self._maybe_reload_detector(snapshot)
        if not self.config.detection.enabled:
            return
        if snapshot is None or snapshot.gcode_state != GcodeState.RUNNING:
            return
        frame = self.camera.ring.latest()
        if frame is None or frame.seq == self._last_frame_seq:
            return
        self._last_frame_seq = frame.seq
        analysis = self.analyzer.process(frame, snapshot)
        self._last_processed_at = time.monotonic()
        for detection in analysis.detections:
            self.incidents.handle_detection(detection, snapshot)

    def cleanup_retention(self, now: Optional[Any] = None) -> int:
        cutoff = (now or utcnow()) - timedelta(
            days=max(0, int(self.config.storage.retention_days))
        )
        try:
            removed = self.db.purge_resolved_incidents_before(cutoff)
        except Exception:
            self.logger.exception("retention_cleanup_failed")
            return 0
        for incident_id, _ in removed:
            self.store.cleanup_incident(incident_id)
        if removed:
            self.logger.info("retention_cleanup", extra={"incidents": len(removed)})
        return len(removed)

    def storage_status(self) -> Dict[str, Any]:
        probe = Path(self.config.storage.media_dir)
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        free_mb: Optional[float] = None
        try:
            free_mb = shutil.disk_usage(str(probe)).free / (1024.0 * 1024.0)
        except OSError:
            self.logger.debug("disk_usage_failed", exc_info=True)
        minimum = max(0, int(self.config.storage.min_free_disk_mb))
        return {
            "free_mb": round(free_mb, 1) if free_mb is not None else None,
            "min_free_mb": minimum,
            "low": free_mb is not None and free_mb < minimum,
        }

    def _check_storage_health(self) -> None:
        now = time.monotonic()
        if now - self._last_disk_check_at < 60.0:
            return
        self._last_disk_check_at = now
        storage = self.storage_status()
        free_mb = storage["free_mb"]
        if free_mb is None:
            return
        if storage["low"]:
            self._disk_low = True
            if now - self._last_disk_alert_at < 3600.0:
                return
            self._last_disk_alert_at = now
            details = {"free_mb": free_mb, "min_free_mb": storage["min_free_mb"]}
            self.db.add_event(None, "daemon", "disk_low", details)
            self.logger.warning("disk_low", extra=details)
            self.dispatcher.enqueue_system(
                "disk_low",
                "磁盘剩余 {:.0f} MB，低于安全阈值 {} MB。事件证据仍会保留，请清理磁盘。".format(
                    free_mb, storage["min_free_mb"]
                ),
                details,
            )
        elif self._disk_low:
            self._disk_low = False
            self.db.add_event(
                None,
                "daemon",
                "disk_recovered",
                {"free_mb": free_mb},
            )
            self.logger.info("disk_recovered", extra={"free_mb": free_mb})

    def _current_model_signature(self) -> Optional[Tuple[int, int]]:
        if not self._owns_detector:
            return None
        if (self.config.detection.backend or "null").lower() == "null":
            return None
        try:
            stat = Path(self.config.detection.model_path).stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _maybe_reload_detector(self, snapshot: Optional[PrinterSnapshot]) -> bool:
        if not self._owns_detector:
            return False
        if (self.config.detection.backend or "null").lower() == "null":
            return False
        now = time.monotonic()
        if now - self._last_model_check_at < 30.0:
            return False
        self._last_model_check_at = now
        signature = self._current_model_signature()
        if signature is None or signature == self._model_signature:
            return False
        if signature == self._model_error_signature:
            return False
        if self.db.count_open_incidents() > 0:
            self.logger.info(
                "detector_reload_deferred", extra={"reason": "open_incidents"}
            )
            return False
        if snapshot is not None and snapshot.gcode_state == GcodeState.RUNNING:
            self.logger.info(
                "detector_reload_deferred", extra={"reason": "printer_running"}
            )
            return False

        try:
            reloaded = create_detector(self.config.detection)
        except Exception:
            self._model_error_signature = signature
            self.logger.exception("detector_reload_failed")
            return False
        if isinstance(reloaded, NullDetector):
            self._model_error_signature = signature
            self.logger.error("detector_reload_refused_null")
            return False

        self.detector = reloaded
        self.analyzer.detector = reloaded
        self.confirmer.reset()
        self._model_signature = signature
        self._model_error_signature = None
        self.logger.info(
            "detector_reloaded",
            extra={"model_version": self.config.detection.model_version},
        )
        return True

    def _check_camera_health(self, snapshot: Optional[PrinterSnapshot]) -> None:
        try:
            stats = self.camera.stats()
        except Exception:
            stats = {}
        last_frame_at = stats.get("last_frame_at")
        if last_frame_at is None:
            age = time.monotonic() - self._started_monotonic
            health = (
                "DEGRADED"
                if age >= self.config.camera.degraded_after_seconds
                else "STARTING"
            )
        else:
            age = max(0.0, time.time() - float(last_frame_at))
            healthy = age <= self.config.camera.degraded_after_seconds
            health = "OK" if healthy else "DEGRADED"
        self._camera_frame_age = age

        if health != self._camera_health:
            previous = self._camera_health
            self._camera_health = health
            if health == "DEGRADED":
                self.db.add_event(
                    None,
                    "daemon",
                    "camera_degraded",
                    {"frame_age_seconds": round(age, 1), "previous": previous},
                )
                self.logger.warning(
                    "camera_degraded",
                    extra={"frame_age_seconds": round(age, 1)},
                )
                self.dispatcher.enqueue_system(
                    "camera_degraded",
                    "摄像头已断流 {:.0f} 秒，自动检测暂时失效。请检查摄像头和网络。".format(
                        age
                    ),
                    {"frame_age_seconds": round(age, 1)},
                )
            elif health == "OK" and previous == "DEGRADED":
                self.db.add_event(
                    None,
                    "daemon",
                    "camera_recovered",
                    {"frame_age_seconds": round(age, 1)},
                )
                self.logger.info("camera_recovered")
                self.dispatcher.enqueue_system(
                    "camera_recovered",
                    "摄像头画面已恢复，自动检测继续。",
                    {"frame_age_seconds": round(age, 1)},
                )

        running = snapshot is not None and snapshot.gcode_state == GcodeState.RUNNING
        if (
            running
            and age >= self.config.camera.stall_notify_seconds
            and not self._stall_notified
        ):
            self._stall_notified = True
            self.logger.error(
                "camera_stall_while_printing",
                extra={"frame_age_seconds": round(age, 1)},
            )
            self.dispatcher.enqueue_system(
                "camera_stall_while_printing",
                "打印中摄像头已断流 {:.0f} 秒，当前无法检测异常。请尽快检查打印机或网络。".format(
                    age
                ),
                {
                    "frame_age_seconds": round(age, 1),
                    "gcode_state": snapshot.gcode_state.value,
                },
            )
        elif not running or age < self.config.camera.stall_notify_seconds:
            self._stall_notified = False

    def _on_holding(self, incident: Incident) -> None:
        self._attach_pre_frames(incident)
        self.dispatcher.enqueue("holding", incident)

    def _on_observation(self, incident: Incident) -> None:
        self._attach_pre_frames(incident)
        self.dispatcher.enqueue("observation", incident)

    def _on_reminder(self, incident: Incident, minutes: float) -> None:
        self._attach_pre_frames(incident)
        remaining = max(0.0, float(self.config.hold.max_wait_minutes) - float(minutes))
        self.dispatcher.enqueue(
            "reminder",
            incident,
            {
                "minutes": float(minutes),
                "elapsed_minutes": float(minutes),
                "remaining_minutes": remaining,
            },
        )

    def _on_resolved(self, incident: Incident) -> None:
        self._attach_post_frames(incident)
        self.dispatcher.enqueue("resolved", incident)

    def _on_error(self, incident: Incident, message: str) -> None:
        self._attach_pre_frames(incident)
        self.dispatcher.enqueue("error", incident, {"message": message})

    def _attach_pre_frames(self, incident: Incident) -> None:
        if incident.filmstrip_dir:
            return
        frames = self.camera.ring.snapshot(self.config.storage.filmstrip_before)
        if not frames:
            return
        try:
            directory = self.store.save_frames(incident.id, frames, "pre")
        except Exception:
            self.logger.exception(
                "filmstrip_save_failed", extra={"incident_id": incident.id}
            )
            return
        incident.filmstrip_dir = directory
        self.db.upsert_incident(incident)

    def _attach_post_frames(self, incident: Incident) -> None:
        frames = self.camera.ring.snapshot(self.config.storage.filmstrip_after)
        if not frames:
            return
        try:
            for frame in frames:
                self.store.append_frame(incident.id, frame, "post")
        except Exception:
            self.logger.exception(
                "filmstrip_post_save_failed", extra={"incident_id": incident.id}
            )
            return
        if not incident.filmstrip_dir:
            incident.filmstrip_dir = str(self.store.media_dir / incident.id)
            self.db.upsert_incident(incident)

    def status(self) -> Dict[str, Any]:
        snapshot = self.mqtt.latest_snapshot()
        camera_stats: Dict[str, Any] = {}
        try:
            camera_stats = self.camera.stats()
        except Exception:
            self.logger.debug("camera_stats_failed", exc_info=True)
        camera_stats["health"] = self._camera_health
        camera_stats["frame_age_seconds"] = (
            round(self._camera_frame_age, 1)
            if self._camera_frame_age is not None
            else None
        )
        payload: Dict[str, Any] = {
            "daemon": "running" if self._thread is not None else "stopped",
            "started_at": self._started_at.isoformat(),
            "camera": camera_stats,
            "mqtt": {"connected": bool(getattr(self.mqtt, "connected", False))},
            "printer": snapshot.to_dict() if snapshot is not None else None,
            "open_incidents": self.db.count_open_incidents(),
            "detection": {
                "enabled": self.config.detection.enabled,
                "backend": self.config.detection.backend,
                "loaded": not isinstance(self.detector, NullDetector),
                "model_path": self.config.detection.model_path,
                "model_version": self.config.detection.model_version,
                "providers": list(self.config.detection.providers),
                "input_width": self.config.detection.input_width,
                "input_height": self.config.detection.input_height,
            },
            "storage": self.storage_status(),
            "notifications": {"unsent": self.db.count_unsent_notifications()},
            "last_processed_at": self._last_processed_at,
            "last_error": self._last_error,
        }
        return payload

    def list_incidents(self, open_only: bool, limit: int) -> List[Dict[str, Any]]:
        return [
            incident.to_dict()
            for incident in self.db.list_incidents(limit=limit, open_only=open_only)
        ]

    def get_incident_detail(self, reference: str) -> Optional[Dict[str, Any]]:
        incident = self.incidents.get_incident(reference)
        if incident is None:
            return None
        return {
            "incident": incident.to_dict(),
            "events": self.db.list_events(incident.id),
            "labels": self.db.list_labels(incident.id),
            "frames": self.store.list_incident_frames(incident.id),
        }

    def decide(
        self,
        reference: str,
        action: str,
        actor: str,
        note: Optional[str],
        short_code: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> DecisionResult:
        try:
            decision_action = DecisionAction(action)
        except ValueError:
            return DecisionResult(
                success=False,
                code="invalid_action",
                message="不支持的处置动作: {}".format(action),
                incident_id=reference,
            )

        if idempotency_key:
            cached = self.db.get_decision(idempotency_key)
            if cached is not None:
                if cached["action"] != decision_action.value:
                    return DecisionResult(
                        success=False,
                        code="idempotency_conflict",
                        message="幂等键已用于其他动作",
                        incident_id=cached["incident_id"],
                        action=decision_action,
                    )
                return DecisionResult.from_dict(cached["result"])

        incident = self.incidents.get_incident(reference)
        if incident is not None and short_code:
            if incident.short_code.upper() != short_code.strip().upper():
                result = DecisionResult(
                    success=False,
                    code="short_code_mismatch",
                    message="事件 ID 与短码不匹配",
                    incident_id=incident.id,
                    state=incident.state,
                    action=decision_action,
                )
                if idempotency_key:
                    self.db.save_decision(
                        idempotency_key, incident.id, decision_action.value, result.to_dict()
                    )
                return result

        try:
            result = self.incidents.decide(
                reference, decision_action, actor=actor, note=note
            )
        except Exception as exc:
            self.logger.exception("decision_failed")
            result = DecisionResult(
                success=False,
                code="command_failed",
                message="{}: {}".format(type(exc).__name__, exc),
                incident_id=reference,
                action=decision_action,
            )
            if idempotency_key:
                self.db.save_decision(
                    idempotency_key, reference, decision_action.value, result.to_dict()
                )
            return result

        if idempotency_key:
            self.db.save_decision(
                idempotency_key, result.incident_id, decision_action.value, result.to_dict()
            )
        if result.success and decision_action in (
            DecisionAction.RESUME,
            DecisionAction.FALSE_POSITIVE,
        ):
            incident = self.incidents.get_incident(reference)
            if incident is not None:
                self.confirmer.suppress(
                    incident.kind,
                    incident.bbox,
                    confidence=incident.confidence,
                )
        return result

    def label(
        self, reference: str, label: str, actor: str, note: Optional[str]
    ) -> Tuple[bool, str]:
        if label not in ALLOWED_LABELS:
            return False, "invalid_label"
        try:
            success = self.incidents.label_incident(
                reference, label, actor=actor, note=note
            )
        except ValueError:
            return False, "invalid_label"
        return (True, "ok") if success else (False, "incident_not_found")

    def latest_frame(self) -> Optional[Frame]:
        return self.camera.ring.latest()

    def read_incident_frame(self, reference: str, index: int) -> Optional[bytes]:
        incident = self.incidents.get_incident(reference)
        if incident is None:
            return None
        frames = self.store.list_incident_frames(incident.id)
        if index < 0 or index >= len(frames):
            return None
        try:
            return Path(frames[index]).read_bytes()
        except OSError:
            return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="smart-p1scd", description="Smart P1SC 本地异常监控 daemon"
    )
    parser.add_argument("--config", help="配置文件路径（默认 config.json）")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        secrets = [
            value
            for value in (
                config.printer.access_code(),
                config.http.api_token(),
                config.openclaw.token(),
            )
            if value
        ]
        setup_logging(
            config.logging.level,
            config.logging.json,
            secrets=secrets,
            file_path=config.logging.file_path,
        )
        config.require_printer()
        daemon = Daemon(config)
    except ConfigError as exc:
        print("error: config: {}".format(exc), file=sys.stderr)
        return 2
    except DaemonAlreadyRunning as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 3

    def _shutdown(signum: int, frame: Any) -> None:
        daemon.stop()

    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is not None:
            try:
                signal.signal(signum, _shutdown)
            except (ValueError, OSError):
                pass
    try:
        daemon.run_forever()
    except DaemonAlreadyRunning as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        daemon.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
