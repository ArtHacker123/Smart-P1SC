import json

from smart_p1_sc.models import (
    Candidate,
    GcodeState,
    Incident,
    IncidentState,
    PrinterSnapshot,
    Severity,
    deep_merge,
    parse_gcode_state,
)


def test_deep_merge_recurses_and_replaces_lists():
    base = {"print": {"gcode_state": "RUNNING", "hms": [1]}, "keep": True}
    update = {"print": {"layer_num": 4, "hms": []}}
    merged = deep_merge(base, update)
    assert merged["print"]["gcode_state"] == "RUNNING"
    assert merged["print"]["layer_num"] == 4
    assert merged["print"]["hms"] == []
    assert merged["keep"] is True
    assert "layer_num" not in base["print"]


def test_parse_gcode_state_aliases():
    assert parse_gcode_state("running") == GcodeState.RUNNING
    assert parse_gcode_state("PAUSED") == GcodeState.PAUSE
    assert parse_gcode_state("weird") == GcodeState.UNKNOWN
    assert parse_gcode_state(None) == GcodeState.UNKNOWN


def test_snapshot_partial_update_keeps_previous_state():
    first = PrinterSnapshot.from_report(
        {
            "print": {
                "gcode_state": "RUNNING",
                "layer_num": 3,
                "total_layer_num": 20,
                "nozzle_temper": 220.5,
                "bed_temper": 60,
            }
        }
    )
    assert first.gcode_state == GcodeState.RUNNING
    assert first.is_running

    ack = PrinterSnapshot.from_report(
        {"print": {"sequence_id": "10", "result": "success"}}, previous=first
    )
    assert ack.gcode_state == GcodeState.RUNNING
    assert ack.layer_num == 3
    assert ack.nozzle_temp == 220.5
    assert ack.raw["print"]["result"] == "success"


def test_snapshot_accepts_pushing_section():
    snapshot = PrinterSnapshot.from_report(
        {"pushing": {"gcode_state": "PAUSE", "layer_num": "5", "nozzle_temper": "150"}}
    )
    assert snapshot.gcode_state == GcodeState.PAUSE
    assert snapshot.is_paused
    assert snapshot.layer_num == 5
    assert snapshot.nozzle_temp == 150.0


def test_snapshot_terminal_states():
    assert PrinterSnapshot.from_report({"print": {"gcode_state": "FINISH"}}).is_terminal
    assert PrinterSnapshot.from_report({"print": {"gcode_state": "IDLE"}}).is_terminal
    assert not PrinterSnapshot.from_report({"print": {"gcode_state": "PREPARE"}}).is_terminal


def test_candidate_and_incident_are_json_serializable():
    candidate = Candidate(
        kind="spaghetti",
        confidence=0.912345,
        bbox=(1.0, 2.0, 30.0, 40.0),
        frame_seq=12,
        model_version="stub-v0",
    )
    assert candidate.to_dict()["confidence"] == 0.9123

    incident = Incident(
        id="inc-1",
        short_code="7F3A",
        kind="spaghetti",
        severity=Severity.P1,
        confidence=0.91,
        state=IncidentState.HOLDING,
        bbox=(1.0, 2.0, 30.0, 40.0),
        metadata={"source": "test"},
    )
    payload = incident.to_dict()
    assert payload["state"] == "HOLDING"
    assert incident.is_open is True
    json.dumps(payload)

    restored = Incident.from_dict(payload)
    assert restored.id == incident.id
    assert restored.short_code == incident.short_code
    assert restored.severity == Severity.P1
    assert restored.state == IncidentState.HOLDING
    assert restored.bbox == (1.0, 2.0, 30.0, 40.0)
    assert restored.metadata == {"source": "test"}
    assert restored.is_open is True
