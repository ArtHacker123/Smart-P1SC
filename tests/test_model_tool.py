import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from smart_p1_sc.model_tool import (
    ModelToolError,
    _dataset_class_names,
    _normalize_dataset,
    check_image,
    class_map_from_names,
    export_onnx,
    inspect_model,
    metadata_path,
    parse_class_map,
    parse_class_names,
    read_metadata,
    recommend_output,
    write_metadata,
)


class FakeTensor:
    def __init__(self, name, shape, tensor_type="tensor(float)"):
        self.name = name
        self.shape = shape
        self.type = tensor_type


class FakeSession:
    def __init__(self, inputs, outputs):
        self.inputs = inputs
        self.outputs = outputs

    def get_inputs(self):
        return self.inputs

    def get_outputs(self):
        return self.outputs


def test_parse_class_names_accepts_supported_names():
    assert parse_class_names("spaghetti, first_layer_detachment") == [
        "spaghetti",
        "first_layer_detachment",
    ]


def test_parse_class_names_rejects_unknown_names():
    with pytest.raises(ModelToolError):
        parse_class_names("spaghetti, fire")


def test_parse_class_map_accepts_sparse_indexes():
    assert parse_class_map("0=spaghetti,2=structural_anomaly") == {
        "0": "spaghetti",
        "2": "structural_anomaly",
    }


def test_parse_class_map_rejects_invalid_entries():
    with pytest.raises(ModelToolError):
        parse_class_map("spaghetti")
    with pytest.raises(ModelToolError):
        parse_class_map("x=spaghetti")
    with pytest.raises(ModelToolError):
        parse_class_map("0=unknown")


def test_class_map_from_names_uses_indexes():
    assert class_map_from_names(
        ["spaghetti", "first_layer_detachment", "structural_anomaly"]
    ) == {
        "0": "spaghetti",
        "1": "first_layer_detachment",
        "2": "structural_anomaly",
    }


@pytest.mark.parametrize(
    "shape, expected_format, expected_class_count",
    [
        ([1, 7, 8400], "cxcywh_class", 3),
        ([1, 84, 8400], "cxcywh_class", 80),
        ([1, 25200, 8], "cxcywh_object_class", 3),
        ([1, 300, 6], "xyxy_score_class", None),
        ([1, 6, 8400], "cxcywh_class", 2),
    ],
)
def test_recommend_output_for_common_yolo_shapes(
    shape, expected_format, expected_class_count
):
    result = recommend_output(shape)
    assert result["output_format"] == expected_format
    assert result["class_count"] == expected_class_count


def test_recommend_output_rejects_dynamic_shapes():
    with pytest.raises(ModelToolError):
        recommend_output([1, 7, "anchors"])


def test_dataset_class_names_sorts_numeric_keys():
    assert _dataset_class_names(
        {"names": {"2": "structural_anomaly", "0": "spaghetti", "1": "detachment"}}
    ) == ["spaghetti", "detachment", "structural_anomaly"]


def test_normalize_dataset_resolves_relative_path_from_working_directory(
    tmp_path, monkeypatch
):
    training = tmp_path / "training"
    training.mkdir()
    root = tmp_path / "datasets" / "sample"
    root.mkdir(parents=True)
    dataset = training / "dataset.yaml"
    dataset.write_text(
        "path: datasets/sample\ntrain: images/train\nval: images/val\n"
        "names:\n  0: spaghetti\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    normalized, temp_dir, names = _normalize_dataset(dataset)
    try:
        payload = yaml.safe_load(Path(normalized).read_text(encoding="utf-8"))
        assert Path(payload["path"]) == root.resolve()
        assert names == ["spaghetti"]
    finally:
        temp_dir.cleanup()


def test_metadata_round_trip(tmp_path):
    model = tmp_path / "models" / "spaghetti.onnx"
    model.parent.mkdir()
    model.write_bytes(b"model")
    path = write_metadata(
        model,
        ["spaghetti", "first_layer_detachment", "structural_anomaly"],
        "spaghetti-v1",
        640,
        "best.pt",
    )
    assert path == metadata_path(model)
    payload = read_metadata(model)
    assert payload is not None
    assert payload["model_version"] == "spaghetti-v1"
    assert payload["class_count"] == 3
    assert payload["class_map"]["1"] == "first_layer_detachment"
    assert payload["input_width"] == 640
    assert json.loads(path.read_text(encoding="utf-8"))["source_weights"] == "best.pt"


def test_export_rejects_class_count_mismatch(tmp_path, monkeypatch):
    weights = tmp_path / "best.pt"
    weights.write_bytes(b"weights")

    class FakeModel:
        names = {str(index): "class{}".format(index) for index in range(80)}

        def export(self, **kwargs):
            raise AssertionError("export should not run")

    monkeypatch.setattr(
        "smart_p1_sc.model_tool._load_ultralytics", lambda: lambda path: FakeModel()
    )
    with pytest.raises(ModelToolError) as error:
        export_onnx(
            weights,
            tmp_path / "spaghetti.onnx",
            640,
            12,
            ["spaghetti", "first_layer_detachment", "structural_anomaly"],
            "spaghetti-v1",
        )
    assert error.value.code == "class_count_mismatch"


def test_inspect_model_reports_recommended_config(tmp_path, monkeypatch):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"model")
    session = FakeSession(
        [FakeTensor("images", [1, 3, 640, 640])],
        [FakeTensor("output0", [1, 7, 8400])],
    )

    class FakeRuntime:
        def InferenceSession(self, path, providers):
            assert path == str(model)
            assert providers == ["CPUExecutionProvider"]
            return session

    monkeypatch.setattr(
        "smart_p1_sc.model_tool._load_onnxruntime", lambda: FakeRuntime()
    )
    result = inspect_model(model)
    assert result["code"] == "inspected"
    assert result["recommended"]["input_width"] == 640
    assert result["recommended"]["input_height"] == 640
    assert result["recommended"]["class_count"] == 3
    assert result["recommended"]["output_format"] == "cxcywh_class"


def test_inspect_model_rejects_multiple_outputs(tmp_path, monkeypatch):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"model")
    session = FakeSession(
        [FakeTensor("images", [1, 3, 640, 640])],
        [FakeTensor("boxes", [1, 100, 4]), FakeTensor("scores", [1, 100, 3])],
    )

    class FakeRuntime:
        def InferenceSession(self, path, providers):
            return session

    monkeypatch.setattr(
        "smart_p1_sc.model_tool._load_onnxruntime", lambda: FakeRuntime()
    )
    with pytest.raises(ModelToolError):
        inspect_model(model)


def test_check_image_uses_explicit_class_count(tmp_path, monkeypatch):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"model")
    image = tmp_path / "test.jpg"
    image.write_bytes(b"\xff\xd8x\xff\xd9")
    seen = {}

    class FakeDetector:
        def detect(self, frame, snapshot):
            return []

    def fake_create_detector(config):
        seen["config"] = config
        return FakeDetector()

    monkeypatch.setattr(
        "smart_p1_sc.model_tool.create_detector", fake_create_detector
    )
    args = SimpleNamespace(
        model=str(model),
        image=str(image),
        class_map="3=spaghetti,4=spaghetti",
        class_count=9,
        imgsz=320,
        score_threshold=0.25,
        nms_iou_threshold=0.45,
    )
    result = check_image(args)
    assert result["candidate_count"] == 0
    assert seen["config"].class_count == 9
    assert seen["config"].class_map == {"3": "spaghetti", "4": "spaghetti"}


def test_check_image_rejects_class_map_beyond_class_count(tmp_path):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"model")
    image = tmp_path / "test.jpg"
    image.write_bytes(b"\xff\xd8x\xff\xd9")
    args = SimpleNamespace(
        model=str(model),
        image=str(image),
        class_map="3=spaghetti",
        class_count=3,
        imgsz=320,
        score_threshold=0.25,
        nms_iou_threshold=0.45,
    )
    with pytest.raises(ModelToolError) as error:
        check_image(args)
    assert error.value.code == "invalid_class_count"
