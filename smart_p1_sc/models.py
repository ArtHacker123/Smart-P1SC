from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def deep_merge(base: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(base)
    for key, value in update.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class GcodeState(str, Enum):
    IDLE = "IDLE"
    PREPARE = "PREPARE"
    RUNNING = "RUNNING"
    PAUSE = "PAUSE"
    FINISH = "FINISH"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


_GCODE_ALIASES = {
    "IDLE": GcodeState.IDLE,
    "PREPARE": GcodeState.PREPARE,
    "RUNNING": GcodeState.RUNNING,
    "PAUSE": GcodeState.PAUSE,
    "PAUSED": GcodeState.PAUSE,
    "FINISH": GcodeState.FINISH,
    "FAILED": GcodeState.FAILED,
    "FAIL": GcodeState.FAILED,
}


def parse_gcode_state(value: Any) -> GcodeState:
    if value is None:
        return GcodeState.UNKNOWN
    return _GCODE_ALIASES.get(str(value).strip().upper(), GcodeState.UNKNOWN)


class Severity(str, Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


SEVERITY_ORDER = {Severity.P1: 1, Severity.P2: 2, Severity.P3: 3}


class IncidentState(str, Enum):
    MONITORING = "MONITORING"
    PAUSING = "PAUSING"
    HOLDING = "HOLDING"
    RESUMING = "RESUMING"
    STOPPING = "STOPPING"
    AUTO_STOPPING = "AUTO_STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"
    DEGRADED = "DEGRADED"


class IncidentOutcome(str, Enum):
    RESUMED = "resumed"
    USER_STOPPED = "user_stopped"
    AUTO_STOPPED = "auto_stopped"
    FALSE_POSITIVE = "false_positive"
    CONTROL_FAILED = "control_failed"
    PAUSE_FAILED_STOPPED = "pause_failed_stopped"


class DecisionAction(str, Enum):
    RESUME = "resume"
    STOP = "stop"
    FALSE_POSITIVE = "false_positive"


@dataclass
class Frame:
    seq: int
    monotonic_ts: float
    wall_ts: datetime
    jpeg: bytes
    width: Optional[int] = None
    height: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "monotonic_ts": self.monotonic_ts,
            "wall_ts": iso(self.wall_ts),
            "bytes": len(self.jpeg),
            "width": self.width,
            "height": self.height,
        }


@dataclass
class PrinterSnapshot:
    raw: Dict[str, Any] = field(default_factory=dict)
    gcode_state: GcodeState = GcodeState.UNKNOWN
    layer_num: Optional[int] = None
    total_layer_num: Optional[int] = None
    nozzle_temp: Optional[float] = None
    bed_temp: Optional[float] = None
    print_error: Any = None
    hms: Any = field(default_factory=list)
    ipcam: Dict[str, Any] = field(default_factory=dict)
    ams: Dict[str, Any] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=utcnow)

    @classmethod
    def from_report(
        cls, payload: Dict[str, Any], previous: Optional["PrinterSnapshot"] = None
    ) -> "PrinterSnapshot":
        base = previous.raw if previous is not None else {}
        raw = deep_merge(base, payload or {})

        print_obj: Dict[str, Any] = {}
        for key in ("pushing", "print"):
            section = raw.get(key)
            if isinstance(section, dict):
                print_obj = deep_merge(print_obj, section)

        ipcam = print_obj.get("ipcam")
        ams = print_obj.get("ams")
        hms = print_obj.get("hms")
        return cls(
            raw=raw,
            gcode_state=parse_gcode_state(print_obj.get("gcode_state")),
            layer_num=_optional_int(print_obj.get("layer_num")),
            total_layer_num=_optional_int(print_obj.get("total_layer_num")),
            nozzle_temp=_optional_float(print_obj.get("nozzle_temper")),
            bed_temp=_optional_float(print_obj.get("bed_temper")),
            print_error=print_obj.get("print_error", print_obj.get("mc_print_error_code")),
            hms=hms if hms is not None else [],
            ipcam=ipcam if isinstance(ipcam, dict) else {},
            ams=ams if isinstance(ams, dict) else {},
            updated_at=utcnow(),
        )

    @property
    def is_running(self) -> bool:
        return self.gcode_state in (GcodeState.PREPARE, GcodeState.RUNNING)

    @property
    def is_paused(self) -> bool:
        return self.gcode_state == GcodeState.PAUSE

    @property
    def is_terminal(self) -> bool:
        return self.gcode_state in (GcodeState.IDLE, GcodeState.FINISH, GcodeState.FAILED)

    def to_dict(self, include_raw: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "gcode_state": self.gcode_state.value,
            "layer_num": self.layer_num,
            "total_layer_num": self.total_layer_num,
            "nozzle_temp": self.nozzle_temp,
            "bed_temp": self.bed_temp,
            "print_error": self.print_error,
            "hms": self.hms,
            "updated_at": iso(self.updated_at),
        }
        if include_raw:
            payload["raw"] = self.raw
        return payload


@dataclass
class Candidate:
    kind: str
    confidence: float
    bbox: Optional[Tuple[float, float, float, float]]
    frame_seq: int
    model_version: str
    mask: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.kind,
            "confidence": round(self.confidence, 4),
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "frame_seq": self.frame_seq,
            "model_version": self.model_version,
        }


@dataclass
class Detection:
    kind: str
    severity: Severity
    confidence: float
    bbox: Optional[Tuple[float, float, float, float]]
    frame_seq: int
    evidence_frames: int
    area_growth: Optional[float]
    model_version: str
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.kind,
            "severity": self.severity.value,
            "confidence": round(self.confidence, 4),
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "frame_seq": self.frame_seq,
            "evidence_frames": self.evidence_frames,
            "area_growth": self.area_growth,
            "model_version": self.model_version,
            "created_at": iso(self.created_at),
        }


@dataclass
class Incident:
    id: str
    short_code: str
    kind: str
    severity: Severity
    confidence: float
    state: IncidentState
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    hold_deadline: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    outcome: Optional[str] = None
    layer_num: Optional[int] = None
    total_layers: Optional[int] = None
    printer_state: GcodeState = GcodeState.UNKNOWN
    filmstrip_dir: Optional[str] = None
    model_version: str = ""
    bbox: Optional[Tuple[float, float, float, float]] = None
    area_growth: Optional[float] = None
    evidence_frames: int = 0
    reminder_sent: List[float] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Incident":
        def parse(value: Any) -> Optional[datetime]:
            if not value:
                return None
            try:
                return datetime.fromisoformat(str(value))
            except ValueError:
                return None

        try:
            severity = Severity(str(data.get("severity", Severity.P3.value)))
        except ValueError:
            severity = Severity.P3
        try:
            state = IncidentState(str(data.get("state", IncidentState.ERROR.value)))
        except ValueError:
            state = IncidentState.ERROR
        try:
            printer_state = GcodeState(
                str(data.get("printer_state", GcodeState.UNKNOWN.value))
            )
        except ValueError:
            printer_state = GcodeState.UNKNOWN
        bbox_raw = data.get("bbox")
        bbox = None
        if isinstance(bbox_raw, (list, tuple)) and len(bbox_raw) == 4:
            try:
                bbox = tuple(float(value) for value in bbox_raw)  # type: ignore[assignment]
            except (TypeError, ValueError):
                bbox = None
        created_at = parse(data.get("created_at")) or utcnow()
        return cls(
            id=str(data.get("id", "")),
            short_code=str(data.get("short_code", "")),
            kind=str(data.get("type", data.get("kind", "unknown"))),
            severity=severity,
            confidence=float(data.get("confidence", 0.0)),
            state=state,
            created_at=created_at,
            updated_at=parse(data.get("updated_at")) or created_at,
            hold_deadline=parse(data.get("hold_deadline")),
            resolved_at=parse(data.get("resolved_at")),
            outcome=data.get("outcome"),
            layer_num=data.get("layer_num"),
            total_layers=data.get("total_layers"),
            printer_state=printer_state,
            filmstrip_dir=data.get("filmstrip_dir"),
            model_version=str(data.get("model_version", "")),
            bbox=bbox,
            area_growth=data.get("area_growth"),
            evidence_frames=int(data.get("evidence_frames", 0)),
            reminder_sent=list(data.get("reminder_sent") or []),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "short_code": self.short_code,
            "type": self.kind,
            "severity": self.severity.value,
            "confidence": round(self.confidence, 4),
            "state": self.state.value,
            "outcome": self.outcome,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "hold_deadline": iso(self.hold_deadline),
            "resolved_at": iso(self.resolved_at),
            "layer_num": self.layer_num,
            "total_layers": self.total_layers,
            "printer_state": self.printer_state.value,
            "filmstrip_dir": self.filmstrip_dir,
            "model_version": self.model_version,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "area_growth": self.area_growth,
            "evidence_frames": self.evidence_frames,
            "reminder_sent": list(self.reminder_sent),
            "metadata": self.metadata,
        }


@dataclass
class CommandResult:
    command: str
    success: bool
    sequence_id: Optional[str] = None
    ack_result: Optional[str] = None
    state_before: GcodeState = GcodeState.UNKNOWN
    state_after: GcodeState = GcodeState.UNKNOWN
    error: Optional[str] = None
    attempts: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "success": self.success,
            "sequence_id": self.sequence_id,
            "ack_result": self.ack_result,
            "state_before": self.state_before.value,
            "state_after": self.state_after.value,
            "error": self.error,
            "attempts": self.attempts,
        }


@dataclass
class DecisionResult:
    success: bool
    code: str
    message: str
    incident_id: str
    state: Optional[IncidentState] = None
    action: Optional[DecisionAction] = None
    outcome: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "code": self.code,
            "message": self.message,
            "incident_id": self.incident_id,
            "state": self.state.value if self.state is not None else None,
            "action": self.action.value if self.action is not None else None,
            "outcome": self.outcome,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DecisionResult":
        state = None
        if data.get("state"):
            try:
                state = IncidentState(str(data["state"]))
            except ValueError:
                state = None
        action = None
        if data.get("action"):
            try:
                action = DecisionAction(str(data["action"]))
            except ValueError:
                action = None
        return cls(
            success=bool(data.get("success")),
            code=str(data.get("code", "unknown")),
            message=str(data.get("message", "")),
            incident_id=str(data.get("incident_id", "")),
            state=state,
            action=action,
            outcome=data.get("outcome"),
        )
