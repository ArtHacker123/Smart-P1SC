from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import DetectionConfig
from .detector import DETECTION_TYPES
from .models import Frame
from .onnx_detector import DetectorError, create_detector

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_DEPENDENCY = 3
EXIT_FAILED = 4


class ModelToolError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = EXIT_FAILED) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def parse_class_names(value: str) -> List[str]:
    names = [item.strip() for item in value.split(",") if item.strip()]
    if not names:
        raise ModelToolError("invalid_class_names", "class names are empty", EXIT_INVALID)
    invalid = [name for name in names if name not in DETECTION_TYPES]
    if invalid:
        raise ModelToolError(
            "invalid_class_names",
            "unsupported class names: {}".format(", ".join(invalid)),
            EXIT_INVALID,
        )
    return names


def class_map_from_names(names: Sequence[str]) -> Dict[str, str]:
    return {str(index): name for index, name in enumerate(names)}


def parse_class_map(value: str) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ModelToolError(
                "invalid_class_map",
                "class map entries must use index=name: {}".format(item),
                EXIT_INVALID,
            )
        index, name = item.split("=", 1)
        index = index.strip()
        name = name.strip()
        if not index.isdigit():
            raise ModelToolError(
                "invalid_class_map",
                "class map index is not an integer: {}".format(index),
                EXIT_INVALID,
            )
        if name not in DETECTION_TYPES:
            raise ModelToolError(
                "invalid_class_map",
                "unsupported detection type: {}".format(name),
                EXIT_INVALID,
            )
        mapping[index] = name
    if not mapping:
        raise ModelToolError("invalid_class_map", "class map is empty", EXIT_INVALID)
    return mapping


def recommend_output(
    shape: Sequence[Any],
) -> Dict[str, Any]:
    dimensions = _static_dimensions(shape)
    if dimensions is None:
        raise ModelToolError(
            "dynamic_output",
            "output shape contains dynamic dimensions: {}".format(tuple(shape)),
        )
    matrix = list(dimensions)
    while len(matrix) > 2 and matrix[0] == 1:
        matrix.pop(0)
    if len(matrix) == 3 and matrix[0] == 1:
        matrix.pop(0)
    if len(matrix) != 2:
        raise ModelToolError(
            "unsupported_output",
            "unsupported output shape: {}".format(tuple(shape)),
        )
    rows, columns = matrix
    if columns == 6:
        return {
            "class_count": None,
            "output_format": "xyxy_score_class",
            "box_coordinates": "pixels",
            "transposed": False,
        }
    if rows > columns and columns > 5:
        class_count = columns - 5
        if 0 < class_count <= 1000:
            return {
                "class_count": class_count,
                "output_format": "cxcywh_object_class",
                "box_coordinates": "pixels",
                "transposed": False,
            }
    if columns > rows and rows > 4:
        class_count = rows - 4
        if 0 < class_count <= 1000:
            return {
                "class_count": class_count,
                "output_format": "cxcywh_class",
                "box_coordinates": "pixels",
                "transposed": False,
            }
    if rows == columns and rows > 5:
        return {
            "class_count": rows - 4,
            "output_format": "cxcywh_class",
            "box_coordinates": "pixels",
            "transposed": False,
        }
    raise ModelToolError(
        "unsupported_output",
        "cannot infer output format for shape {}".format(tuple(shape)),
    )


def _static_dimensions(shape: Sequence[Any]) -> Optional[List[int]]:
    dimensions: List[int] = []
    for value in shape:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        if number <= 0:
            return None
        dimensions.append(number)
    return dimensions


def metadata_path(model_path: Path) -> Path:
    return Path(str(model_path) + ".json")


def write_metadata(
    model_path: Path,
    names: Sequence[str],
    model_version: str,
    imgsz: int,
    source_weights: Optional[str],
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    path = metadata_path(model_path)
    payload: Dict[str, Any] = {
        "model_version": model_version,
        "model_path": str(model_path),
        "input_width": imgsz,
        "input_height": imgsz,
        "class_count": len(names),
        "class_map": class_map_from_names(names),
        "class_names": list(names),
        "input_layout": "nchw",
        "channel_order": "rgb",
        "normalize": True,
        "output_format": "auto",
        "box_coordinates": "pixels",
        "nms_iou_threshold": 0.45,
        "providers": ["CPUExecutionProvider"],
        "source_weights": source_weights,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        payload.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def read_metadata(model_path: Path) -> Optional[Dict[str, Any]]:
    path = metadata_path(model_path)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _load_ultralytics() -> Any:
    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise ModelToolError(
            "missing_training_dependency",
            "ultralytics is not installed; run: python -m pip install -e .[training]",
            EXIT_DEPENDENCY,
        ) from exc
    return YOLO


def _load_onnxruntime() -> Any:
    try:
        import onnxruntime
    except ImportError as exc:
        raise ModelToolError(
            "missing_runtime_dependency",
            "onnxruntime is not installed; run: python -m pip install -e .[vision]",
            EXIT_DEPENDENCY,
        ) from exc
    return onnxruntime


def _model_class_count(model: Any) -> Optional[int]:
    names = getattr(model, "names", None)
    if isinstance(names, dict):
        return len(names)
    if isinstance(names, (list, tuple)):
        return len(names)
    count = getattr(getattr(model, "model", None), "nc", None)
    if count is None:
        return None
    try:
        return int(count)
    except (TypeError, ValueError):
        return None


def _dataset_class_names(payload: Dict[str, Any]) -> Optional[List[str]]:
    names = payload.get("names")
    if isinstance(names, dict):
        try:
            keys = sorted(names, key=lambda key: int(key))
        except (TypeError, ValueError):
            keys = list(names)
        return [str(names[key]) for key in keys]
    if isinstance(names, (list, tuple)):
        return [str(name) for name in names]
    return None


def _normalize_dataset(data: Path) -> Tuple[Path, Any, Optional[List[str]]]:
    try:
        import yaml
    except ImportError as exc:
        raise ModelToolError(
            "missing_training_dependency",
            "PyYAML is not installed; run: python -m pip install -e .[training]",
            EXIT_DEPENDENCY,
        ) from exc
    try:
        payload = yaml.safe_load(data.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ModelToolError(
            "invalid_dataset",
            "cannot read dataset YAML: {}".format(exc),
            EXIT_INVALID,
        ) from exc
    if not isinstance(payload, dict):
        raise ModelToolError(
            "invalid_dataset",
            "dataset YAML must contain a mapping",
            EXIT_INVALID,
        )
    root = payload.get("path")
    if root is None:
        root_path = data.parent.resolve()
    else:
        root_path = Path(str(root))
        if not root_path.is_absolute():
            local_path = (data.parent / root_path).resolve()
            root_path = local_path if local_path.exists() else (Path.cwd() / root_path).resolve()
    payload["path"] = str(root_path)
    temp_dir = tempfile.TemporaryDirectory(prefix="smart-p1sc-dataset-")
    normalized = Path(temp_dir.name) / "dataset.yaml"
    normalized.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return normalized, temp_dir, _dataset_class_names(payload)


def export_onnx(
    weights: Path,
    output: Path,
    imgsz: int,
    opset: int,
    names: Sequence[str],
    model_version: str,
) -> Dict[str, Any]:
    weights = weights.expanduser().absolute()
    output = output.expanduser().absolute()
    if not weights.is_file():
        raise ModelToolError(
            "weights_not_found",
            "weights not found: {}".format(weights),
            EXIT_INVALID,
        )
    YOLO = _load_ultralytics()
    model = YOLO(str(weights))
    class_count = _model_class_count(model)
    if class_count is not None and class_count != len(names):
        raise ModelToolError(
            "class_count_mismatch",
            "model has {} classes but {} class names were supplied".format(
                class_count, len(names)
            ),
            EXIT_INVALID,
        )
    exported = Path(
        model.export(
            format="onnx",
            imgsz=imgsz,
            opset=opset,
            simplify=False,
            dynamic=False,
            batch=1,
            nms=False,
        )
    )
    if not exported.is_file():
        raise ModelToolError(
            "export_failed",
            "ultralytics did not produce an ONNX file",
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    if exported.resolve() != output.resolve():
        shutil.copy2(str(exported), str(output))
        if weights.suffix.lower() in (".yaml", ".yml"):
            exported.unlink()
    sidecar = write_metadata(
        output,
        names,
        model_version,
        imgsz,
        str(weights),
        {"opset": opset, "nms": False, "dynamic": False, "simplify": False},
    )
    return {
        "code": "exported",
        "model_path": str(output),
        "metadata_path": str(sidecar),
        "bytes": output.stat().st_size,
    }


def train_and_export(args: argparse.Namespace) -> Dict[str, Any]:
    data = Path(args.data)
    if not data.is_file():
        raise ModelToolError(
            "dataset_not_found",
            "dataset YAML not found: {}".format(data),
            EXIT_INVALID,
        )
    names = parse_class_names(args.class_names)
    YOLO = _load_ultralytics()
    model = YOLO(args.model)
    normalized_data, temp_dir, dataset_names = _normalize_dataset(data)
    try:
        if dataset_names is not None and dataset_names != list(names):
            raise ModelToolError(
                "class_names_mismatch",
                "dataset classes {} do not match --class-names {}".format(
                    dataset_names, list(names)
                ),
                EXIT_INVALID,
            )
        model.train(
            data=str(normalized_data),
            epochs=args.epochs,
            imgsz=args.imgsz,
            batch=args.batch,
            device=args.device,
            project=str(Path(args.project).expanduser().absolute()),
            name=args.name,
            exist_ok=True,
        )
    finally:
        temp_dir.cleanup()
    trainer = getattr(model, "trainer", None)
    best = Path(str(getattr(trainer, "best", ""))) if trainer is not None else Path()
    if not best.is_file():
        raise ModelToolError(
            "training_failed",
            "training finished but best weights were not found",
        )
    if args.no_export:
        return {
            "code": "trained",
            "weights": str(best),
            "exported": False,
        }
    result = export_onnx(
        best,
        Path(args.output),
        args.imgsz,
        args.opset,
        names,
        args.model_version,
    )
    result["code"] = "trained"
    result["weights"] = str(best)
    return result


def inspect_model(model_path: Path) -> Dict[str, Any]:
    if not model_path.is_file():
        raise ModelToolError(
            "model_not_found",
            "model not found: {}".format(model_path),
            EXIT_INVALID,
        )
    runtime = _load_onnxruntime()
    session = runtime.InferenceSession(
        str(model_path), providers=["CPUExecutionProvider"]
    )
    inputs = [
        {"name": item.name, "shape": list(item.shape), "type": item.type}
        for item in session.get_inputs()
    ]
    outputs = [
        {"name": item.name, "shape": list(item.shape), "type": item.type}
        for item in session.get_outputs()
    ]
    if len(inputs) != 1 or len(outputs) != 1:
        raise ModelToolError(
            "unsupported_model",
            "expected one input and one output, got {} input(s) and {} output(s)".format(
                len(inputs), len(outputs)
            ),
        )
    input_shape = inputs[0]["shape"]
    if len(input_shape) != 4:
        raise ModelToolError(
            "unsupported_model",
            "expected a 4D input, got {}".format(tuple(input_shape)),
        )
    layout = "nchw"
    height_index, width_index = 2, 3
    if int(input_shape[3]) == 3:
        layout = "nhwc"
        height_index, width_index = 1, 2
    height = input_shape[height_index]
    width = input_shape[width_index]
    if not isinstance(height, int) or not isinstance(width, int) or height <= 0 or width <= 0:
        raise ModelToolError(
            "dynamic_input",
            "input height/width must be static: {}".format(tuple(input_shape)),
        )
    recommendation = recommend_output(outputs[0]["shape"])
    result: Dict[str, Any] = {
        "code": "inspected",
        "model_path": str(model_path),
        "inputs": inputs,
        "outputs": outputs,
        "recommended": {
            "input_width": width,
            "input_height": height,
            "input_layout": layout,
            "channel_order": "rgb",
            "normalize": True,
            "output_format": recommendation["output_format"],
            "box_coordinates": recommendation["box_coordinates"],
            "providers": ["CPUExecutionProvider"],
        },
    }
    if recommendation["class_count"] is not None:
        result["recommended"]["class_count"] = recommendation["class_count"]
    metadata = read_metadata(model_path)
    if metadata:
        result["metadata"] = metadata
    return result


def check_image(args: argparse.Namespace) -> Dict[str, Any]:
    model_path = Path(args.model)
    image_path = Path(args.image)
    if not image_path.is_file():
        raise ModelToolError(
            "image_not_found",
            "image not found: {}".format(image_path),
            EXIT_INVALID,
        )
    metadata = read_metadata(model_path)
    if args.class_map:
        class_map = parse_class_map(args.class_map)
    elif metadata and isinstance(metadata.get("class_map"), dict):
        class_map = {
            str(key): str(value) for key, value in metadata["class_map"].items()
        }
    else:
        raise ModelToolError(
            "missing_class_map",
            "class map is missing; pass --class-map 0=spaghetti,1=first_layer_detachment",
            EXIT_INVALID,
        )
    imgsz = args.imgsz
    if imgsz <= 0:
        imgsz = int((metadata or {}).get("input_width") or 640)
    config = DetectionConfig(
        backend="onnx",
        model_path=str(model_path),
        input_width=imgsz,
        input_height=imgsz,
        input_layout=str((metadata or {}).get("input_layout") or "nchw"),
        channel_order=str((metadata or {}).get("channel_order") or "rgb"),
        normalize=True,
        output_format="auto",
        class_count=max(int(key) for key in class_map) + 1,
        class_map=class_map,
        score_threshold=args.score_threshold,
        nms_iou_threshold=args.nms_iou_threshold,
        box_coordinates=str((metadata or {}).get("box_coordinates") or "pixels"),
        providers=["CPUExecutionProvider"],
        model_version=str((metadata or {}).get("model_version") or model_path.stem),
    )
    detector = create_detector(config)
    frame = Frame(
        seq=1,
        monotonic_ts=time.monotonic(),
        wall_ts=datetime.now(timezone.utc),
        jpeg=image_path.read_bytes(),
        width=0,
        height=0,
    )
    started = time.perf_counter()
    candidates = detector.detect(frame, None)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return {
        "code": "checked",
        "model_path": str(model_path),
        "image_path": str(image_path),
        "elapsed_ms": round(elapsed_ms, 3),
        "candidate_count": len(candidates),
        "candidates": [
            {
                "kind": item.kind,
                "confidence": item.confidence,
                "bbox": list(item.bbox),
                "model_version": item.model_version,
            }
            for item in candidates
        ],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smart-p1sc-model",
        description="Train, export and inspect YOLO models for Smart P1SC",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print stable JSON")

    train = sub.add_parser("train", parents=[common], help="train and export a YOLO model")
    train.add_argument("--data", required=True, help="YOLO dataset YAML")
    train.add_argument("--model", default="yolov8n.pt", help="base weights or model YAML")
    train.add_argument("--epochs", type=int, default=100)
    train.add_argument("--imgsz", type=int, default=640)
    train.add_argument("--batch", type=int, default=8)
    train.add_argument("--device", default="cpu")
    train.add_argument("--project", default="runs/train")
    train.add_argument("--name", default="spaghetti")
    train.add_argument("--output", default="models/spaghetti.onnx")
    train.add_argument("--model-version", default="spaghetti-v1")
    train.add_argument("--opset", type=int, default=12)
    train.add_argument(
        "--class-names",
        default="spaghetti,first_layer_detachment,structural_anomaly",
    )
    train.add_argument("--no-export", action="store_true")

    export = sub.add_parser("export", parents=[common], help="export existing weights to ONNX")
    export.add_argument("--weights", required=True, help="PyTorch weights (best.pt)")
    export.add_argument("--output", default="models/spaghetti.onnx")
    export.add_argument("--imgsz", type=int, default=640)
    export.add_argument("--opset", type=int, default=12)
    export.add_argument("--model-version", default="spaghetti-v1")
    export.add_argument(
        "--class-names",
        default="spaghetti,first_layer_detachment,structural_anomaly",
    )

    inspect = sub.add_parser("inspect", parents=[common], help="inspect ONNX input/output")
    inspect.add_argument("--model", required=True)

    check = sub.add_parser("check", parents=[common], help="run ONNX inference on one image")
    check.add_argument("--model", required=True)
    check.add_argument("--image", required=True)
    check.add_argument("--imgsz", type=int, default=0)
    check.add_argument("--class-map", default="")
    check.add_argument("--score-threshold", type=float, default=0.25)
    check.add_argument("--nms-iou-threshold", type=float, default=0.45)
    return parser


def _print_result(result: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    code = result.get("code")
    if code in ("trained", "exported"):
        print("weights: {}".format(result.get("weights", "?")))
        print("model: {}".format(result.get("model_path", "?")))
        print("metadata: {}".format(result.get("metadata_path", "?")))
    elif code == "inspected":
        for item in result.get("inputs", []):
            print("input: {} {} {}".format(item["name"], item["shape"], item["type"]))
        for item in result.get("outputs", []):
            print("output: {} {} {}".format(item["name"], item["shape"], item["type"]))
        print("recommended: {}".format(json.dumps(result["recommended"], ensure_ascii=False)))
    elif code == "checked":
        print("candidates: {}".format(result.get("candidate_count")))
        print("elapsed_ms: {}".format(result.get("elapsed_ms")))
        for item in result.get("candidates", []):
            print(
                "{} conf={:.3f} bbox={}".format(
                    item["kind"],
                    float(item["confidence"]),
                    item["bbox"],
                )
            )
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "train":
            result = train_and_export(args)
        elif args.command == "export":
            result = export_onnx(
                Path(args.weights),
                Path(args.output),
                args.imgsz,
                args.opset,
                parse_class_names(args.class_names),
                args.model_version,
            )
        elif args.command == "inspect":
            result = inspect_model(Path(args.model))
        elif args.command == "check":
            result = check_image(args)
        else:
            parser.error("unknown command: {}".format(args.command))
            return EXIT_INVALID
    except ModelToolError as exc:
        print("error: {}: {}".format(exc.code, exc.message), file=sys.stderr)
        return exc.exit_code
    except DetectorError as exc:
        print("error: detector: {}".format(exc), file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        return 130
    _print_result(result, bool(args.json))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
