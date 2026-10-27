from datetime import datetime, timedelta, timezone

import pytest

from smart_p1_sc.config import HoldConfig
from smart_p1_sc.db import Database
from smart_p1_sc.incidents import IncidentManager
from smart_p1_sc.models import (
    CommandResult,
    DecisionAction,
    Detection,
    GcodeState,
    IncidentOutcome,
    IncidentState,
    PrinterSnapshot,
    Severity,
)


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def command(name, success, before, after, error=None):
    return CommandResult(
        command=name,
        success=success,
        state_before=before,
        state_after=after,
        error=error,
        ack_result="success" if success else "timeout",
        attempts=1,
    )


class FakeExecutor:
    def __init__(self, pause=None, resume=None, stop=None, hold=None):
        self.pause_result = pause or command(
            "pause", True, GcodeState.RUNNING, GcodeState.PAUSE
        )
        self.resume_result = resume or command(
            "resume", True, GcodeState.PAUSE, GcodeState.RUNNING
        )
        self.stop_result = stop or command("stop", True, GcodeState.RUNNING, GcodeState.IDLE)
        self.hold_result = hold or command(
            "hold", True, GcodeState.PAUSE, GcodeState.PAUSE
        )
        self.calls = []
        self.hold_calls = []

    def pause(self):
        self.calls.append("pause")
        return self.pause_result

    def resume(self):
        self.calls.append("resume")
        return self.resume_result

    def stop(self):
        self.calls.append("stop")
        return self.stop_result

    def hold(self, nozzle, bed):
        self.hold_calls.append((nozzle, bed))
        return self.hold_result


def detection(kind="spaghetti"):
    return Detection(
        kind=kind,
        severity=Severity.P1,
        confidence=0.91,
        bbox=(1.0, 2.0, 30.0, 40.0),
        frame_seq=12,
        evidence_frames=4,
        area_growth=0.2,
        model_version="stub-v0",
    )


def snapshot():
    return PrinterSnapshot.from_report(
        {
            "print": {
                "gcode_state": "RUNNING",
                "layer_num": 5,
                "total_layer_num": 20,
                "bed_temper": 60,
            }
        }
    )


class Harness:
    def __init__(self, executor=None):
        self.db = Database(":memory:")
        self.executor = executor or FakeExecutor()
        self.holding = []
        self.reminders = []
        self.resolved = []
        self.errors = []
        self.manager = IncidentManager(
            self.db,
            self.executor,
            HoldConfig(max_wait_minutes=60, reminder_minutes=[10, 50]),
            now=lambda: NOW,
            on_holding=self.holding.append,
            on_reminder=lambda incident, minutes: self.reminders.append((incident, minutes)),
            on_resolved=self.resolved.append,
            on_error=lambda incident, message: self.errors.append((incident, message)),
        )

    def create(self):
        incident = self.manager.handle_detection(detection(), snapshot(), now=NOW)
        assert incident is not None
        return incident


def test_detection_pauses_and_starts_hold():
    harness = Harness()
    incident = harness.create()
    assert incident.state == IncidentState.HOLDING
    assert incident.hold_deadline == NOW + timedelta(minutes=60)
    assert incident.printer_state == GcodeState.PAUSE
    assert harness.executor.calls == ["pause"]
    assert harness.executor.hold_calls == [(150, 60.0)]
    assert harness.holding == [incident]
    assert harness.db.count_open_incidents() == 1
    actions = [event["action"] for event in harness.db.list_events(incident.id)]
    assert actions == ["detected", "paused"]


def test_duplicate_detection_reuses_open_incident():
    harness = Harness()
    first = harness.create()
    second = harness.manager.handle_detection(detection(), snapshot(), now=NOW)
    assert second.id == first.id
    assert harness.executor.calls == ["pause"]
    assert harness.db.count_open_incidents() == 1


def test_structural_anomaly_is_recorded_without_pausing():
    harness = Harness()
    result = harness.manager.handle_detection(detection("structural_anomaly"), snapshot(), now=NOW)
    assert result is None
    assert harness.executor.calls == []
    assert harness.db.count_open_incidents() == 0


def test_p2_observes_without_pausing_and_upgrades_to_p1():
    harness = Harness()
    p2 = detection()
    p2.severity = Severity.P2
    incident = harness.manager.handle_detection(p2, snapshot(), now=NOW)
    assert incident.state == IncidentState.MONITORING
    assert harness.executor.calls == []
    assert harness.db.count_open_incidents() == 1

    p1 = detection()
    upgraded = harness.manager.handle_detection(p1, snapshot(), now=NOW)
    assert upgraded.id == incident.id
    assert upgraded.severity == Severity.P1
    assert upgraded.state == IncidentState.HOLDING
    assert harness.executor.calls == ["pause"]


def test_p3_is_recorded_without_incident():
    harness = Harness()
    p3 = detection()
    p3.severity = Severity.P3
    result = harness.manager.handle_detection(p3, snapshot(), now=NOW)
    assert result is None
    assert harness.executor.calls == []
    assert harness.db.count_open_incidents() == 0


def test_reminders_fire_once_and_auto_stop_at_deadline():
    harness = Harness()
    incident = harness.create()
    harness.manager.tick(NOW + timedelta(minutes=10))
    assert harness.reminders[0][0].id == incident.id
    assert harness.reminders[0][1] == 10.0
    harness.manager.tick(NOW + timedelta(minutes=10))
    assert len(harness.reminders) == 1

    harness.manager.tick(NOW + timedelta(minutes=50))
    assert harness.reminders[-1][1] == 50.0
    harness.manager.tick(NOW + timedelta(minutes=59))
    assert len(harness.reminders) == 2

    harness.manager.tick(NOW + timedelta(minutes=60))
    stopped = harness.db.get_incident(incident.id)
    assert stopped.state == IncidentState.STOPPED
    assert stopped.outcome == IncidentOutcome.AUTO_STOPPED.value
    assert stopped.resolved_at is not None
    assert harness.executor.calls[-1] == "stop"
    assert harness.resolved[-1].id == incident.id


def test_auto_stop_failure_raises_error_state():
    executor = FakeExecutor(stop=command("stop", False, GcodeState.PAUSE, GcodeState.PAUSE, "boom"))
    harness = Harness(executor)
    incident = harness.create()
    harness.manager.tick(NOW + timedelta(minutes=60))
    failed = harness.db.get_incident(incident.id)
    assert failed.state == IncidentState.ERROR
    assert failed.outcome == IncidentOutcome.CONTROL_FAILED.value
    assert harness.errors


def test_resume_decision():
    harness = Harness()
    incident = harness.create()
    result = harness.manager.decide(
        incident.short_code, DecisionAction.RESUME, actor="feishu", now=NOW + timedelta(minutes=2)
    )
    assert result.success is True
    assert result.code == "ok"
    loaded = harness.db.get_incident(incident.id)
    assert loaded.state == IncidentState.MONITORING
    assert loaded.outcome == IncidentOutcome.RESUMED.value
    assert loaded.resolved_at is not None
    assert harness.executor.calls[-1] == "resume"
    assert harness.resolved[-1].id == incident.id


def test_false_positive_resumes_and_labels():
    harness = Harness()
    incident = harness.create()
    result = harness.manager.decide(
        incident.id, DecisionAction.FALSE_POSITIVE, actor="feishu", note="shadow", now=NOW
    )
    assert result.success is True
    loaded = harness.db.get_incident(incident.id)
    assert loaded.outcome == IncidentOutcome.FALSE_POSITIVE.value
    labels = harness.db.list_labels(incident.id)
    assert labels[0]["label"] == "false_positive"
    assert labels[0]["note"] == "shadow"


def test_stop_decision():
    harness = Harness()
    incident = harness.create()
    result = harness.manager.decide(incident.id, DecisionAction.STOP, actor="cli", now=NOW)
    assert result.success is True
    loaded = harness.db.get_incident(incident.id)
    assert loaded.state == IncidentState.STOPPED
    assert loaded.outcome == IncidentOutcome.USER_STOPPED.value


def test_resume_failure_keeps_incident_holding():
    executor = FakeExecutor(
        resume=command("resume", False, GcodeState.PAUSE, GcodeState.PAUSE, "timeout")
    )
    harness = Harness(executor)
    incident = harness.create()
    result = harness.manager.decide(incident.id, DecisionAction.RESUME, now=NOW)
    assert result.success is False
    assert result.code == "command_failed"
    loaded = harness.db.get_incident(incident.id)
    assert loaded.state == IncidentState.HOLDING
    assert loaded.resolved_at is None

    retry = harness.manager.decide(incident.id, DecisionAction.STOP, now=NOW)
    assert retry.success is True


def test_pause_failure_triggers_fail_safe_stop():
    executor = FakeExecutor(
        pause=command("pause", False, GcodeState.RUNNING, GcodeState.RUNNING, "no ack"),
        stop=command("stop", True, GcodeState.RUNNING, GcodeState.IDLE),
    )
    harness = Harness(executor)
    incident = harness.manager.handle_detection(detection(), snapshot(), now=NOW)
    assert incident is not None
    loaded = harness.db.get_incident(incident.id)
    assert loaded.state == IncidentState.STOPPED
    assert loaded.outcome == IncidentOutcome.PAUSE_FAILED_STOPPED.value
    assert harness.resolved[-1].id == incident.id


def test_pause_and_stop_failure_enters_error():
    executor = FakeExecutor(
        pause=command("pause", False, GcodeState.RUNNING, GcodeState.RUNNING, "no ack"),
        stop=command("stop", False, GcodeState.RUNNING, GcodeState.RUNNING, "still no"),
    )
    harness = Harness(executor)
    incident = harness.manager.handle_detection(detection(), snapshot(), now=NOW)
    loaded = harness.db.get_incident(incident.id)
    assert loaded.state == IncidentState.ERROR
    assert harness.errors
    assert harness.db.count_open_incidents() == 1


def test_decide_after_auto_stop_is_rejected():
    harness = Harness()
    incident = harness.create()
    harness.manager.tick(NOW + timedelta(minutes=60))
    result = harness.manager.decide(incident.id, DecisionAction.RESUME, now=NOW)
    assert result.success is False
    assert result.code == "already_resolved"


def test_unknown_incident_and_invalid_state():
    harness = Harness()
    missing = harness.manager.decide("NOPE", DecisionAction.RESUME, now=NOW)
    assert missing.code == "incident_not_found"

    incident = harness.create()
    incident.state = IncidentState.PAUSING
    harness.db.upsert_incident(incident)
    invalid = harness.manager.decide(incident.id, DecisionAction.RESUME, now=NOW)
    assert invalid.code == "invalid_state"


def test_label_incident_and_reject_unknown_label():
    harness = Harness()
    incident = harness.create()
    assert harness.manager.label_incident(incident.id, "true_positive", note="ok") is True
    assert harness.db.list_labels(incident.id)[0]["label"] == "true_positive"
    with pytest.raises(ValueError):
        harness.manager.label_incident(incident.id, "made_up")
