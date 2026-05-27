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
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


def fetch_run(
    robohack_base: str,
    run_id: str,
    dest_dir: Path,
    *,
    timeout: float = 60.0,
) -> int:
    """Download every frame for `run_id` from robohack into `dest_dir`.

    Returns the number of images now present in `dest_dir` for this run
    (counts both freshly downloaded and already-present files).
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
        return 0

    log.info("run %s: %d frames to fetch", run_id, len(targets))
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for fname, url in targets:
            dst = dest_dir / fname
            if dst.exists() and dst.stat().st_size > 0:
                continue
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with dst.open("wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        f.write(chunk)

    log.info("run %s: %d frames in %s", run_id, len(targets), dest_dir)
    return len(targets)
