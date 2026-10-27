from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Deque, Dict, List, Optional, Protocol, Tuple

from .config import DetectionConfig
from .models import (
    Candidate,
    Detection,
    Frame,
    GcodeState,
    PrinterSnapshot,
    Severity,
    utcnow,
)

LOGGER = logging.getLogger(__name__)

SPAGHETTI = "spaghetti"
FIRST_LAYER = "first_layer_detachment"
STRUCTURAL = "structural_anomaly"
DETECTION_TYPES = (SPAGHETTI, FIRST_LAYER, STRUCTURAL)


class Detector(Protocol):
    def detect(
        self, frame: Frame, snapshot: Optional[PrinterSnapshot]
    ) -> List[Candidate]:
        ...


class NullDetector:
    def __init__(self) -> None:
        self._logged = False

    def detect(
        self, frame: Frame, snapshot: Optional[PrinterSnapshot]
    ) -> List[Candidate]:
        if not self._logged:
            self._logged = True
            LOGGER.warning("detector_not_configured: no visual candidates will be produced")
        return []


class CallableDetector:
    def __init__(
        self,
        fn: Callable[[Frame, Optional[PrinterSnapshot]], List[Candidate]],
    ) -> None:
        self._fn = fn

    def detect(
        self, frame: Frame, snapshot: Optional[PrinterSnapshot]
    ) -> List[Candidate]:
        return self._fn(frame, snapshot)


@dataclass
class Observation:
    candidate: Optional[Candidate]
    frame_seq: int
    monotonic_ts: float
    layer_num: Optional[int]


@dataclass
class Suppression:
    until: float
    bbox: Optional[Tuple[float, float, float, float]]
    area: float
    confidence: float


@dataclass
class FrameAnalysis:
    candidates: List[Candidate] = field(default_factory=list)
    detections: List[Detection] = field(default_factory=list)


def bbox_iou(
    a: Optional[Tuple[float, float, float, float]],
    b: Optional[Tuple[float, float, float, float]],
) -> float:
    if a is None or b is None:
        return 0.0
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    inter_x1 = max(min(ax1, ax2), min(bx1, bx2))
    inter_y1 = max(min(ay1, ay2), min(by1, by2))
    inter_x2 = min(max(ax1, ax2), max(bx1, bx2))
    inter_y2 = min(max(ay1, ay2), max(by1, by2))
    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    intersection = inter_w * inter_h
    area_a = abs(ax2 - ax1) * abs(ay2 - ay1)
    area_b = abs(bx2 - bx1) * abs(by2 - by1)
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def bbox_area(bbox: Optional[Tuple[float, float, float, float]]) -> float:
    if bbox is None:
        return 0.0
    x1, y1, x2, y2 = bbox
    return abs(x2 - x1) * abs(y2 - y1)


def bbox_area_ratio(
    bbox: Optional[Tuple[float, float, float, float]], frame: Optional[Frame]
) -> Optional[float]:
    if bbox is None:
        return None
    area = bbox_area(bbox)
    if frame is not None and frame.width and frame.height:
        x1, y1, x2, y2 = bbox
        if max(abs(x1), abs(x2), abs(y1), abs(y2)) > 1.5:
            return min(1.0, area / float(frame.width * frame.height))
    return min(1.0, area)


def has_hardware_fault(snapshot: Optional[PrinterSnapshot]) -> bool:
    if snapshot is None:
        return False
    if snapshot.print_error not in (None, 0, "0", "", False):
        return True
    hms = snapshot.hms
    if isinstance(hms, (list, dict)) and len(hms) > 0:
        return True
    return False


class DetectionConfirmer:
    def __init__(
        self,
        config: DetectionConfig,
        now_monotonic: Optional[Callable[[], float]] = None,
        wall_clock: Optional[Callable[[], datetime]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self.logger = logger or LOGGER
        self._now_monotonic = now_monotonic or time.monotonic
        self._wall_clock = wall_clock or utcnow
        self._lock = threading.RLock()
        self._windows: Dict[str, Deque[Observation]] = {
            kind: deque(maxlen=max(1, config.window_frames)) for kind in DETECTION_TYPES
        }
        self._suppressions: Dict[str, Suppression] = {}

    def observe(
        self,
        frame: Frame,
        snapshot: Optional[PrinterSnapshot],
        candidates: List[Candidate],
    ) -> List[Detection]:
        if snapshot is None or snapshot.gcode_state != GcodeState.RUNNING:
            self._clear_windows()
            return []
        if (
            snapshot.nozzle_temp is not None
            and snapshot.nozzle_temp < self.config.min_nozzle_temp_c
        ):
            self._clear_windows()
            return []
        if has_hardware_fault(snapshot):
            self._clear_windows()
            return []

        by_kind: Dict[str, Candidate] = {}
        for candidate in candidates:
            if candidate.kind not in DETECTION_TYPES:
                continue
            if candidate.confidence < self.config.candidate_threshold:
                continue
            if candidate.kind == FIRST_LAYER:
                if snapshot.layer_num is None or snapshot.layer_num > self.config.first_layer_max:
                    continue
            current = by_kind.get(candidate.kind)
            if current is None or candidate.confidence > current.confidence:
                by_kind[candidate.kind] = candidate

        now_mono = self._now_monotonic()
        detections: List[Detection] = []
        with self._lock:
            for kind in DETECTION_TYPES:
                self._windows[kind].append(
                    Observation(
                        candidate=by_kind.get(kind),
                        frame_seq=frame.seq,
                        monotonic_ts=now_mono,
                        layer_num=snapshot.layer_num,
                    )
                )
                candidate = by_kind.get(kind)
                if candidate is None:
                    continue
                detection = self._confirm(kind, frame, snapshot, now_mono)
                if detection is not None:
                    detections.append(detection)
        return detections

    def _confirm(
        self,
        kind: str,
        frame: Frame,
        snapshot: PrinterSnapshot,
        now_mono: float,
    ) -> Optional[Detection]:
        window = list(self._windows[kind])
        positives = [
            item
            for item in window
            if item.candidate is not None
            and item.candidate.confidence >= self.config.candidate_threshold
        ]
        if len(positives) < self.config.min_positive_frames:
            return None

        anchor = positives[-1].candidate
        if anchor is None or anchor.bbox is None:
            return None
        max_confidence = max(item.candidate.confidence for item in positives if item.candidate)
        if max_confidence < self.config.confirm_threshold:
            return None

        cluster = [
            item
            for item in positives
            if item.candidate is not None
            and bbox_iou(item.candidate.bbox, anchor.bbox)
            >= self.config.spatial_iou_threshold
        ]
        if len(cluster) < self.config.min_positive_frames:
            return None

        area_ratio = bbox_area_ratio(anchor.bbox, frame)
        suppression = self._suppressions.get(kind)
        if suppression is not None and now_mono < suppression.until:
            same_region = (
                bbox_iou(anchor.bbox, suppression.bbox)
                >= self.config.spatial_iou_threshold
            )
            if same_region:
                area_grew = (
                    area_ratio is not None
                    and area_ratio
                    > suppression.area * (1.0 + self.config.suppression_growth_ratio)
                )
                confidence_rose = (
                    max_confidence
                    > suppression.confidence + self.config.suppression_confidence_delta
                )
                if not area_grew and not confidence_rose:
                    self.logger.info(
                        "detection_suppressed",
                        extra={
                            "type": kind,
                            "bbox": anchor.bbox,
                            "remaining_seconds": round(suppression.until - now_mono, 1),
                        },
                    )
                    return None

        first_area = bbox_area(cluster[0].candidate.bbox) if cluster[0].candidate else 0.0
        last_area = bbox_area(cluster[-1].candidate.bbox) if cluster[-1].candidate else 0.0
        area_growth: Optional[float] = None
        if first_area > 0:
            area_growth = (last_area - first_area) / first_area

        severity = self._severity(kind, max_confidence, area_ratio)
        detection = Detection(
            kind=kind,
            severity=severity,
            confidence=max_confidence,
            bbox=anchor.bbox,
            frame_seq=frame.seq,
            evidence_frames=len(cluster),
            area_growth=area_growth,
            model_version=anchor.model_version,
            created_at=self._wall_clock(),
        )
        self._windows[kind].clear()
        self.logger.info(
            "detection_confirmed",
            extra={
                "type": kind,
                "severity": severity.value,
                "confidence": round(max_confidence, 3),
                "evidence_frames": len(cluster),
                "area_growth": area_growth,
                "layer_num": snapshot.layer_num,
            },
        )
        return detection

    def _severity(
        self, kind: str, confidence: float, area_ratio: Optional[float]
    ) -> Severity:
        if kind == STRUCTURAL:
            return Severity.P3
        if kind == FIRST_LAYER:
            if (
                confidence >= self.config.p1_threshold
                and area_ratio is not None
                and area_ratio >= self.config.first_layer_min_area_ratio
            ):
                return Severity.P1
        elif confidence >= self.config.p1_threshold:
            return Severity.P1
        if confidence >= self.config.p2_threshold:
            return Severity.P2
        return Severity.P3

    def suppress(
        self,
        kind: str,
        bbox: Optional[Tuple[float, float, float, float]],
        seconds: Optional[float] = None,
        confidence: float = 0.0,
        now_monotonic: Optional[float] = None,
    ) -> None:
        now_mono = self._now_monotonic() if now_monotonic is None else now_monotonic
        with self._lock:
            self._suppressions[kind] = Suppression(
                until=now_mono
                + (self.config.suppression_seconds if seconds is None else seconds),
                bbox=bbox,
                area=bbox_area(bbox),
                confidence=confidence,
            )
        self.logger.info(
            "detection_suppression_added",
            extra={"type": kind, "bbox": bbox, "seconds": seconds or self.config.suppression_seconds},
        )

    def clear_suppression(self, kind: Optional[str] = None) -> None:
        with self._lock:
            if kind is None:
                self._suppressions.clear()
            else:
                self._suppressions.pop(kind, None)

    def reset(self) -> None:
        with self._lock:
            self._clear_windows()
            self._suppressions.clear()

    def _clear_windows(self) -> None:
        with self._lock:
            for window in self._windows.values():
                window.clear()


class FrameAnalyzer:
    def __init__(
        self,
        detector: Detector,
        confirmer: DetectionConfirmer,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.detector = detector
        self.confirmer = confirmer
        self.logger = logger or LOGGER

    def process(
        self, frame: Frame, snapshot: Optional[PrinterSnapshot]
    ) -> FrameAnalysis:
        candidates = self.detector.detect(frame, snapshot)
        detections = self.confirmer.observe(frame, snapshot, candidates)
        if candidates:
            self.logger.debug(
                "detector_candidates",
                extra={
                    "frame_seq": frame.seq,
                    "count": len(candidates),
                    "types": [candidate.kind for candidate in candidates],
                },
            )
        return FrameAnalysis(candidates=candidates, detections=detections)
