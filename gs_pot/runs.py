"""Pull a robohack run's frames into a local directory.

Robohack exposes `GET /api/scans/<run_id>` (public, no auth) returning the
run's frames as a tree:

    { "scans": [
        { "run": "...", "positions": [
            { "position": 0, "images": [{ "id", "angle", "url" }, ...] },
            ...
        ]}
    ]}

Each `url` is a presigned S3 GET (6h TTL). We download each into a local
folder named `p{position:03d}_a{angle:03d}_{id}.jpg` so the listing is
sortable for debugging — COLMAP itself doesn't care about order.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


def fetch_run(
    robohack_base: str,
    run_id: str,
    dest_dir: Path,
    *,
    timeout: float = 60.0,
    on_progress: Callable[[int, int], None] | None = None,
) -> int:
    """Download every frame for `run_id` from robohack into `dest_dir`.

    `on_progress(i, n)` is invoked once per frame *after* completion (counts
    both freshly downloaded and already-present files, so resuming a partial
    fetch still ticks the counter forward). Use it to surface "5/20"-style
    progress in a UI status feed.

    Returns the number of images now present in `dest_dir` for this run.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    list_url = f"{robohack_base.rstrip('/')}/api/scans/{run_id}"
    log.info("fetching run %s from %s", run_id, list_url)

    with httpx.Client(timeout=timeout) as client:
        r = client.get(list_url)
        r.raise_for_status()
        data = r.json()

    targets: list[tuple[str, str]] = []
    for scan in data.get("scans", []):
        if scan.get("run") != run_id:
            continue
        for pos in scan.get("positions", []):
            position = pos.get("position")
            pos_label = f"{int(position):03d}" if position is not None else "xxx"
            for img in pos.get("images", []):
                angle = img.get("angle")
                ang_label = f"{int(round(angle)):03d}" if angle is not None else "xxx"
                fname = f"p{pos_label}_a{ang_label}_{img['id']}.jpg"
                targets.append((fname, img["url"]))

    if not targets:
        log.warning("run %s: 0 frames found", run_id)
        if on_progress:
            on_progress(0, 0)
        return 0

    total = len(targets)
    log.info("run %s: %d frames to fetch", run_id, total)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for i, (fname, url) in enumerate(targets, start=1):
            dst = dest_dir / fname
            if not (dst.exists() and dst.stat().st_size > 0):
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    with dst.open("wb") as f:
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            f.write(chunk)
            if on_progress:
                on_progress(i, total)

    log.info("run %s: %d frames in %s", run_id, total, dest_dir)
    return total


def fetch_pointcloud(
    robohack_base: str,
    run_id: str,
    dest_path: Path,
    *,
    timeout: float = 120.0,
) -> Path:
    """Pull the newest LiDAR cloud for `run_id` into `dest_path`.

    Robohack exposes `GET /api/scans/<run_id>/pointcloud` (public, no
    auth) that 302s to a presigned S3 GET. We follow the redirect and
    stream the (potentially ~100 MB) cloud straight to disk. Returns
    the final on-disk path so callers can hand it to
    `lidar_poses.write_colmap_workspace(cloud_ply=...)`.

    Raises `httpx.HTTPStatusError` if the run has no cloud (404 — the
    LiDAR-splat path can't run without one).
    """
    list_url = f"{robohack_base.rstrip('/')}/api/scans/{run_id}/pointcloud"
    log.info("fetching pointcloud for run %s from %s", run_id, list_url)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", list_url) as resp:
            resp.raise_for_status()
            with dest_path.open("wb") as f:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    f.write(chunk)
    log.info("run %s: pointcloud → %s (%d bytes)",
             run_id, dest_path, dest_path.stat().st_size)
    return dest_path


def fetch_poses(
    robohack_base: str,
    run_id: str,
    *,
    timeout: float = 60.0,
) -> dict[str, object]:
    """Pull the 6-DoF poses for every pose-populated frame in `run_id`.

    Robohack exposes `GET /api/scans/<run_id>/poses` (public, no auth)
    returning:

        {
          "runId":      str,
          "intrinsics": null | {fx, fy, cx, cy, w, h, model, k1..k4},
          "frames":     [{frameId, tx, ty, tz, qx, qy, qz, qw, tsNs?}],
        }

    Frames without a complete quaternion are filtered out server-side
    (a partial quaternion would warp the splat). Returns the parsed
    JSON verbatim; callers convert to `lidar_poses.FramePose` after
    pairing each `frameId` with its on-disk filename via
    `list_frame_filenames`.
    """
    url = f"{robohack_base.rstrip('/')}/api/scans/{run_id}/poses"
    log.info("fetching poses for run %s from %s", run_id, url)
    with httpx.Client(timeout=timeout) as client:
        r = client.get(url)
        r.raise_for_status()
        data = r.json()
    n_frames = len(data.get("frames", []))
    log.info("run %s: %d poses", run_id, n_frames)
    return data


def list_frame_filenames(
    robohack_base: str,
    run_id: str,
    *,
    timeout: float = 60.0,
) -> dict[str, str]:
    """Return a `{frameId: on-disk-filename}` mapping for the run.

    Hits the same `GET /api/scans/<run_id>` endpoint `fetch_run` uses,
    then derives the staged-on-disk filename via the same
    `p{position:03d}_a{angle:03d}_{id}.jpg` recipe. Used by the
    LiDAR-splat path to pair `FramePose.image_name` to the file that
    `fetch_run` already wrote into the workspace.
    """
    list_url = f"{robohack_base.rstrip('/')}/api/scans/{run_id}"
    with httpx.Client(timeout=timeout) as client:
        r = client.get(list_url)
        r.raise_for_status()
        data = r.json()

    out: dict[str, str] = {}
    for scan in data.get("scans", []):
        if scan.get("run") != run_id:
            continue
        for pos in scan.get("positions", []):
            position = pos.get("position")
            pos_label = f"{int(position):03d}" if position is not None else "xxx"
            for img in pos.get("images", []):
                angle = img.get("angle")
                ang_label = (
                    f"{int(round(angle)):03d}" if angle is not None else "xxx"
                )
                out[img["id"]] = f"p{pos_label}_a{ang_label}_{img['id']}.jpg"
    return out
