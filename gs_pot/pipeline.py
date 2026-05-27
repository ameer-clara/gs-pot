"""End-to-end pipeline orchestrator: images → poses → train → thumbnail → READY."""

import logging
from pathlib import Path
from typing import Any

from .models import ScanInfo, ScanStatus
from .poses import Quality, run_colmap
from .store import get_store
from .thumb import make_thumbnail
from .train import run_brush

log = logging.getLogger(__name__)


def _patch(scan_id: str, **changes: Any) -> None:
    store = get_store()
    info = store.get(scan_id)
    if info is None:
        return
    store.put(info.model_copy(update=changes))


def run_scan(
    *,
    scan_id: str,
    images_dir: Path,
    scenes_dir: Path,
    steps: int = 7000,
    quality: Quality = "medium",
) -> ScanInfo:
    """Run the full pipeline. Mutates the scan in the store as we progress.

    Raises on failure (and writes status=error to the store before re-raising).
    """
    workspace = scenes_dir / scan_id
    try:
        _patch(scan_id, status=ScanStatus.POSES, progress=0.1)
        log.info("[%s] colmap start: images=%s workspace=%s", scan_id, images_dir, workspace)
        run_colmap(workspace, images_dir, quality=quality)

        _patch(scan_id, status=ScanStatus.TRAINING, progress=0.4)
        log.info("[%s] brush start: steps=%d", scan_id, steps)
        run_brush(workspace, workspace, steps=steps, export_name="scene.ply")

        make_thumbnail(images_dir, workspace / "thumb.jpg")

        _patch(
            scan_id,
            status=ScanStatus.READY,
            progress=1.0,
            scene_url=f"/scenes/{scan_id}.ply",
            thumb_url=f"/scenes/{scan_id}/thumb.jpg",
        )
        log.info("[%s] DONE", scan_id)
    except Exception as exc:
        log.exception("[%s] pipeline failed", scan_id)
        _patch(scan_id, status=ScanStatus.ERROR, error=str(exc))
        raise

    info = get_store().get(scan_id)
    assert info is not None  # we just patched it
    return info
