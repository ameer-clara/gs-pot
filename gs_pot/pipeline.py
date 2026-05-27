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

import numpy as np

from .ingest import push_splat
from .lidar_poses import (
    CameraIntrinsics,
    FramePose,
    write_colmap_workspace,
)
from .models import PoseMode, ScanInfo, ScanStatus
from .poses import Quality, run_colmap
from .runs import fetch_pointcloud, fetch_poses, list_frame_filenames
from .store import get_property_store, get_store
from .thumb import make_thumbnail
from .train import TrainerName, run_trainer

log = logging.getLogger(__name__)

# Default Go2 RGB intrinsics — placeholder until issue #4 lands the real
# checkerboard-calibrated constants in `gs_pot/go2_calib.py`. Conservative
# defaults: 640×360 (the gs-pot scan-go2 preset's max_image_size for the
# Go2's native resolution), 60° HFOV → fx ≈ width / (2*tan(30°)) ≈ 554.
# Wrong intrinsics warp the splat; replace with real calibration ASAP.
_DEFAULT_GO2_INTRINSICS = CameraIntrinsics(
    fx=554.26, fy=554.26, cx=320.0, cy=180.0, width=640, height=360
)


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


def _set_detail(scan_id: str, text: str | None) -> None:
    _patch(scan_id, detail=text)


def run_scan(
    *,
    scan_id: str,
    images_dir: Path,
    scenes_dir: Path,
    steps: int = 7000,
    quality: Quality = "medium",
    trainer: TrainerName = "brush",
    ingest_url: str | None = None,
    ingest_token: str | None = None,
    run_id: str | None = None,
    mode: PoseMode = PoseMode.COLMAP,
    robohack_base: str | None = None,
    go2_mode: bool = False,
) -> ScanInfo:
    """Run the full pipeline. Mutates the scan in the store as we progress.

    `ingest_url`/`ingest_token` override `GS_POT_INGEST_URL`/`GS_POT_INGEST_TOKEN`
    env so per-request webhook calls (e.g. /api/runs/.../process) can target
    robohack with a request-scoped token instead of a shared env secret.

    `run_id` is the scan-run identifier (robohack `frames.run`); required
    when `mode=lidar` so we know which run's pointcloud + poses to fetch.
    Optional in `mode=colmap` — when supplied, it's threaded to
    `push_splat` so the trained splat links back to its scan run
    (robohack splats.runId FK).

    `mode=lidar` skips pycolmap SfM entirely. Instead we pull the dense
    LiDAR cloud and per-frame 6-DoF poses from robohack
    (`/api/scans/:run/{pointcloud,poses}`) and hand them to
    `lidar_poses.write_colmap_workspace`, which emits a Brush-ready
    COLMAP-binary workspace. Brush has no idea the poses came from
    SLAM rather than from SfM. Requires `robohack_base` to be set
    (used to GET the cloud + poses).

    Raises on failure (and writes status=error to the store before re-raising).
    """
    workspace = scenes_dir / scan_id
    try:
        if mode == PoseMode.LIDAR:
            if not run_id:
                raise ValueError("mode=lidar requires run_id")
            if not robohack_base:
                raise ValueError("mode=lidar requires robohack_base")
            _patch(scan_id, status=ScanStatus.SLAM, progress=0.1, detail="fetching pointcloud")
            log.info("[%s] lidar mode: run_id=%s base=%s", scan_id, run_id, robohack_base)
            cloud_path = fetch_pointcloud(
                robohack_base, run_id, workspace / "cloud.ply"
            )
            _patch(scan_id, detail="fetching poses")
            poses_payload = fetch_poses(robohack_base, run_id)
            filenames = list_frame_filenames(robohack_base, run_id)
            poses = _payload_to_frame_poses(poses_payload, filenames)
            if not poses:
                raise RuntimeError(
                    f"run {run_id} has no pose-populated frames yet "
                    "(every frame must have a complete quaternion)"
                )
            _patch(scan_id, detail=f"writing colmap-bin from {len(poses)} poses")
            intrinsics = _intrinsics_from_payload(poses_payload)
            write_colmap_workspace(
                workspace=workspace,
                intrinsics=intrinsics,
                poses=poses,
                images_dir=images_dir,
                cloud_ply=cloud_path,
                image_link_mode="symlink",
            )
            _patch(scan_id, detail=None)
        else:
            _patch(scan_id, status=ScanStatus.POSES, progress=0.1)
            log.info("[%s] colmap start: images=%s workspace=%s go2_mode=%s", scan_id, images_dir, workspace, go2_mode)
            run_colmap(workspace, images_dir, quality=quality, go2_mode=go2_mode)

        _patch(scan_id, status=ScanStatus.TRAINING, progress=0.4)
        log.info("[%s] %s start: steps=%d", scan_id, trainer, steps)
        ply = run_trainer(trainer, workspace, workspace, steps=steps, export_name="scene.ply")

        # Optional push to robohack. Explicit args take precedence over env vars.
        push_url = ingest_url or os.environ.get("GS_POT_INGEST_URL")
        push_token = ingest_token or os.environ.get("GS_POT_INGEST_TOKEN")
        if push_url and push_token:
            _patch(scan_id, status=ScanStatus.PUSHING, progress=0.85, detail=None)
            info = get_store().get(scan_id)
            label = _push_label(info) if info else None
            log.info("[%s] pushing to %s (label=%s)", scan_id, push_url, label)
            result = push_splat(
                ply,
                ingest_url=push_url,
                token=push_token,
                name=label,
                run_id=run_id,
                on_progress=lambda text: _set_detail(scan_id, text),
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


def _payload_to_frame_poses(
    payload: dict[str, object],
    filenames: dict[str, str],
) -> list[FramePose]:
    """Convert robohack's `GET /api/scans/:run/poses` JSON into
    `FramePose` objects suitable for `write_colmap_workspace`.

    Each payload entry carries `frameId, tx, ty, tz, qx, qy, qz, qw`.
    We pair the `frameId` with the on-disk image filename (which
    `fetch_run` already wrote under that name) via the `filenames`
    map. Frames without a known filename are dropped with a warning —
    that means the cloud + poses arrived but the corresponding image
    did not, which we can't train on.

    Builds the 4×4 `T_world_cam` from the (q, t) pair:
        R = quaternion_to_matrix([qx, qy, qz, qw])
        T = [[R, t], [0, 1]]
    """
    raw = payload.get("frames")
    if not isinstance(raw, list):
        return []
    out: list[FramePose] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        frame_id = entry.get("frameId")
        if not isinstance(frame_id, str):
            continue
        fname = filenames.get(frame_id)
        if fname is None:
            log.warning("pose for unknown frame %s — skipping", frame_id)
            continue
        try:
            tx = float(entry["tx"]); ty = float(entry["ty"]); tz = float(entry["tz"])  # noqa: E702
            qx = float(entry["qx"]); qy = float(entry["qy"]); qz = float(entry["qz"]); qw = float(entry["qw"])  # noqa: E702
        except (KeyError, TypeError, ValueError):
            log.warning("malformed pose entry for frame %s — skipping", frame_id)
            continue
        ts_ns_raw = entry.get("tsNs")
        ts_ns = 0
        if isinstance(ts_ns_raw, str) and ts_ns_raw.isdigit():
            ts_ns = int(ts_ns_raw)
        elif isinstance(ts_ns_raw, int):
            ts_ns = ts_ns_raw
        T = np.eye(4, dtype=np.float64)
        T[:3, :3] = _quat_xyzw_to_rotation(qx, qy, qz, qw)
        T[:3, 3] = (tx, ty, tz)
        out.append(FramePose(image_name=fname, T_world_cam=T, ts_ns=ts_ns))
    return out


def _quat_xyzw_to_rotation(qx: float, qy: float, qz: float, qw: float) -> np.ndarray:
    """Standard quaternion-to-3×3 conversion. Assumes unit quaternion;
    normalizes defensively in case a robohack pose lost precision in
    JSON round-tripping (one digit of float64 loss is enough to take
    a unit quaternion off the manifold)."""
    n = (qx * qx + qy * qy + qz * qz + qw * qw) ** 0.5
    if n == 0:
        return np.eye(3, dtype=np.float64)
    qx, qy, qz, qw = qx / n, qy / n, qz / n, qw / n
    xx, yy, zz = qx * qx, qy * qy, qz * qz
    xy, xz, yz = qx * qy, qx * qz, qy * qz
    wx, wy, wz = qw * qx, qw * qy, qw * qz
    return np.array(
        [
            [1 - 2 * (yy + zz), 2 * (xy - wz),     2 * (xz + wy)],
            [2 * (xy + wz),     1 - 2 * (xx + zz), 2 * (yz - wx)],
            [2 * (xz - wy),     2 * (yz + wx),     1 - 2 * (xx + yy)],
        ],
        dtype=np.float64,
    )


def _intrinsics_from_payload(payload: dict[str, object]) -> CameraIntrinsics:
    """Use the robohack-supplied intrinsics if present, otherwise the
    placeholder default. Per issue #4, the calibrated Go2 constants
    will live in `gs_pot/go2_calib.py` and override either source."""
    intr = payload.get("intrinsics")
    if not isinstance(intr, dict):
        return _DEFAULT_GO2_INTRINSICS
    try:
        return CameraIntrinsics(
            fx=float(intr["fx"]),
            fy=float(intr["fy"]),
            cx=float(intr["cx"]),
            cy=float(intr["cy"]),
            width=int(intr["width"]),
            height=int(intr["height"]),
            k1=float(intr.get("k1", 0)),
            k2=float(intr.get("k2", 0)),
            p1=float(intr.get("p1", 0)),
            p2=float(intr.get("p2", 0)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("malformed intrinsics payload (%s); using default", exc)
        return _DEFAULT_GO2_INTRINSICS
