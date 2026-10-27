from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    GcodeState,
    Incident,
    IncidentState,
    Severity,
    iso,
    utcnow,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    short_code TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    severity TEXT NOT NULL,
    confidence REAL NOT NULL,
    state TEXT NOT NULL,
    outcome TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    hold_deadline TEXT,
    resolved_at TEXT,
    layer_num INTEGER,
    total_layers INTEGER,
    printer_state TEXT NOT NULL,
    filmstrip_dir TEXT,
    model_version TEXT NOT NULL,
    bbox_json TEXT,
    area_growth REAL,
    evidence_frames INTEGER NOT NULL DEFAULT 0,
    reminder_sent_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_incidents_open
    ON incidents (resolved_at, created_at);
CREATE INDEX IF NOT EXISTS idx_incidents_created
    ON incidents (created_at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (incident_id) REFERENCES incidents (id)
);

CREATE INDEX IF NOT EXISTS idx_events_incident
    ON events (incident_id, at);

CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL,
    label TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (incident_id, label),
    FOREIGN KEY (incident_id) REFERENCES incidents (id)
);

CREATE INDEX IF NOT EXISTS idx_labels_incident
    ON labels (incident_id);

CREATE TABLE IF NOT EXISTS decisions (
    idempotency_key TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL,
    action TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    event_type TEXT NOT NULL,
    incident_id TEXT,
    payload_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_attempt_at TEXT,
    sent_at TEXT,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_notifications_pending
    ON notifications (sent_at, id);
"""


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _row_to_incident(row: sqlite3.Row) -> Incident:
    try:
        severity = Severity(row["severity"])
    except ValueError:
        severity = Severity.P3
    try:
        state = IncidentState(row["state"])
    except ValueError:
        state = IncidentState.ERROR
    try:
        printer_state = GcodeState(row["printer_state"])
    except ValueError:
        printer_state = GcodeState.UNKNOWN

    bbox_raw = row["bbox_json"]
    bbox: Optional[Tuple[float, float, float, float]] = None
    if bbox_raw:
        try:
            values = json.loads(bbox_raw)
            if isinstance(values, list) and len(values) == 4:
                bbox = (float(values[0]), float(values[1]), float(values[2]), float(values[3]))
        except (ValueError, TypeError):
            bbox = None

    try:
        reminder_sent = json.loads(row["reminder_sent_json"] or "[]")
    except ValueError:
        reminder_sent = []
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except ValueError:
        metadata = {}

    return Incident(
        id=row["id"],
        short_code=row["short_code"],
        kind=row["type"],
        severity=severity,
        confidence=float(row["confidence"]),
        state=state,
        created_at=_parse_datetime(row["created_at"]) or utcnow(),
        updated_at=_parse_datetime(row["updated_at"]) or utcnow(),
        hold_deadline=_parse_datetime(row["hold_deadline"]),
        resolved_at=_parse_datetime(row["resolved_at"]),
        outcome=row["outcome"],
        layer_num=row["layer_num"],
        total_layers=row["total_layers"],
        printer_state=printer_state,
        filmstrip_dir=row["filmstrip_dir"],
        model_version=row["model_version"],
        bbox=bbox,
        area_growth=row["area_growth"],
        evidence_frames=int(row["evidence_frames"] or 0),
        reminder_sent=[float(item) for item in reminder_sent],
        metadata=metadata if isinstance(metadata, dict) else {},
    )


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            parent = Path(path).expanduser().parent
            parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self.initialize()

    def initialize(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def upsert_incident(self, incident: Incident) -> None:
        bbox_json = json.dumps(list(incident.bbox)) if incident.bbox is not None else None
        params = (
            incident.id,
            incident.short_code,
            incident.kind,
            incident.severity.value,
            incident.confidence,
            incident.state.value,
            incident.outcome,
            iso(incident.created_at),
            iso(incident.updated_at),
            iso(incident.hold_deadline),
            iso(incident.resolved_at),
            incident.layer_num,
            incident.total_layers,
            incident.printer_state.value,
            incident.filmstrip_dir,
            incident.model_version,
            bbox_json,
            incident.area_growth,
            incident.evidence_frames,
            json.dumps(incident.reminder_sent),
            json.dumps(incident.metadata),
        )
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO incidents (
                    id, short_code, type, severity, confidence, state, outcome,
                    created_at, updated_at, hold_deadline, resolved_at, layer_num,
                    total_layers, printer_state, filmstrip_dir, model_version,
                    bbox_json, area_growth, evidence_frames, reminder_sent_json,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    short_code=excluded.short_code,
                    type=excluded.type,
                    severity=excluded.severity,
                    confidence=excluded.confidence,
                    state=excluded.state,
                    outcome=excluded.outcome,
                    updated_at=excluded.updated_at,
                    hold_deadline=excluded.hold_deadline,
                    resolved_at=excluded.resolved_at,
                    layer_num=excluded.layer_num,
                    total_layers=excluded.total_layers,
                    printer_state=excluded.printer_state,
                    filmstrip_dir=excluded.filmstrip_dir,
                    model_version=excluded.model_version,
                    bbox_json=excluded.bbox_json,
                    area_growth=excluded.area_growth,
                    evidence_frames=excluded.evidence_frames,
                    reminder_sent_json=excluded.reminder_sent_json,
                    metadata_json=excluded.metadata_json
                """,
                params,
            )

    def get_incident(self, incident_id: str) -> Optional[Incident]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
        return _row_to_incident(row) if row is not None else None

    def get_incident_by_short_code(self, short_code: str) -> Optional[Incident]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM incidents WHERE short_code = ?", (short_code.upper(),)
            ).fetchone()
        return _row_to_incident(row) if row is not None else None

    def short_code_exists(self, short_code: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM incidents WHERE short_code = ?", (short_code.upper(),)
            ).fetchone()
        return row is not None

    def list_incidents(
        self,
        limit: int = 50,
        open_only: bool = False,
        state: Optional[IncidentState] = None,
    ) -> List[Incident]:
        clauses: List[str] = []
        params: List[Any] = []
        if open_only:
            clauses.append("resolved_at IS NULL")
        if state is not None:
            clauses.append("state = ?")
            params.append(state.value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM incidents{} ORDER BY created_at DESC LIMIT ?".format(where),
                params,
            ).fetchall()
        return [_row_to_incident(row) for row in rows]

    def list_open_incidents(self) -> List[Incident]:
        return self.list_incidents(limit=100, open_only=True)

    def find_open_by_type(self, kind: str) -> Optional[Incident]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM incidents
                WHERE type = ? AND resolved_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (kind,),
            ).fetchone()
        return _row_to_incident(row) if row is not None else None

    def add_event(
        self,
        incident_id: Optional[str],
        actor: str,
        action: str,
        details: Optional[Dict[str, Any]] = None,
        at: Optional[datetime] = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events (incident_id, at, actor, action, details_json) VALUES (?, ?, ?, ?, ?)",
                (incident_id, iso(at or utcnow()), actor, action, json.dumps(details or {})),
            )

    def list_events(self, incident_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE incident_id = ? ORDER BY id ASC", (incident_id,)
            ).fetchall()
        events: List[Dict[str, Any]] = []
        for row in rows:
            try:
                details = json.loads(row["details_json"] or "{}")
            except ValueError:
                details = {}
            events.append(
                {
                    "id": row["id"],
                    "incident_id": row["incident_id"],
                    "at": row["at"],
                    "actor": row["actor"],
                    "action": row["action"],
                    "details": details,
                }
            )
        return events

    def add_label(self, incident_id: str, label: str, note: Optional[str] = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO labels (incident_id, label, note, created_at) VALUES (?, ?, ?, ?)",
                (incident_id, label, note, iso(utcnow())),
            )

    def list_labels(self, incident_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM labels WHERE incident_id = ? ORDER BY id ASC", (incident_id,)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "incident_id": row["incident_id"],
                "label": row["label"],
                "note": row["note"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_decision(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM decisions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            result = json.loads(row["result_json"])
        except ValueError:
            return None
        return {
            "idempotency_key": row["idempotency_key"],
            "incident_id": row["incident_id"],
            "action": row["action"],
            "result": result,
            "created_at": row["created_at"],
        }

    def save_decision(
        self,
        idempotency_key: str,
        incident_id: str,
        action: str,
        result: Dict[str, Any],
        at: Optional[datetime] = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO decisions
                    (idempotency_key, incident_id, action, result_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    incident_id,
                    action,
                    json.dumps(result, ensure_ascii=False),
                    iso(at or utcnow()),
                ),
            )

    def enqueue_notification(
        self,
        idempotency_key: str,
        kind: str,
        event_type: str,
        incident_id: Optional[str],
        payload: Dict[str, Any],
        at: Optional[datetime] = None,
    ) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO notifications
                    (idempotency_key, kind, event_type, incident_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    kind,
                    event_type,
                    incident_id,
                    json.dumps(payload, ensure_ascii=False),
                    iso(at or utcnow()),
                ),
            )
        return cursor.rowcount > 0

    def list_pending_notifications(
        self, limit: int = 50, min_age_seconds: float = 0.0
    ) -> List[Dict[str, Any]]:
        cutoff = iso(utcnow() - timedelta(seconds=max(0.0, min_age_seconds)))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM notifications
                WHERE sent_at IS NULL
                  AND (last_attempt_at IS NULL OR last_attempt_at <= ?)
                ORDER BY id ASC LIMIT ?
                """,
                (cutoff, max(1, int(limit))),
            ).fetchall()
        pending: List[Dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except ValueError:
                payload = {}
            pending.append(
                {
                    "id": row["id"],
                    "idempotency_key": row["idempotency_key"],
                    "kind": row["kind"],
                    "event_type": row["event_type"],
                    "incident_id": row["incident_id"],
                    "payload": payload,
                    "attempts": row["attempts"],
                    "created_at": row["created_at"],
                    "last_attempt_at": row["last_attempt_at"],
                    "last_error": row["last_error"],
                }
            )
        return pending

    def mark_notification_sent(
        self, idempotency_key: str, at: Optional[datetime] = None
    ) -> None:
        now = iso(at or utcnow())
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE notifications
                SET sent_at = ?, last_attempt_at = ?, attempts = attempts + 1,
                    last_error = NULL
                WHERE idempotency_key = ?
                """,
                (now, now, idempotency_key),
            )

    def mark_notification_failed(
        self,
        idempotency_key: str,
        error: str,
        at: Optional[datetime] = None,
    ) -> None:
        now = iso(at or utcnow())
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE notifications
                SET last_attempt_at = ?, attempts = attempts + 1, last_error = ?
                WHERE idempotency_key = ? AND sent_at IS NULL
                """,
                (now, error[:500], idempotency_key),
            )

    def purge_resolved_incidents_before(
        self, cutoff: datetime
    ) -> List[Tuple[str, Optional[str]]]:
        cutoff_text = iso(cutoff)
        with self._lock, self._conn:
            rows = self._conn.execute(
                """
                SELECT id, filmstrip_dir FROM incidents
                WHERE resolved_at IS NOT NULL AND resolved_at < ?
                """,
                (cutoff_text,),
            ).fetchall()
            removed = [(row["id"], row["filmstrip_dir"]) for row in rows]
            for incident_id, _ in removed:
                self._conn.execute(
                    "DELETE FROM labels WHERE incident_id = ?", (incident_id,)
                )
                self._conn.execute(
                    "DELETE FROM events WHERE incident_id = ?", (incident_id,)
                )
                self._conn.execute(
                    "DELETE FROM decisions WHERE incident_id = ?", (incident_id,)
                )
                self._conn.execute(
                    "DELETE FROM notifications WHERE incident_id = ?", (incident_id,)
                )
                self._conn.execute(
                    "DELETE FROM incidents WHERE id = ?", (incident_id,)
                )
        return removed

    def count_unsent_notifications(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE sent_at IS NULL"
            ).fetchone()
        return int(row["n"]) if row is not None else 0

    def count_open_incidents(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM incidents WHERE resolved_at IS NULL"
            ).fetchone()
        return int(row["n"]) if row is not None else 0
