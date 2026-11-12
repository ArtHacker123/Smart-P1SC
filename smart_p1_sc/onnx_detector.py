from __future__ import annotations

import io
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import ConfigError, DetectionConfig
from .detector import DETECTION_TYPES, NullDetector
from .models import Candidate, Frame, PrinterSnapshot

LOGGER = logging.getLogger(__name__)

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore


class DetectorError(ConfigError):
    pass


def _require_vision_dependencies() -> None:
    if np is None:
        raise DetectorError("numpy is required for ONNX detection")
    if Image is None:
        raise DetectorError("Pillow is required for ONNX detection")


def _load_onnxruntime() -> Any:
    try:
        import onnxruntime
    except ImportError:
        return None
    return onnxruntime


def create_detector(config: DetectionConfig) -> Any:
    backend = (config.backend or "null").lower()
    if backend == "null":
        return NullDetector()
    if backend not in ("auto", "onnx"):
        raise DetectorError("unsupported detection backend: {}".format(config.backend))

    model_path = Path(config.model_path)
    runtime = _load_onnxruntime()
    if not model_path.is_file():
        message = "detector model not found: {}".format(model_path)
        if backend == "auto":
            LOGGER.warning("detector_model_missing", extra={"path": str(model_path)})
            return NullDetector()
        raise DetectorError(message)
    if runtime is None:
        message = "onnxruntime is not installed"
        if backend == "auto":
            LOGGER.warning("detector_runtime_missing")
            return NullDetector()
        raise DetectorError(message)
    if not config.class_map:
        message = "detection.class_map is empty"
        if backend == "auto":
            LOGGER.warning("detector_class_map_empty")
            return NullDetector()
        raise DetectorError(message)

    _require_vision_dependencies()
    session = runtime.InferenceSession(
        str(model_path), providers=list(config.providers)
    )
    detector = OnnxDetector(session, config)
    LOGGER.info(
        "onnx_detector_loaded",
        extra={
            "model_path": str(model_path),
            "model_version": config.model_version,
            "providers": list(config.providers),
        },
    )
    return detector


class OnnxDetector:
    def __init__(self, session: Any, config: DetectionConfig) -> None:
        _require_vision_dependencies()
        self.session = session
        self.config = config
        self.class_map = _class_map(config)
        if not self.class_map:
            raise DetectorError("detection.class_map is empty")
        self.input = session.get_inputs()[0]
        self.input_name = self.input.name
        self.output_name = session.get_outputs()[0].name
        self.input_width, self.input_height = _input_size(self.input.shape, config)
        self._last_error_at: Optional[float] = None

    def detect(
        self, frame: Frame, snapshot: Optional[PrinterSnapshot]
    ) -> List[Candidate]:
        if not frame.jpeg:
            return []
        tensor = self.preprocess(frame.jpeg)
        try:
            outputs = self.session.run([self.output_name], {self.input_name: tensor})
        except Exception:
            now = time.monotonic()
            if self._last_error_at is None or now - self._last_error_at >= 60.0:
                LOGGER.exception("detector_inference_failed")
                self._last_error_at = now
            return []
        self._last_error_at = None
        return decode_output(
            outputs,
            self.config,
            frame.seq,
            self.class_map,
            input_width=self.input_width,
            input_height=self.input_height,
        )

    def preprocess(self, jpeg: bytes) -> Any:
        image = Image.open(io.BytesIO(jpeg)).convert("RGB")
        image = image.resize((self.input_width, self.input_height), Image.BILINEAR)
        array = np.asarray(image, dtype=np.float32)
        if self.config.normalize:
            array = array / 255.0
        if (self.config.channel_order or "rgb").lower() == "bgr":
            array = array[:, :, ::-1]
        if (self.config.input_layout or "nchw").lower() == "nchw":
            array = np.transpose(array, (2, 0, 1))
        return np.expand_dims(array, 0)


def decode_output(
    outputs: Any,
    config: DetectionConfig,
    frame_seq: int,
    class_map: Optional[Dict[int, str]] = None,
    input_width: Optional[int] = None,
    input_height: Optional[int] = None,
) -> List[Candidate]:
    _require_vision_dependencies()
    if isinstance(outputs, (list, tuple)):
        if not outputs:
            return []
        output = outputs[0]
    else:
        output = outputs
    mapping = class_map if class_map is not None else _class_map(config)
    matrix, output_format = _prepare_matrix(output, config)
    if matrix.size == 0:
        return []

    class_count = _class_count(config, mapping)
    if output_format == "xyxy_score_class":
        boxes = matrix[:, :4]
        scores = matrix[:, 4]
        class_ids = matrix[:, 5].astype(np.int32)
    elif output_format == "cxcywh_object_class":
        boxes = matrix[:, :4]
        objectness = matrix[:, 4]
        class_scores = matrix[:, 5 : 5 + class_count]
        if class_scores.size == 0:
            return []
        class_ids = class_scores.argmax(axis=1).astype(np.int32)
        scores = objectness * class_scores[
            np.arange(class_scores.shape[0]), class_ids
        ]
    elif output_format == "cxcywh_class":
        boxes = matrix[:, :4]
        class_scores = matrix[:, 4 : 4 + class_count]
        if class_scores.size == 0:
            return []
        class_ids = class_scores.argmax(axis=1).astype(np.int32)
        scores = class_scores[np.arange(class_scores.shape[0]), class_ids]
    else:
        raise DetectorError("unsupported output format: {}".format(output_format))

    keep = scores >= float(config.score_threshold)
    boxes = boxes[keep]
    scores = scores[keep]
    class_ids = class_ids[keep]
    if boxes.size == 0:
        return []

    boxes = _normalize_boxes(
        boxes,
        config,
        output_format,
        int(input_width or config.input_width),
        int(input_height or config.input_height),
    )
    valid = (boxes[:, 2] > boxes[:, 0]) & (boxes[:, 3] > boxes[:, 1])
    boxes = boxes[valid]
    scores = scores[valid]
    class_ids = class_ids[valid]
    if boxes.size == 0:
        return []

    keep_indices = _nms(
        boxes,
        scores,
        class_ids,
        float(config.nms_iou_threshold),
        bool(config.class_agnostic_nms),
    )
    max_detections = max(0, int(config.max_detections))
    keep_indices = keep_indices[:max_detections]

    candidates: List[Candidate] = []
    for index in keep_indices:
        kind = mapping.get(int(class_ids[index]))
        if kind is None:
            continue
        bbox = tuple(float(value) for value in boxes[index])
        candidates.append(
            Candidate(
                kind=kind,
                confidence=float(scores[index]),
                bbox=bbox,
                frame_seq=frame_seq,
                model_version=config.model_version,
            )
        )
    return candidates


def _class_map(config: DetectionConfig) -> Dict[int, str]:
    mapping: Dict[int, str] = {}
    for key, value in (config.class_map or {}).items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            raise DetectorError(
                "class_map key is not an integer: {}".format(key)
            ) from None
        if index < 0:
            raise DetectorError("class_map key must be non-negative: {}".format(key))
        kind = str(value)
        if kind not in DETECTION_TYPES:
            raise DetectorError("unsupported detection type in class_map: {}".format(kind))
        mapping[index] = kind
    return mapping


def _input_size(shape: Sequence[Any], config: DetectionConfig) -> Tuple[int, int]:
    width = max(1, int(config.input_width))
    height = max(1, int(config.input_height))
    if len(shape) != 4:
        return width, height
    layout = (config.input_layout or "nchw").lower()
    if layout == "nchw":
        height = _positive_dim(shape[2], height)
        width = _positive_dim(shape[3], width)
    else:
        height = _positive_dim(shape[1], height)
        width = _positive_dim(shape[2], width)
    return width, height


def _positive_dim(value: Any, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


def _prepare_matrix(output: Any, config: DetectionConfig) -> Tuple[Any, str]:
    matrix = np.asarray(output)
    while matrix.ndim > 2 and matrix.shape[0] == 1:
        matrix = matrix[0]
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    if matrix.ndim != 2:
        raise DetectorError(
            "unsupported detector output shape: {}".format(tuple(matrix.shape))
        )

    output_format = (config.output_format or "auto").lower()
    class_count = _class_count(config, _class_map(config))
    matrix = _orient_matrix(matrix, output_format, class_count)
    if output_format != "auto":
        return matrix, output_format

    columns = matrix.shape[1]
    if columns == 6 and _looks_like_xyxy_score_class(matrix):
        return matrix, "xyxy_score_class"
    if class_count > 0:
        if columns == 5 + class_count:
            return matrix, "cxcywh_object_class"
        if columns == 4 + class_count:
            return matrix, "cxcywh_class"
        if matrix.shape[0] == 5 + class_count and columns != 5 + class_count:
            return matrix.T, "cxcywh_object_class"
        if matrix.shape[0] == 4 + class_count and columns != 4 + class_count:
            return matrix.T, "cxcywh_class"
    raise DetectorError(
        "cannot infer detector output format for shape {}; set detection.output_format".format(
            tuple(matrix.shape)
        )
    )


def _orient_matrix(matrix: Any, output_format: str, class_count: int) -> Any:
    if output_format == "xyxy_score_class":
        expected_columns = 6
    elif output_format == "cxcywh_object_class" and class_count > 0:
        expected_columns = 5 + class_count
    elif output_format == "cxcywh_class" and class_count > 0:
        expected_columns = 4 + class_count
    else:
        expected_columns = None

    if (
        expected_columns is not None
        and matrix.shape[1] != expected_columns
        and matrix.shape[0] == expected_columns
    ):
        return matrix.T
    return matrix


def _looks_like_xyxy_score_class(matrix: Any) -> bool:
    if matrix.shape[1] != 6:
        return False
    classes = matrix[:, 5]
    return bool(np.all(np.abs(classes - np.round(classes)) < 1e-3))


def _class_count(config: DetectionConfig, mapping: Dict[int, str]) -> int:
    if int(config.class_count or 0) > 0:
        return int(config.class_count)
    if not mapping:
        return 0
    return max(mapping) + 1


def _normalize_boxes(
    boxes: Any,
    config: DetectionConfig,
    output_format: str,
    input_width: int,
    input_height: int,
) -> Any:
    boxes = np.asarray(boxes, dtype=np.float32).copy()
    coordinates = (config.box_coordinates or "auto").lower()
    if coordinates == "auto":
        coordinates = "pixels" if float(np.max(boxes)) > 1.5 else "normalized"

    if output_format == "cxcywh_object_class" or output_format == "cxcywh_class":
        if coordinates == "pixels":
            boxes[:, 0] /= float(input_width)
            boxes[:, 1] /= float(input_height)
            boxes[:, 2] /= float(input_width)
            boxes[:, 3] /= float(input_height)
        cx, cy, width, height = (
            boxes[:, 0].copy(),
            boxes[:, 1].copy(),
            boxes[:, 2].copy(),
            boxes[:, 3].copy(),
        )
        boxes[:, 0] = cx - width / 2.0
        boxes[:, 1] = cy - height / 2.0
        boxes[:, 2] = cx + width / 2.0
        boxes[:, 3] = cy + height / 2.0
    elif coordinates == "pixels":
        boxes[:, 0] /= float(input_width)
        boxes[:, 2] /= float(input_width)
        boxes[:, 1] /= float(input_height)
        boxes[:, 3] /= float(input_height)

    boxes[:, 0:4:2] = np.clip(boxes[:, 0:4:2], 0.0, 1.0)
    boxes[:, 1:4:2] = np.clip(boxes[:, 1:4:2], 0.0, 1.0)
    return boxes


def _nms(
    boxes: Any,
    scores: Any,
    class_ids: Any,
    iou_threshold: float,
    class_agnostic: bool,
) -> List[int]:
    order = scores.argsort()[::-1]
    keep: List[int] = []
    while order.size > 0:
        current = int(order[0])
        keep.append(current)
        if order.size == 1:
            break
        remaining = order[1:]
        intersections = _box_iou(boxes[current], boxes[remaining])
        if class_agnostic:
            suppress = intersections > iou_threshold
        else:
            same_class = class_ids[remaining] == class_ids[current]
            suppress = (intersections > iou_threshold) & same_class
        order = remaining[~suppress]
    return keep


def _box_iou(box: Any, boxes: Any) -> Any:
    x1 = np.maximum(box[0], boxes[:, 0])
    y1 = np.maximum(box[1], boxes[:, 1])
    x2 = np.minimum(box[2], boxes[:, 2])
    y2 = np.minimum(box[3], boxes[:, 3])
    inter = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    area = max(0.0, float(box[2] - box[0])) * max(0.0, float(box[3] - box[1]))
    areas = np.maximum(0.0, boxes[:, 2] - boxes[:, 0]) * np.maximum(
        0.0, boxes[:, 3] - boxes[:, 1]
    )
    union = area + areas - inter
    return np.where(union > 0, inter / union, 0.0)
