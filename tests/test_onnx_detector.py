import io
from datetime import datetime, timezone

import numpy as np
import pytest
from PIL import Image

from smart_p1_sc.config import ConfigError, DetectionConfig
from smart_p1_sc.detector import NullDetector
from smart_p1_sc.models import Frame
from smart_p1_sc.onnx_detector import (
    DetectorError,
    OnnxDetector,
    create_detector,
    decode_output,
)


class FakeInput:
    name = "images"
    shape = [1, 3, 64, 64]


class FakeOutput:
    name = "predictions"


class FakeSession:
    def __init__(self, output, input_shape=None):
        self.output = output
        self.input = FakeInput()
        if input_shape is not None:
            self.input.shape = input_shape
        self.last_feed = None

    def get_inputs(self):
        return [self.input]

    def get_outputs(self):
        return [FakeOutput()]

    def run(self, names, feed):
        self.last_feed = feed
        return [self.output]


def make_frame(width=32, height=24, color=(255, 0, 0)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG")
    return Frame(
        seq=7,
        monotonic_ts=1.0,
        wall_ts=datetime.now(timezone.utc),
        jpeg=buffer.getvalue(),
        width=width,
        height=height,
    )


def make_config(**overrides):
    values = dict(
        backend="onnx",
        model_version="test-v1",
        input_width=64,
        input_height=64,
        class_count=2,
        class_map={"0": "spaghetti", "1": "first_layer_detachment"},
        score_threshold=0.25,
    )
    values.update(overrides)
    return DetectionConfig(**values)


def test_preprocess_builds_normalized_rgb_nchw_tensor():
    detector = OnnxDetector(FakeSession(np.zeros((1, 1, 6))), make_config())
    tensor = detector.preprocess(make_frame(color=(255, 0, 0)).jpeg)
    assert tensor.shape == (1, 3, 64, 64)
    assert tensor.dtype == np.float32
    assert tensor[0, 0, 32, 32] > 0.9
    assert tensor[0, 1, 32, 32] < 0.1
    assert tensor[0, 2, 32, 32] < 0.1
    assert 0.0 <= float(tensor.min()) <= float(tensor.max()) <= 1.0


def test_preprocess_letterbox_pads_non_square_frame():
    detector = OnnxDetector(FakeSession(np.zeros((1, 1, 6))), make_config())
    tensor = detector.preprocess(make_frame(width=80, height=40, color=(255, 0, 0)).jpeg)
    assert tensor.shape == (1, 3, 64, 64)
    # 80x40 -> scale 0.8 -> 64x32, padded 16px top and bottom with gray 114.
    assert tensor[0, 0, 0, 32] == pytest.approx(114 / 255.0, abs=0.02)
    assert tensor[0, 1, 0, 32] == pytest.approx(114 / 255.0, abs=0.02)
    assert tensor[0, 2, 0, 32] == pytest.approx(114 / 255.0, abs=0.02)
    assert tensor[0, 0, 32, 32] > 0.9
    assert tensor[0, 1, 32, 32] < 0.1
    assert tensor[0, 2, 32, 32] < 0.1


def test_letterbox_requires_opencv(monkeypatch):
    monkeypatch.setattr("smart_p1_sc.onnx_detector.cv2", None)
    with pytest.raises(DetectorError):
        OnnxDetector(FakeSession(np.zeros((1, 1, 6))), make_config())

    detector = OnnxDetector(
        FakeSession(np.zeros((1, 1, 6))),
        make_config(resize_mode="stretch"),
    )
    assert detector.preprocess(make_frame().jpeg).shape == (1, 3, 64, 64)


def test_letterbox_boxes_are_mapped_back_to_frame_coordinates():
    output = np.array(
        [[[32.0, 32.0, 64.0, 32.0, 0.9, 0.0]]], dtype=np.float32
    )
    detector = OnnxDetector(
        FakeSession(output),
        make_config(
            output_format="cxcywh_class",
            box_coordinates="pixels",
            resize_mode="letterbox",
        ),
    )
    candidates = detector.detect(make_frame(width=80, height=40), None)
    assert len(candidates) == 1
    assert candidates[0].bbox == pytest.approx((0.0, 0.0, 1.0, 1.0))


def test_stretch_mode_keeps_full_frame_normalized_boxes():
    output = np.array(
        [[[32.0, 32.0, 64.0, 64.0, 0.9, 0.0]]], dtype=np.float32
    )
    detector = OnnxDetector(
        FakeSession(output),
        make_config(
            output_format="cxcywh_class",
            box_coordinates="pixels",
            resize_mode="stretch",
        ),
    )
    candidates = detector.detect(make_frame(width=80, height=40), None)
    assert len(candidates) == 1
    assert candidates[0].bbox == pytest.approx((0.0, 0.0, 1.0, 1.0))


def test_preprocess_uses_static_model_shape_and_bgr():
    detector = OnnxDetector(
        FakeSession(np.zeros((1, 1, 6)), input_shape=[1, 3, 32, 48]),
        make_config(channel_order="bgr"),
    )
    tensor = detector.preprocess(make_frame(color=(255, 0, 0)).jpeg)
    assert tensor.shape == (1, 3, 32, 48)
    assert tensor[0, 0, 16, 24] < 0.1
    assert tensor[0, 2, 16, 24] > 0.9


def test_detects_xyxy_score_class_output():
    output = np.array(
        [
            [
                [0.1, 0.2, 0.4, 0.5, 0.9, 0],
                [0.5, 0.5, 0.9, 0.9, 0.8, 7],
            ]
        ],
        dtype=np.float32,
    )
    detector = OnnxDetector(FakeSession(output), make_config())
    candidates = detector.detect(make_frame(), None)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.kind == "spaghetti"
    assert candidate.model_version == "test-v1"
    assert candidate.frame_seq == 7
    assert candidate.bbox == pytest.approx((0.1, 0.2, 0.4, 0.5))
    assert candidate.confidence == pytest.approx(0.9)


def test_detects_transposed_yolov8_class_output():
    output = np.zeros((1, 6, 2), dtype=np.float32)
    output[0, :, 0] = [0.5, 0.5, 0.2, 0.2, 0.8, 0.1]
    output[0, :, 1] = [0.2, 0.3, 0.1, 0.1, 0.1, 0.7]
    candidates = decode_output(output, make_config(), 7)
    assert len(candidates) == 2
    assert candidates[0].kind == "spaghetti"
    assert candidates[0].bbox == pytest.approx((0.4, 0.4, 0.6, 0.6))
    assert candidates[0].confidence == pytest.approx(0.8)
    assert candidates[1].kind == "first_layer_detachment"
    assert candidates[1].confidence == pytest.approx(0.7)


def test_explicit_format_still_transposes_channel_first_output():
    output = np.zeros((1, 6, 2), dtype=np.float32)
    output[0, :, 0] = [0.5, 0.5, 0.2, 0.2, 0.8, 0.1]
    output[0, :, 1] = [0.2, 0.3, 0.1, 0.1, 0.1, 0.7]
    candidates = decode_output(
        output, make_config(output_format="cxcywh_class"), 7
    )
    assert len(candidates) == 2
    assert candidates[0].kind == "spaghetti"
    assert candidates[0].bbox == pytest.approx((0.4, 0.4, 0.6, 0.6))
    assert candidates[0].confidence == pytest.approx(0.8)
    assert candidates[1].kind == "first_layer_detachment"
    assert candidates[1].confidence == pytest.approx(0.7)


def test_detects_yolov5_objectness_output():
    output = np.array(
        [[[0.5, 0.5, 0.2, 0.2, 0.8, 0.9, 0.1]]], dtype=np.float32
    )
    candidates = decode_output(output, make_config(), 7)
    assert len(candidates) == 1
    assert candidates[0].confidence == pytest.approx(0.72)
    assert candidates[0].bbox == pytest.approx((0.4, 0.4, 0.6, 0.6))


def test_explicit_class_output_format_skips_auto_detection():
    output = np.array(
        [[[0.5, 0.5, 0.2, 0.2, 0.1, 0.9]]], dtype=np.float32
    )
    candidates = decode_output(
        output, make_config(output_format="cxcywh_class"), 7
    )
    assert len(candidates) == 1
    assert candidates[0].kind == "first_layer_detachment"


def test_pixel_boxes_are_normalized():
    output = np.array(
        [[[10, 20, 100, 200, 0.9, 0]]], dtype=np.float32
    )
    candidates = decode_output(
        output,
        make_config(input_width=100, input_height=200, box_coordinates="pixels"),
        7,
    )
    assert candidates[0].bbox == pytest.approx((0.1, 0.1, 1.0, 1.0))


def test_pixel_boxes_use_static_model_dimensions():
    output = np.array(
        [[[16, 24, 32, 48, 0.9, 0]]], dtype=np.float32
    )
    detector = OnnxDetector(
        FakeSession(output, input_shape=[1, 3, 48, 32]),
        make_config(input_width=640, input_height=640, box_coordinates="pixels"),
    )
    candidates = detector.detect(make_frame(), None)
    assert candidates[0].bbox == pytest.approx((0.5, 0.5, 1.0, 1.0))


def test_unknown_classes_and_low_scores_are_filtered():
    output = np.array(
        [
            [
                [0.1, 0.1, 0.2, 0.2, 0.9, 5],
                [0.3, 0.3, 0.4, 0.4, 0.1, 0],
            ]
        ],
        dtype=np.float32,
    )
    assert decode_output(output, make_config(), 7) == []


def test_nms_suppresses_overlapping_same_class_boxes():
    output = np.array(
        [
            [
                [0.1, 0.1, 0.5, 0.5, 0.9, 0],
                [0.12, 0.12, 0.52, 0.52, 0.8, 0],
                [0.6, 0.6, 0.9, 0.9, 0.7, 0],
            ]
        ],
        dtype=np.float32,
    )
    candidates = decode_output(output, make_config(), 7)
    assert len(candidates) == 2
    assert candidates[0].confidence == pytest.approx(0.9)
    assert candidates[1].confidence == pytest.approx(0.7)


def test_invalid_class_map_kind_is_rejected():
    with pytest.raises(DetectorError):
        OnnxDetector(
            FakeSession(np.zeros((1, 1, 6))),
            make_config(class_map={"0": "unknown"}),
        )


def test_auto_backend_falls_back_when_model_is_missing(tmp_path):
    config = make_config(model_path=str(tmp_path / "missing.onnx"))
    config.backend = "auto"
    assert isinstance(create_detector(config), NullDetector)


def test_explicit_onnx_backend_requires_model(tmp_path):
    config = make_config(model_path=str(tmp_path / "missing.onnx"))
    with pytest.raises(DetectorError):
        create_detector(config)


def test_auto_backend_falls_back_when_runtime_is_missing(tmp_path, monkeypatch):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"not-a-real-model")
    config = make_config(model_path=str(model))
    config.backend = "auto"
    monkeypatch.setattr("smart_p1_sc.onnx_detector._load_onnxruntime", lambda: None)
    assert isinstance(create_detector(config), NullDetector)


def test_onnx_backend_creates_session_with_configured_providers(
    tmp_path, monkeypatch
):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"not-a-real-model")

    class FakeRuntime:
        def __init__(self):
            self.calls = []

        def InferenceSession(self, path, providers):
            self.calls.append((path, providers))
            return FakeSession(np.zeros((1, 1, 6)))

    runtime = FakeRuntime()
    config = make_config(
        model_path=str(model),
        providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
    )
    monkeypatch.setattr(
        "smart_p1_sc.onnx_detector._load_onnxruntime", lambda: runtime
    )
    detector = create_detector(config)
    assert isinstance(detector, OnnxDetector)
    assert runtime.calls == [
        (str(model), ["CUDAExecutionProvider", "CPUExecutionProvider"])
    ]


def test_onnx_backend_requires_class_map():
    with pytest.raises(ConfigError):
        DetectionConfig(backend="onnx", class_map={})
