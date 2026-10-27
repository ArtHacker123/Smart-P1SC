import pytest

from smart_p1_sc.config import DetectionConfig
from smart_p1_sc.detector import (
    FIRST_LAYER,
    SPAGHETTI,
    STRUCTURAL,
    CallableDetector,
    DetectionConfirmer,
    FrameAnalyzer,
    NullDetector,
    bbox_area_ratio,
    bbox_iou,
    has_hardware_fault,
)
from smart_p1_sc.models import Candidate, Frame, PrinterSnapshot, Severity, utcnow

BBOX = (0.1, 0.1, 0.3, 0.3)


def frame(seq=1, jpeg=b"\xff\xd8x\xff\xd9", width=640, height=480):
    return Frame(
        seq=seq,
        monotonic_ts=float(seq),
        wall_ts=utcnow(),
        jpeg=jpeg,
        width=width,
        height=height,
    )


def snapshot(state="RUNNING", layer=5, nozzle=220, hms=None, print_error=None):
    payload = {
        "print": {
            "gcode_state": state,
            "layer_num": layer,
            "nozzle_temper": nozzle,
            "print_error": print_error,
            "hms": hms or [],
        }
    }
    return PrinterSnapshot.from_report(payload)


def candidate(kind=SPAGHETTI, confidence=0.85, bbox=BBOX, seq=1):
    return Candidate(
        kind=kind,
        confidence=confidence,
        bbox=bbox,
        frame_seq=seq,
        model_version="test-v1",
    )


def confirmer(config=None, clock=None):
    config = config or DetectionConfig(
        window_frames=10,
        min_positive_frames=3,
        candidate_threshold=0.35,
        confirm_threshold=0.6,
    )
    return DetectionConfirmer(
        config,
        now_monotonic=(lambda: clock["now"]) if clock is not None else None,
    )


def test_bbox_iou_and_area_ratio():
    assert bbox_iou(BBOX, BBOX) == 1.0
    assert bbox_iou(BBOX, (0.5, 0.5, 0.6, 0.6)) == 0.0
    assert bbox_area_ratio(BBOX, frame()) == pytest.approx(0.04)
    pixel = (64.0, 48.0, 128.0, 96.0)
    assert bbox_area_ratio(pixel, frame()) == pytest.approx(0.01)
    assert bbox_area_ratio(None, frame()) is None


def test_single_frame_is_not_confirmed():
    conf = confirmer()
    assert conf.observe(frame(1), snapshot(), [candidate(seq=1)]) == []
    for seq in range(2, 11):
        conf.observe(frame(seq), snapshot(), [])
    assert conf.observe(frame(11), snapshot(), [candidate(seq=11)]) == []


def test_persistent_spatially_consistent_candidate_confirms():
    conf = confirmer()
    detections = []
    for seq in range(1, 4):
        detections.extend(conf.observe(frame(seq), snapshot(), [candidate(seq=seq)]))
    assert len(detections) == 1
    detection = detections[0]
    assert detection.kind == SPAGHETTI
    assert detection.severity == Severity.P1
    assert detection.evidence_frames == 3
    assert detection.model_version == "test-v1"


def test_spatially_scattered_candidates_do_not_confirm():
    conf = confirmer()
    boxes = [(0.0, 0.0, 0.1, 0.1), (0.5, 0.5, 0.6, 0.6), (0.2, 0.8, 0.3, 0.9)]
    for seq, box in enumerate(boxes, start=1):
        assert conf.observe(frame(seq), snapshot(), [candidate(bbox=box, seq=seq)]) == []


def test_first_layer_gate_and_area_severity():
    conf = confirmer()
    for seq in range(1, 6):
        assert (
            conf.observe(
                frame(seq),
                snapshot(layer=5),
                [candidate(kind=FIRST_LAYER, confidence=0.95, bbox=(0, 0, 0.5, 0.4), seq=seq)],
            )
            == []
        )

    detections = []
    for seq in range(6, 9):
        detections.extend(
            conf.observe(
                frame(seq),
                snapshot(layer=1),
                [candidate(kind=FIRST_LAYER, confidence=0.95, bbox=(0, 0, 0.5, 0.4), seq=seq)],
            )
        )
    assert detections[0].severity == Severity.P1

    small = confirmer()
    small_detections = []
    for seq in range(1, 4):
        small_detections.extend(
            small.observe(
                frame(seq),
                snapshot(layer=1),
                [candidate(kind=FIRST_LAYER, confidence=0.95, bbox=(0, 0, 0.2, 0.2), seq=seq)],
            )
        )
    assert small_detections[0].severity == Severity.P2


def test_structural_anomaly_is_p3():
    conf = confirmer()
    detections = []
    for seq in range(1, 4):
        detections.extend(
            conf.observe(frame(seq), snapshot(), [candidate(kind=STRUCTURAL, seq=seq)])
        )
    assert detections[0].severity == Severity.P3


def test_detection_only_runs_while_running_and_heated():
    conf = confirmer()
    for seq in range(1, 4):
        assert conf.observe(frame(seq), snapshot(state="PAUSE"), [candidate(seq=seq)]) == []

    cold = confirmer()
    for seq in range(1, 4):
        assert cold.observe(frame(seq), snapshot(nozzle=100), [candidate(seq=seq)]) == []


def test_hardware_fault_suppresses_visual_detection():
    conf = confirmer()
    faulty = snapshot(hms=[{"code": "0700_0001"}])
    assert has_hardware_fault(faulty) is True
    for seq in range(1, 4):
        assert conf.observe(frame(seq), faulty, [candidate(seq=seq)]) == []
    assert has_hardware_fault(snapshot(print_error="0")) is False


def test_suppression_blocks_same_region_until_area_grows():
    clock = {"now": 1000.0}
    conf = confirmer(clock=clock)
    for seq in range(1, 4):
        detections = conf.observe(frame(seq), snapshot(), [candidate(seq=seq)])
    assert len(detections) == 1

    conf.suppress(SPAGHETTI, BBOX, seconds=300, confidence=0.85, now_monotonic=1000.0)
    clock["now"] = 1001.0
    for seq in range(4, 7):
        assert conf.observe(frame(seq), snapshot(), [candidate(seq=seq)]) == []

    clock["now"] = 1002.0
    grown = (0.1, 0.1, 0.5, 0.5)
    detected = []
    for seq in range(7, 10):
        detected.extend(
            conf.observe(frame(seq), snapshot(), [candidate(bbox=grown, seq=seq)])
        )
    assert len(detected) == 1


def test_frame_analyzer_combines_detector_and_confirmer():
    conf = confirmer()
    detector = CallableDetector(lambda frame, snapshot: [candidate(seq=frame.seq)])
    analyzer = FrameAnalyzer(detector, conf)
    results = [analyzer.process(frame(seq), snapshot()) for seq in range(1, 4)]
    assert results[0].detections == []
    assert len(results[-1].detections) == 1
    assert len(results[-1].candidates) == 1


def test_null_detector_returns_no_candidates():
    assert NullDetector().detect(frame(), snapshot()) == []
