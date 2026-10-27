from __future__ import annotations

import logging
import secrets
import threading
import uuid
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional

from .config import HoldConfig
from .db import Database
from .models import (
    DecisionAction,
    DecisionResult,
    Detection,
    GcodeState,
    Incident,
    IncidentOutcome,
    IncidentState,
    PrinterSnapshot,
    Severity,
    utcnow,
)
from .mqtt import PrinterCommandExecutor

ALLOWED_LABELS = {"true_positive", "false_positive", "unknown", "structural_anomaly"}


class IncidentManager:
    def __init__(
        self,
        db: Database,
        executor: PrinterCommandExecutor,
        hold_config: HoldConfig,
        now: Optional[Callable[[], datetime]] = None,
        on_holding: Optional[Callable[[Incident], None]] = None,
        on_observation: Optional[Callable[[Incident], None]] = None,
        on_reminder: Optional[Callable[[Incident, float], None]] = None,
        on_resolved: Optional[Callable[[Incident], None]] = None,
        on_error: Optional[Callable[[Incident, str], None]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.db = db
        self.executor = executor
        self.hold_config = hold_config
        self._now = now or utcnow
        self._lock = threading.RLock()
        self.logger = logger or logging.getLogger(__name__)

        self.on_holding = on_holding
        self.on_observation = on_observation
        self.on_reminder = on_reminder
        self.on_resolved = on_resolved
        self.on_error = on_error

    def handle_detection(
        self,
        detection: Detection,
        snapshot: Optional[PrinterSnapshot],
        filmstrip_dir: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Optional[Incident]:
        now = now or self._now()
        if detection.kind == "structural_anomaly":
            self.db.add_event(
                None,
                "detector",
                "structural_anomaly_candidate",
                detection.to_dict(),
                at=now,
            )
            return None

        pause_incident: Optional[Incident] = None
        with self._lock:
            existing = self.db.find_open_by_type(detection.kind)
            if existing is not None:
                existing.confidence = max(existing.confidence, detection.confidence)
                existing.evidence_frames = max(
                    existing.evidence_frames, detection.evidence_frames
                )
                existing.area_growth = detection.area_growth
                existing.bbox = detection.bbox
                if snapshot is not None:
                    existing.layer_num = snapshot.layer_num
                    existing.total_layers = snapshot.total_layer_num
                if (
                    detection.severity == Severity.P1
                    and existing.severity != Severity.P1
                    and existing.state == IncidentState.MONITORING
                ):
                    existing.severity = Severity.P1
                    existing.state = IncidentState.PAUSING
                    pause_incident = existing
                    event_action = "severity_upgraded"
                else:
                    event_action = "detection_matched"
                self._save(existing, now, event_action, detection.to_dict())
                if pause_incident is None:
                    return existing
                incident = existing
            else:
                if detection.severity == Severity.P3:
                    self.db.add_event(
                        None,
                        "detector",
                        "p3_detection",
                        detection.to_dict(),
                        at=now,
                    )
                    return None
                incident = self._new_incident(detection, snapshot, filmstrip_dir, now)
                if detection.severity == Severity.P2:
                    incident.state = IncidentState.MONITORING
                    self._save(incident, now, "observed", details=detection.to_dict())
                    self._notify(self.on_observation, incident)
                    return incident
                incident.state = IncidentState.PAUSING
                self._save(incident, now, "detected", details=detection.to_dict())
                pause_incident = incident

        if pause_incident is not None:
            self._pause_and_hold(pause_incident, snapshot, now)
        return incident

    def _new_incident(
        self,
        detection: Detection,
        snapshot: Optional[PrinterSnapshot],
        filmstrip_dir: Optional[str],
        now: datetime,
    ) -> Incident:
        return Incident(
            id=uuid.uuid4().hex,
            short_code=self._new_short_code(),
            kind=detection.kind,
            severity=detection.severity,
            confidence=detection.confidence,
            state=IncidentState.PAUSING,
            created_at=now,
            updated_at=now,
            layer_num=snapshot.layer_num if snapshot is not None else None,
            total_layers=snapshot.total_layer_num if snapshot is not None else None,
            printer_state=snapshot.gcode_state if snapshot is not None else GcodeState.UNKNOWN,
            filmstrip_dir=filmstrip_dir,
            model_version=detection.model_version,
            bbox=detection.bbox,
            area_growth=detection.area_growth,
            evidence_frames=detection.evidence_frames,
            metadata={"detection": detection.to_dict()},
        )

    def _new_short_code(self) -> str:
        alphabet = "0123456789ABCDEF"
        for _ in range(50):
            code = "".join(secrets.choice(alphabet) for _ in range(4))
            if not self.db.short_code_exists(code):
                return code
        raise RuntimeError("无法生成唯一事件短码")

    def _pause_and_hold(
        self,
        incident: Incident,
        snapshot: Optional[PrinterSnapshot],
        now: datetime,
    ) -> None:
        result = self.executor.pause()
        with self._lock:
            incident.printer_state = result.state_after
            if not result.success:
                self._handle_pause_failure(incident, result.error or "pause failed", now)
                return
            incident.state = IncidentState.HOLDING
            incident.hold_deadline = now + timedelta(minutes=self.hold_config.max_wait_minutes)
            self._save(incident, now, "paused", details=result.to_dict())

        hold = getattr(self.executor, "hold", None)
        if callable(hold):
            bed_temp = snapshot.bed_temp if snapshot is not None else None
            if not self.hold_config.keep_bed_temperature:
                bed_temp = None
            try:
                hold_result = hold(self.hold_config.nozzle_temperature_c, bed_temp)
            except Exception as exc:
                self.logger.exception("hold_command_failed")
                hold_result = None
                hold_error = "{}: {}".format(type(exc).__name__, exc)
            else:
                hold_error = None if hold_result.success else (hold_result.error or "hold failed")
            if hold_error:
                with self._lock:
                    self.db.add_event(
                        incident.id,
                        "daemon",
                        "hold_failed",
                        {"error": hold_error},
                        at=now,
                    )
                self.logger.warning(
                    "hold_failed", extra={"incident_id": incident.id, "error": hold_error}
                )

        self._notify(self.on_holding, incident)

    def _handle_pause_failure(self, incident: Incident, error: str, now: datetime) -> None:
        self.db.add_event(incident.id, "daemon", "pause_failed", {"error": error}, at=now)
        if not self.hold_config.fail_safe_stop_on_pause_failure:
            incident.state = IncidentState.ERROR
            incident.outcome = IncidentOutcome.CONTROL_FAILED.value
            self._save(incident, now)
            self._notify(
                self.on_error,
                incident,
                "暂停失败，自动停止已禁用，需要人工处理: {}".format(error),
            )
            return

        incident.state = IncidentState.AUTO_STOPPING
        self._save(incident, now, "fail_safe_stop_started", details={"pause_error": error})
        stop_result = self.executor.stop()
        incident.printer_state = stop_result.state_after
        if stop_result.success:
            incident.state = IncidentState.STOPPED
            incident.outcome = IncidentOutcome.PAUSE_FAILED_STOPPED.value
            incident.resolved_at = now
            self._save(incident, now, "fail_safe_stopped", details=stop_result.to_dict())
            self._notify(self.on_resolved, incident)
        else:
            incident.state = IncidentState.ERROR
            incident.outcome = IncidentOutcome.CONTROL_FAILED.value
            self._save(incident, now, "fail_safe_stop_failed", details=stop_result.to_dict())
            self._notify(
                self.on_error,
                incident,
                "暂停失败且停止失败，需要立即人工处理: {}".format(
                    stop_result.error or "unknown error"
                ),
            )

    def tick(self, now: Optional[datetime] = None) -> None:
        now = now or self._now()
        for incident in self.db.list_open_incidents():
            if incident.state == IncidentState.HOLDING:
                self._tick_holding(incident, now)

    def _tick_holding(self, incident: Incident, now: datetime) -> None:
        with self._lock:
            incident = self.db.get_incident(incident.id)
            if incident is None or incident.state != IncidentState.HOLDING:
                return
            if incident.resolved_at is not None:
                return
            if incident.hold_deadline is None:
                return
            if now >= incident.hold_deadline:
                self._auto_stop(incident, now)
                return

            hold_started = incident.hold_deadline - timedelta(
                minutes=self.hold_config.max_wait_minutes
            )
            elapsed_minutes = (now - hold_started).total_seconds() / 60.0
            sent = set(incident.reminder_sent)
            due = sorted(
                reminder
                for reminder in self.hold_config.reminder_minutes
                if reminder < self.hold_config.max_wait_minutes
                and elapsed_minutes >= reminder
                and reminder not in sent
            )
            if not due:
                return
            highest = due[-1]
            sent.update(due)
            incident.reminder_sent = sorted(sent)
            self._save(
                incident,
                now,
                "reminder",
                details={"minutes": highest, "due": due},
            )
            self._notify(self.on_reminder, incident, highest)

    def _auto_stop(self, incident: Incident, now: datetime) -> None:
        incident.state = IncidentState.AUTO_STOPPING
        self._save(incident, now, "auto_stop_started")
        result = self.executor.stop()
        incident.printer_state = result.state_after
        if result.success:
            incident.state = IncidentState.STOPPED
            incident.outcome = IncidentOutcome.AUTO_STOPPED.value
            incident.resolved_at = now
            self._save(incident, now, "auto_stopped", details=result.to_dict())
            self._notify(self.on_resolved, incident)
        else:
            incident.state = IncidentState.ERROR
            incident.outcome = IncidentOutcome.CONTROL_FAILED.value
            self._save(incident, now, "auto_stop_failed", details=result.to_dict())
            self._notify(
                self.on_error,
                incident,
                "到达 {} 分钟仍未收到回复，自动停止失败，需要立即人工处理: {}".format(
                    int(self.hold_config.max_wait_minutes),
                    result.error or "unknown error",
                ),
            )

    def decide(
        self,
        reference: str,
        action: DecisionAction,
        actor: str = "user",
        note: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> DecisionResult:
        now = now or self._now()
        with self._lock:
            incident = self._resolve_ref(reference)
            if incident is None:
                return DecisionResult(
                    success=False,
                    code="incident_not_found",
                    message="找不到事件 {}".format(reference),
                    incident_id=reference,
                    action=action,
                )
            if incident.resolved_at is not None:
                return DecisionResult(
                    success=False,
                    code="already_resolved",
                    message="事件 {} 已结束（{}），不能继续操作".format(
                        incident.short_code, incident.outcome or incident.state.value
                    ),
                    incident_id=incident.id,
                    state=incident.state,
                    action=action,
                    outcome=incident.outcome,
                )

            if action == DecisionAction.STOP:
                if incident.state not in (
                    IncidentState.HOLDING,
                    IncidentState.ERROR,
                    IncidentState.PAUSING,
                ):
                    return self._invalid_state(incident, action)
                return self._stop(incident, actor, note, now)

            if incident.state != IncidentState.HOLDING:
                return self._invalid_state(incident, action)

            if action == DecisionAction.RESUME:
                return self._resume(
                    incident,
                    actor,
                    note,
                    now,
                    outcome=IncidentOutcome.RESUMED,
                    requested_action="resume_requested",
                    resolved_action="resumed",
                )
            if action == DecisionAction.FALSE_POSITIVE:
                self.db.add_label(incident.id, "false_positive", note)
                self.db.add_event(
                    incident.id,
                    actor,
                    "labeled",
                    {"label": "false_positive", "note": note},
                    at=now,
                )
                return self._resume(
                    incident,
                    actor,
                    note,
                    now,
                    outcome=IncidentOutcome.FALSE_POSITIVE,
                    requested_action="false_positive_requested",
                    resolved_action="false_positive",
                )
            return self._invalid_state(incident, action)

    def _resume(
        self,
        incident: Incident,
        actor: str,
        note: Optional[str],
        now: datetime,
        outcome: IncidentOutcome,
        requested_action: str,
        resolved_action: str,
    ) -> DecisionResult:
        incident.state = IncidentState.RESUMING
        self._save(
            incident,
            now,
            requested_action,
            details={"actor": actor, "note": note},
        )
        result = self.executor.resume()
        incident.printer_state = result.state_after
        if result.success:
            incident.state = IncidentState.MONITORING
            incident.outcome = outcome.value
            incident.resolved_at = now
            self._save(incident, now, resolved_action, details=result.to_dict())
            self._notify(self.on_resolved, incident)
            return DecisionResult(
                success=True,
                code="ok",
                message="已继续打印",
                incident_id=incident.id,
                state=incident.state,
                action=(
                    DecisionAction.FALSE_POSITIVE
                    if outcome == IncidentOutcome.FALSE_POSITIVE
                    else DecisionAction.RESUME
                ),
                outcome=incident.outcome,
            )

        incident.state = IncidentState.HOLDING
        self._save(incident, now, "resume_failed", details=result.to_dict())
        return DecisionResult(
            success=False,
            code="command_failed",
            message="继续打印失败: {}".format(result.error or "unknown error"),
            incident_id=incident.id,
            state=incident.state,
            action=DecisionAction.RESUME,
            outcome=incident.outcome,
        )

    def _stop(
        self, incident: Incident, actor: str, note: Optional[str], now: datetime
    ) -> DecisionResult:
        incident.state = IncidentState.STOPPING
        self._save(
            incident,
            now,
            "stop_requested",
            details={"actor": actor, "note": note},
        )
        result = self.executor.stop()
        incident.printer_state = result.state_after
        if result.success:
            incident.state = IncidentState.STOPPED
            incident.outcome = IncidentOutcome.USER_STOPPED.value
            incident.resolved_at = now
            self._save(incident, now, "stopped", details=result.to_dict())
            self._notify(self.on_resolved, incident)
            return DecisionResult(
                success=True,
                code="ok",
                message="已停止打印",
                incident_id=incident.id,
                state=incident.state,
                action=DecisionAction.STOP,
                outcome=incident.outcome,
            )

        incident.state = IncidentState.ERROR
        incident.outcome = IncidentOutcome.CONTROL_FAILED.value
        self._save(incident, now, "stop_failed", details=result.to_dict())
        self._notify(
            self.on_error,
            incident,
            "停止打印失败，需要立即人工处理: {}".format(result.error or "unknown error"),
        )
        return DecisionResult(
            success=False,
            code="command_failed",
            message="停止打印失败: {}".format(result.error or "unknown error"),
            incident_id=incident.id,
            state=incident.state,
            action=DecisionAction.STOP,
            outcome=incident.outcome,
        )

    def _invalid_state(self, incident: Incident, action: DecisionAction) -> DecisionResult:
        return DecisionResult(
            success=False,
            code="invalid_state",
            message="事件 {} 当前状态为 {}，不能执行 {}".format(
                incident.short_code, incident.state.value, action.value
            ),
            incident_id=incident.id,
            state=incident.state,
            action=action,
            outcome=incident.outcome,
        )

    def label_incident(
        self,
        reference: str,
        label: str,
        actor: str = "user",
        note: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        if label not in ALLOWED_LABELS:
            raise ValueError("不支持的标签: {}".format(label))
        now = now or self._now()
        with self._lock:
            incident = self._resolve_ref(reference)
            if incident is None:
                return False
            self.db.add_label(incident.id, label, note)
            self.db.add_event(
                incident.id,
                actor,
                "labeled",
                {"label": label, "note": note},
                at=now,
            )
            return True

    def get_incident(self, reference: str) -> Optional[Incident]:
        return self._resolve_ref(reference)

    def _resolve_ref(self, reference: str) -> Optional[Incident]:
        incident = self.db.get_incident(reference)
        if incident is not None:
            return incident
        return self.db.get_incident_by_short_code(reference)

    def _save(
        self,
        incident: Incident,
        now: datetime,
        action: Optional[str] = None,
        details: Optional[Dict[str, object]] = None,
        actor: str = "daemon",
    ) -> None:
        incident.updated_at = now
        self.db.upsert_incident(incident)
        if action is not None:
            self.db.add_event(incident.id, actor, action, details, at=now)

    def _notify(self, callback: Optional[Callable[..., None]], *args: object) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            self.logger.exception("incident_callback_failed")
