from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Iterable, List, Optional

from .models import Frame

LOGGER = logging.getLogger(__name__)


class FilmstripStore:
    def __init__(self, media_dir: str) -> None:
        self.media_dir = Path(media_dir)

    def save_frames(
        self,
        incident_id: str,
        frames: Iterable[Frame],
        phase: str = "pre",
    ) -> str:
        target = self.media_dir / incident_id
        target.mkdir(parents=True, exist_ok=True)
        written = 0
        for index, frame in enumerate(frames):
            name = "{}_{:04d}_seq{:06d}.jpg".format(phase, index, frame.seq)
            (target / name).write_bytes(frame.jpeg)
            written += 1
        self._write_manifest(target, incident_id, written)
        LOGGER.info(
            "filmstrip_saved",
            extra={"incident_id": incident_id, "frames": written, "path": str(target)},
        )
        return str(target)

    def append_frame(self, incident_id: str, frame: Frame, phase: str = "post") -> str:
        target = self.media_dir / incident_id
        target.mkdir(parents=True, exist_ok=True)
        name = "{}_{:06d}.jpg".format(phase, frame.seq)
        path = target / name
        path.write_bytes(frame.jpeg)
        return str(path)

    def cleanup_incident(self, incident_id: str) -> None:
        target = self.media_dir / incident_id
        if target.exists():
            shutil.rmtree(target)
            LOGGER.info("filmstrip_removed", extra={"incident_id": incident_id})

    def list_incident_frames(self, incident_id: str) -> List[str]:
        target = self.media_dir / incident_id
        if not target.exists():
            return []
        return sorted(str(path) for path in target.glob("*.jpg"))

    def _write_manifest(self, target: Path, incident_id: str, frame_count: int) -> None:
        manifest = target / "manifest.txt"
        manifest.write_text(
            "incident_id={}\nframes={}\n".format(incident_id, frame_count),
            encoding="utf-8",
        )


def save_filmstrip(
    media_dir: str,
    incident_id: str,
    frames: Iterable[Frame],
    phase: str = "pre",
) -> Optional[str]:
    if not frames:
        return None
    return FilmstripStore(media_dir).save_frames(incident_id, frames, phase)
