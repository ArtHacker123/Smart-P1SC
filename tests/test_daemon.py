import os
import time
from datetime import timedelta
from pathlib import Path

from smart_p1_sc.config import AppConfig
from smart_p1_sc.daemon import Daemon
from smart_p1_sc.db import Database
from smart_p1_sc.detector import CallableDetector
from smart_p1_sc.models import (
    Candidate,
    CommandResult,
    Frame,
    GcodeState,
    PrinterSnapshot,
    utcnow,
)


class FakeRing:
    def __init__(self):
        self.frames = []

    def latest(self):
        return self.frames[-1] if self.frames else None

    def snapshot(self, last_n=None):
        if last_n is None:
            return list(self.frames)
        return list(self.frames[-last_n:]) if last_n > 0 else []


class FakeCamera:
    def __init__(self):
        self.ring = FakeRing()
        self.started = False
        self.stopped = False
        self.last_frame_at = None

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def stats(self):
        return {
            "connected": self.started,
            "frames_received": len(self.ring.frames),
            "last_frame_at": self.last_frame_at,
        }


class FakeMqtt:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.started = False
        self.stopped = False
        self.connected = True

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def latest_snapshot(self):
        return self.snapshot


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def pause(self):
        self.calls.append(("pause",))
        return CommandResult(
            command="pause",
            success=True,
            state_before=GcodeState.RUNNING,
            state_after=GcodeState.PAUSE,
        )

    def resume(self):
        self.calls.append(("resume",))
        return CommandResult(
            command="resume",
            success=True,
            state_before=GcodeState.PAUSE,
            state_after=GcodeState.RUNNING,
        )

    def stop(self):
        self.calls.append(("stop",))
        return CommandResult(
            command="stop",
            success=True,
            state_before=GcodeState.PAUSE,
            state_after=GcodeState.IDLE,
        )

    def hold(self, nozzle_temperature_c, bed_temperature_c=None):
        self.calls.append(("hold", nozzle_temperature_c, bed_temperature_c))
        return CommandResult(command="hold", success=True, state_after=GcodeState.PAUSE)


class FakeNotifier:
    def __init__(self):
        self.events = []
        self.system_events = []

    def notify_event(self, event_type, incident, extra=None, idempotency_key=None):
        self.events.append((event_type, incident.short_code, extra))
        return True

    def notify_system(self, event_type, text, details=None, idempotency_key=None):
        self.system_events.append((event_type, text, details))
        return True


class FakeApi:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeLock:
    def __init__(self):
        self.acquired = False

    def acquire(self):
        self.acquired = True
        return True

    def release(self):
        self.acquired = False


def make_config(tmp_path):
    config = AppConfig()
    config.printer.host = "printer.local"
    config.printer.serial = "SERIAL"
    config.storage.database_path = str(tmp_path / "test.sqlite3")
    config.storage.media_dir = str(tmp_path / "media")
    config.hold.max_wait_minutes = 60
    config.detection.window_frames = 3
    config.detection.min_positive_frames = 3
    config.detection.candidate_threshold = 0.3
    config.detection.confirm_threshold = 0.6
    config.detection.p1_threshold = 0.8
    config.detection.first_layer_max = 1
    return config


def frame(seq):
    return Frame(
        seq=seq,
        monotonic_ts=float(seq),
        wall_ts=utcnow(),
        jpeg="\xff\xd8frame{}\xff\xd9".format(seq).encode("latin-1"),
        width=1280,
        height=720,
    )


def detector():
    return CallableDetector(
        lambda f, s: [
            Candidate(
                kind="spaghetti",
                confidence=0.95,
                bbox=(0.2, 0.2, 0.6, 0.6),
                frame_seq=f.seq,
                model_version="test",
            )
        ]
    )


def build(tmp_path, config=None, use_default_detector=True):
    camera = FakeCamera()
    snapshot = PrinterSnapshot(
        gcode_state=GcodeState.RUNNING, layer_num=5, nozzle_temp=210.0
    )
    mqtt = FakeMqtt(snapshot)
    executor = FakeExecutor()
    notifier = FakeNotifier()
    api = FakeApi()
    lock = FakeLock()
    options = {}
    if use_default_detector:
        options["detector"] = detector()
    daemon = Daemon(
        config or make_config(tmp_path),
        camera=camera,
        mqtt=mqtt,
        executor=executor,
        notifier=notifier,
        db=Database(str(tmp_path / "test.sqlite3")),
        api_server=api,
        lock=lock,
        tick_interval=0.01,
        **options,
    )
    return daemon, camera, mqtt, executor, notifier, api, lock


def make_reload_config(tmp_path):
    config = make_config(tmp_path)
    config.detection.backend = "onnx"
    config.detection.class_count = 1
    config.detection.class_map = {"0": "spaghetti"}
    model = tmp_path / "model.onnx"
    model.write_bytes(b"model-v1")
    config.detection.model_path = str(model)
    return config, model


def touch_model(model):
    stat = model.stat()
    os.utime(model, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))


def test_detector_reload_waits_for_idle_and_swaps_atomically(tmp_path, monkeypatch):
    config, model = make_reload_config(tmp_path)
    created = []

    def fake_create(_config):
        new_detector = detector()
        created.append(new_detector)
        return new_detector

    monkeypatch.setattr("smart_p1_sc.daemon.create_detector", fake_create)
    daemon, _, mqtt, _, _, _, _ = build(
        tmp_path, config=config, use_default_detector=False
    )
    original = daemon.analyzer.detector
    assert len(created) == 1

    touch_model(model)
    daemon._last_model_check_at = 0.0
    assert daemon._maybe_reload_detector(mqtt.snapshot) is False
    assert daemon.analyzer.detector is original
    assert len(created) == 1

    mqtt.snapshot = PrinterSnapshot(gcode_state=GcodeState.IDLE)
    daemon._last_model_check_at = 0.0
    assert daemon._maybe_reload_detector(mqtt.snapshot) is True

    assert daemon.analyzer.detector is created[-1]
    assert daemon.analyzer.detector is not original
    assert len(created) == 2


def test_detector_reload_failure_keeps_working_detector(tmp_path, monkeypatch):
    config, model = make_reload_config(tmp_path)
    created = []

    def fake_create(_config):
        if created:
            raise RuntimeError("broken model")
        new_detector = detector()
        created.append(new_detector)
        return new_detector

    monkeypatch.setattr("smart_p1_sc.daemon.create_detector", fake_create)
    daemon, _, mqtt, _, _, _, _ = build(
        tmp_path, config=config, use_default_detector=False
    )
    original = daemon.analyzer.detector
    touch_model(model)
    mqtt.snapshot = PrinterSnapshot(gcode_state=GcodeState.IDLE)
    daemon._last_model_check_at = 0.0

    assert daemon._maybe_reload_detector(mqtt.snapshot) is False
    assert daemon.analyzer.detector is original
    assert len(created) == 1

    daemon._last_model_check_at = 0.0
    assert daemon._maybe_reload_detector(mqtt.snapshot) is False
    assert daemon.analyzer.detector is original
    assert len(created) == 1


def test_detection_pauses_holds_saves_and_notifies(tmp_path):
    daemon, camera, _, executor, notifier, _, _ = build(tmp_path)
    daemon.dispatcher.start()
    try:
        for seq in range(1, 4):
            camera.ring.frames.append(frame(seq))
            daemon._step()

        assert ("pause",) in executor.calls
        assert any(call[0] == "hold" and call[1] == 150 for call in executor.calls)
        assert daemon.db.count_open_incidents() == 1
        incident = daemon.db.list_open_incidents()[0]
        assert incident.state.value == "HOLDING"
        assert incident.filmstrip_dir is not None
        assert len(daemon.store.list_incident_frames(incident.id)) == 3

        assert daemon.dispatcher.flush(5.0)
        assert notifier.events[0][0] == "holding"
        assert notifier.events[0][1] == incident.short_code

        detail = daemon.get_incident_detail(incident.short_code)
        assert detail is not None
        assert len(detail["events"]) >= 1
        assert detail["frames"]
        assert daemon.read_incident_frame(incident.short_code, 0).startswith(b"\xff\xd8")
        assert daemon.read_incident_frame(incident.short_code, 999) is None
    finally:
        daemon.stop()


def test_resume_resolves_and_suppresses(tmp_path):
    daemon, camera, _, executor, notifier, _, _ = build(tmp_path)
    daemon.dispatcher.start()
    try:
        for seq in range(1, 4):
            camera.ring.frames.append(frame(seq))
            daemon._step()
        incident = daemon.db.list_open_incidents()[0]

        result = daemon.decide(incident.short_code, "resume", "tester", None)
        assert result.success is True
        assert ("resume",) in executor.calls
        resolved = daemon.db.get_incident(incident.id)
        assert resolved.resolved_at is not None
        assert resolved.outcome == "resumed"

        assert daemon.dispatcher.flush(5.0)
        assert any(event[0] == "resolved" for event in notifier.events)

        again = daemon.decide(incident.short_code, "resume", "tester", None)
        assert again.success is False
        assert again.code == "already_resolved"

        missing = daemon.decide("NOPE", "stop", "tester", None)
        assert missing.code == "incident_not_found"
        invalid = daemon.decide(incident.short_code, "explode", "tester", None)
        assert invalid.code == "invalid_action"
    finally:
        daemon.stop()


def test_low_disk_alert(tmp_path):
    daemon, _, _, _, notifier, _, _ = build(tmp_path)
    daemon.config.storage.min_free_disk_mb = 10 ** 9
    daemon.dispatcher.start()
    try:
        daemon._check_storage_health()
        assert daemon.dispatcher.flush(2.0) is True
        assert daemon.storage_status()["low"] is True
        assert any(event[0] == "disk_low" for event in notifier.system_events)
        assert daemon.db.count_unsent_notifications() == 0
    finally:
        daemon.stop()


def test_retention_removes_resolved_incident_and_media(tmp_path):
    daemon, camera, _, _, _, _, _ = build(tmp_path)
    for seq in range(1, 4):
        camera.ring.frames.append(frame(seq))
        daemon._step()
    incident = daemon.db.list_open_incidents()[0]
    media_dir = Path(incident.filmstrip_dir)
    assert media_dir.exists()

    incident.resolved_at = utcnow() - timedelta(days=40)
    daemon.db.upsert_incident(incident)
    assert daemon.cleanup_retention() == 1
    assert daemon.db.get_incident(incident.id) is None
    assert not media_dir.exists()


def test_pending_notification_replays_after_restart(tmp_path):
    first, camera, _, _, _, _, _ = build(tmp_path)
    for seq in range(1, 4):
        camera.ring.frames.append(frame(seq))
        first._step()
    assert first.db.count_unsent_notifications() >= 1

    second, _, _, _, notifier, _, _ = build(tmp_path)
    second.dispatcher.start()
    try:
        assert second.dispatcher.flush(2.0) is True
        assert any(event[0] == "holding" for event in notifier.events)
        assert second.db.count_unsent_notifications() == 0
    finally:
        second.stop()


def test_short_code_and_idempotency(tmp_path):
    daemon, camera, _, executor, _, _, _ = build(tmp_path)
    daemon.dispatcher.start()
    try:
        for seq in range(1, 4):
            camera.ring.frames.append(frame(seq))
            daemon._step()
        incident = daemon.db.list_open_incidents()[0]

        mismatch = daemon.decide(
            incident.id, "resume", "tester", None, short_code="0000", idempotency_key="bad"
        )
        assert mismatch.code == "short_code_mismatch"
        assert ("resume",) not in executor.calls

        first = daemon.decide(
            incident.id,
            "resume",
            "tester",
            None,
            short_code=incident.short_code,
            idempotency_key="key-1",
        )
        assert first.success is True
        second = daemon.decide(
            incident.id,
            "resume",
            "tester",
            None,
            short_code=incident.short_code,
            idempotency_key="key-1",
        )
        assert second.success is True
        assert second.code == "ok"
        assert [call for call in executor.calls if call[0] == "resume"] == [("resume",)]

        conflict = daemon.decide(
            incident.id, "stop", "tester", None, idempotency_key="key-1"
        )
        assert conflict.code == "idempotency_conflict"
    finally:
        daemon.stop()


def test_label_and_status(tmp_path):
    daemon, camera, mqtt, _, _, _, _ = build(tmp_path)
    daemon.dispatcher.start()
    try:
        for seq in range(1, 4):
            camera.ring.frames.append(frame(seq))
            daemon._step()
        incident = daemon.db.list_open_incidents()[0]

        assert daemon.label(incident.short_code, "true_positive", "tester", "checked") == (
            True,
            "ok",
        )
        assert daemon.label(incident.short_code, "bogus", "tester", None) == (
            False,
            "invalid_label",
        )
        assert daemon.label("NOPE", "unknown", "tester", None) == (
            False,
            "incident_not_found",
        )

        status = daemon.status()
        assert status["printer"]["gcode_state"] == "RUNNING"
        assert status["open_incidents"] == 1
        assert daemon.latest_frame().seq == 3
    finally:
        daemon.stop()


def test_camera_health_degraded_and_recovered(tmp_path):
    daemon, camera, _, _, notifier, _, _ = build(tmp_path)
    daemon.dispatcher.start()
    try:
        camera.last_frame_at = time.time() - 120
        daemon._step()
        assert daemon.status()["camera"]["health"] == "DEGRADED"
        assert daemon.dispatcher.flush(5.0)
        assert any(event[0] == "camera_degraded" for event in notifier.system_events)
        assert any(
            event[0] == "camera_stall_while_printing" for event in notifier.system_events
        )

        camera.last_frame_at = time.time()
        daemon._step()
        assert daemon.status()["camera"]["health"] == "OK"
        assert daemon.dispatcher.flush(5.0)
        assert any(event[0] == "camera_recovered" for event in notifier.system_events)
    finally:
        daemon.stop()


def test_start_and_stop_lifecycle(tmp_path):
    daemon, camera, mqtt, _, _, api, lock = build(tmp_path)
    daemon.start()
    assert camera.started and mqtt.started and api.started and lock.acquired
    time.sleep(0.05)
    daemon.stop()
    assert camera.stopped and mqtt.stopped and api.stopped
    assert not lock.acquired
