import sqlite3
from datetime import timedelta

import pytest

from smart_p1_sc.db import Database
from smart_p1_sc.models import (
    GcodeState,
    Incident,
    IncidentOutcome,
    IncidentState,
    Severity,
    utcnow,
)


def make_incident(**overrides):
    payload = {
        "id": "inc-1",
        "short_code": "7F3A",
        "kind": "spaghetti",
        "severity": Severity.P1,
        "confidence": 0.91,
        "state": IncidentState.HOLDING,
        "hold_deadline": utcnow() + timedelta(minutes=60),
        "layer_num": 4,
        "total_layers": 20,
        "printer_state": GcodeState.PAUSE,
        "filmstrip_dir": "data/media/inc-1",
        "model_version": "stub-v0",
        "bbox": (1.0, 2.0, 30.0, 40.0),
        "area_growth": 0.2,
        "evidence_frames": 4,
        "reminder_sent": [10.0],
        "metadata": {"source": "test"},
    }
    payload.update(overrides)
    return Incident(**payload)


@pytest.fixture()
def db():
    database = Database(":memory:")
    yield database
    database.close()


def test_roundtrip_incident(db):
    incident = make_incident()
    db.upsert_incident(incident)
    loaded = db.get_incident("inc-1")
    assert loaded is not None
    assert loaded.short_code == "7F3A"
    assert loaded.kind == "spaghetti"
    assert loaded.severity == Severity.P1
    assert loaded.state == IncidentState.HOLDING
    assert loaded.printer_state == GcodeState.PAUSE
    assert loaded.bbox == (1.0, 2.0, 30.0, 40.0)
    assert loaded.reminder_sent == [10.0]
    assert loaded.metadata == {"source": "test"}
    assert loaded.is_open is True


def test_update_incident(db):
    incident = make_incident()
    db.upsert_incident(incident)
    incident.state = IncidentState.STOPPED
    incident.outcome = IncidentOutcome.USER_STOPPED.value
    incident.resolved_at = utcnow()
    db.upsert_incident(incident)
    loaded = db.get_incident("inc-1")
    assert loaded.state == IncidentState.STOPPED
    assert loaded.outcome == "user_stopped"
    assert loaded.is_open is False
    assert db.count_open_incidents() == 0


def test_short_code_is_unique(db):
    db.upsert_incident(make_incident())
    with pytest.raises(sqlite3.IntegrityError):
        db.upsert_incident(make_incident(id="inc-2"))


def test_lookup_by_short_code_is_case_insensitive(db):
    db.upsert_incident(make_incident())
    assert db.get_incident_by_short_code("7f3a").id == "inc-1"
    assert db.short_code_exists("7F3A") is True
    assert db.short_code_exists("0000") is False


def test_list_and_find_open(db):
    db.upsert_incident(make_incident())
    db.upsert_incident(
        make_incident(
            id="inc-2",
            short_code="AAAA",
            kind="first_layer",
            state=IncidentState.STOPPED,
            resolved_at=utcnow(),
        )
    )
    assert db.count_open_incidents() == 1
    assert [item.id for item in db.list_open_incidents()] == ["inc-1"]
    assert db.find_open_by_type("spaghetti").id == "inc-1"
    assert db.find_open_by_type("first_layer") is None
    assert len(db.list_incidents(limit=10)) == 2


def test_events_are_ordered(db):
    db.upsert_incident(make_incident())
    db.add_event("inc-1", "daemon", "paused", {"state": "PAUSE"})
    db.add_event("inc-1", "feishu", "resume_requested", {"short_code": "7F3A"})
    events = db.list_events("inc-1")
    assert [event["action"] for event in events] == ["paused", "resume_requested"]
    assert events[0]["details"] == {"state": "PAUSE"}


def test_labels_are_idempotent(db):
    db.upsert_incident(make_incident())
    db.add_label("inc-1", "true_positive", "looks like spaghetti")
    db.add_label("inc-1", "true_positive", "duplicate")
    labels = db.list_labels("inc-1")
    assert len(labels) == 1
    assert labels[0]["label"] == "true_positive"


def test_purge_resolved_incidents_protects_open(db):
    old = utcnow() - timedelta(days=40)
    db.upsert_incident(make_incident(id="open-1", short_code="AAAA"))
    db.upsert_incident(
        make_incident(
            id="old-1",
            short_code="BBBB",
            state=IncidentState.STOPPED,
            resolved_at=old,
        )
    )
    db.upsert_incident(
        make_incident(
            id="new-1",
            short_code="CCCC",
            state=IncidentState.STOPPED,
            resolved_at=utcnow(),
        )
    )
    db.add_event("old-1", "daemon", "stopped", {})
    db.add_label("old-1", "true_positive")
    db.enqueue_notification("notify-old-1", "incident", "resolved", "old-1", {})

    removed = db.purge_resolved_incidents_before(utcnow() - timedelta(days=30))
    assert [incident_id for incident_id, _ in removed] == ["old-1"]
    assert db.get_incident("old-1") is None
    assert db.get_incident("open-1") is not None
    assert db.get_incident("new-1") is not None
    assert db.list_events("old-1") == []
    assert db.list_labels("old-1") == []
    assert db.list_pending_notifications(min_age_seconds=0) == []


def test_notification_outbox(db):
    assert db.enqueue_notification(
        "hold:inc-1", "incident", "holding", "inc-1", {"incident": {"id": "inc-1"}}
    )
    assert not db.enqueue_notification(
        "hold:inc-1", "incident", "holding", "inc-1", {"incident": {"id": "inc-1"}}
    )
    assert db.count_unsent_notifications() == 1
    pending = db.list_pending_notifications(min_age_seconds=0)
    assert pending[0]["idempotency_key"] == "hold:inc-1"
    assert pending[0]["payload"]["incident"]["id"] == "inc-1"

    db.mark_notification_failed("hold:inc-1", "openclaw down")
    pending = db.list_pending_notifications(min_age_seconds=0)
    assert pending[0]["attempts"] == 1
    assert pending[0]["last_error"] == "openclaw down"

    db.mark_notification_sent("hold:inc-1")
    assert db.count_unsent_notifications() == 0
    assert db.list_pending_notifications(min_age_seconds=0) == []


def test_incident_survives_reopen(tmp_path):
    path = tmp_path / "nested" / "data.sqlite3"
    database = Database(str(path))
    database.upsert_incident(make_incident())
    database.close()

    reopened = Database(str(path))
    assert reopened.get_incident("inc-1") is not None
    reopened.close()
