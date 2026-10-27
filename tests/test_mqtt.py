import json
from types import SimpleNamespace

import pytest

from smart_p1_sc.config import MqttConfig
from smart_p1_sc.models import GcodeState, PrinterSnapshot
from smart_p1_sc.mqtt import (
    MqttCommandError,
    PrinterCommandExecutor,
    PrinterMqttClient,
    parse_ack,
)


def test_parse_ack_print_and_pushing():
    assert parse_ack({"print": {"sequence_id": "7", "result": "SUCCESS"}}) == ("7", "success")
    assert parse_ack({"pushing": {"sequence_id": 8, "result": "Failed"}}) == ("8", "failed")
    assert parse_ack({"print": {"sequence_id": "1"}}) is None
    assert parse_ack({}) is None


def make_client():
    snapshots = []
    raw = []
    client = PrinterMqttClient(
        "127.0.0.1",
        "SERIAL",
        "12345678",
        MqttConfig(),
        client_factory=lambda client_id: None,
        on_snapshot=snapshots.append,
        on_raw=raw.append,
    )
    return client, snapshots, raw


def test_on_message_updates_snapshot_and_ack():
    client, snapshots, raw = make_client()
    payload = {
        "print": {
            "command": "push_status",
            "gcode_state": "RUNNING",
            "layer_num": 4,
            "total_layer_num": 10,
        }
    }
    client._on_message(None, None, SimpleNamespace(payload=json.dumps(payload).encode()))
    snapshot = client.latest_snapshot()
    assert snapshot.gcode_state == GcodeState.RUNNING
    assert snapshot.layer_num == 4
    assert len(snapshots) == 1
    assert raw == [payload]

    ack = {"print": {"sequence_id": "3", "result": "success"}}
    client._on_message(None, None, SimpleNamespace(payload=json.dumps(ack).encode()))
    outcome, _ = client.wait_for_command("3", {GcodeState.PAUSE}, 0.01)
    assert outcome == "timeout"


def test_on_message_ignores_invalid_json():
    client, snapshots, _ = make_client()
    client._on_message(None, None, SimpleNamespace(payload=b"not-json"))
    assert client.latest_snapshot() is None
    assert snapshots == []


def test_wait_for_command_returns_state():
    client, _, _ = make_client()
    payload = {"print": {"gcode_state": "PAUSE", "layer_num": 3}}
    client._on_message(None, None, SimpleNamespace(payload=json.dumps(payload).encode()))
    outcome, snapshot = client.wait_for_command("1", {GcodeState.PAUSE}, 0.01)
    assert outcome == "state"
    assert snapshot.gcode_state == GcodeState.PAUSE


def test_wait_for_command_times_out_without_snapshot():
    client, _, _ = make_client()
    outcome, snapshot = client.wait_for_command("1", {GcodeState.PAUSE}, 0.01)
    assert outcome == "timeout"
    assert snapshot is None


class FakeCommandClient:
    def __init__(self, before, results):
        self.snapshot = PrinterSnapshot.from_report({"print": {"gcode_state": before}})
        self.results = list(results)
        self.published = []
        self.next_result = None

    def latest_snapshot(self):
        return self.snapshot

    def publish_command(self, command, sequence_id):
        self.published.append((command, sequence_id))
        outcome, state = self.results.pop(0)
        if outcome == "raise":
            raise MqttCommandError("boom")
        self.next_result = (outcome, state)

    def wait_for_command(self, sequence_id, expected_states, timeout):
        outcome, state = self.next_result
        if state is not None:
            self.snapshot = PrinterSnapshot.from_report({"print": {"gcode_state": state}})
        return outcome, self.snapshot


def executor(client):
    return PrinterCommandExecutor(client, MqttConfig(command_retries=3, command_timeout_seconds=0.01))


def test_pause_success():
    client = FakeCommandClient("RUNNING", [("state", "PAUSE")])
    result = executor(client).pause()
    assert result.success is True
    assert result.attempts == 1
    assert result.state_after == GcodeState.PAUSE
    assert client.published == [("pause", "1")]


def test_pause_is_idempotent():
    client = FakeCommandClient("PAUSE", [])
    result = executor(client).pause()
    assert result.success is True
    assert result.attempts == 0
    assert client.published == []


def test_pause_refuses_when_not_printing():
    client = FakeCommandClient("IDLE", [])
    result = executor(client).pause()
    assert result.success is False
    assert result.error == "printer is not printing"
    assert client.published == []


def test_pause_retries_then_succeeds():
    client = FakeCommandClient("RUNNING", [("timeout", None), ("state", "PAUSE")])
    result = executor(client).pause()
    assert result.success is True
    assert result.attempts == 2
    assert len(client.published) == 2


def test_pause_retries_publish_exception_then_succeeds():
    client = FakeCommandClient("RUNNING", [("raise", None), ("state", "PAUSE")])
    result = executor(client).pause()
    assert result.success is True
    assert result.attempts == 2


def test_pause_fails_after_retries():
    client = FakeCommandClient("RUNNING", [("timeout", None)] * 3)
    result = executor(client).pause()
    assert result.success is False
    assert result.attempts == 3
    assert result.ack_result == "timeout"


def test_ack_failure_triggers_retry():
    client = FakeCommandClient("RUNNING", [("ack_failed", None), ("state", "PAUSE")])
    result = executor(client).pause()
    assert result.success is True
    assert result.attempts == 2


def test_resume_and_stop_expected_states():
    resume_client = FakeCommandClient("PAUSE", [("state", "RUNNING")])
    assert executor(resume_client).resume().success is True

    stop_client = FakeCommandClient("RUNNING", [("state", "IDLE")])
    result = executor(stop_client).stop()
    assert result.success is True
    assert result.state_after == GcodeState.IDLE


def test_stop_is_idempotent_when_finished():
    client = FakeCommandClient("FINISH", [])
    result = executor(client).stop()
    assert result.success is True
    assert client.published == []
