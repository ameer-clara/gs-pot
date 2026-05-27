"""End-to-end pipeline orchestrator: images → poses → train → push → thumb → READY.

The optional **push** step uploads the trained `.ply` to robohack's
`/api/robot/splat` ingest endpoint (see `ingest.py` for the contract). It only
runs when both `GS_POT_INGEST_URL` and `GS_POT_INGEST_TOKEN` are set; otherwise
the scan still completes locally and serves via our own `/scenes/<id>.ply`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .ingest import push_splat
from .models import ScanInfo, ScanStatus
from .poses import Quality, run_colmap
from .store import get_property_store, get_store
from .thumb import make_thumbnail
from .train import run_brush

log = logging.getLogger(__name__)


def _patch(scan_id: str, **changes: Any) -> None:
    store = get_store()
    info = store.get(scan_id)
    if info is None:
        return
    store.put(info.model_copy(update=changes))


def _push_label(info: ScanInfo) -> str:
    """`<property name> · <scene name>` if the property exists, else just scene."""
    prop = get_property_store().get(info.property_id)
    if prop is not None:
        return f"{prop.name} · {info.scene_name}"
    return info.scene_name


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
        ply = run_brush(workspace, workspace, steps=steps, export_name="scene.ply")

        # Optional: push to robohack's ingest endpoint if both env vars are set.
        ingest_url = os.environ.get("GS_POT_INGEST_URL")
        ingest_token = os.environ.get("GS_POT_INGEST_TOKEN")
        if ingest_url and ingest_token:
            _patch(scan_id, status=ScanStatus.PUSHING, progress=0.85)
            info = get_store().get(scan_id)
            label = _push_label(info) if info else None
            log.info("[%s] pushing to %s (label=%s)", scan_id, ingest_url, label)
            result = push_splat(
                ply,
                ingest_url=ingest_url,
                token=ingest_token,
                name=label,
            )
            _patch(
                scan_id,
                ingest_id=result.get("id"),
                ingest_key=result.get("key"),
            )

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
