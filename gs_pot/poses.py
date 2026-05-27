"""COLMAP pose estimation. Images → camera intrinsics + extrinsics + sparse point cloud.

We use `automatic_reconstructor` (the simple one-shot) with `--dense 0` since
Brush only needs the sparse SfM output, not the dense MVS.
"""

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

Quality = Literal["low", "medium", "high", "extreme"]


def run_colmap(workspace: Path, image_dir: Path, *, quality: Quality = "medium") -> Path:
    """Run COLMAP on `image_dir`, leave outputs under `workspace`. Returns the sparse model dir.

    The workspace ends up looking like:
        workspace/
            images/           ← symlink (or copy) of image_dir
            database.db
            sparse/0/         ← Brush's input
                cameras.bin
                images.bin
                points3D.bin
    """
    workspace.mkdir(parents=True, exist_ok=True)

    workspace_images = workspace / "images"
    if not workspace_images.exists():
        # Symlink to keep disk footprint small. Brush + COLMAP both follow.
        workspace_images.symlink_to(image_dir.resolve())
    elif workspace_images.is_symlink() and workspace_images.resolve() != image_dir.resolve():
        workspace_images.unlink()
        workspace_images.symlink_to(image_dir.resolve())

    cmd = [
        "colmap",
        "automatic_reconstructor",
        "--workspace_path",
        str(workspace),
        "--image_path",
        str(workspace_images),
        "--use_gpu",
        "0",
        "--quality",
        quality,
        "--dense",
        "0",
        "--single_camera",
        "1",
    ]
    log.info("running: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)

    sparse_root = workspace / "sparse"
    if not sparse_root.exists():
        raise RuntimeError(f"COLMAP did not produce {sparse_root}")
    models = sorted(p for p in sparse_root.iterdir() if p.is_dir())
    if not models:
        raise RuntimeError(f"COLMAP produced no sparse models under {sparse_root}")
    log.info("COLMAP sparse model: %s", models[0])
    return models[0]


def colmap_available() -> bool:
    """Quick check that colmap is on PATH (without invoking a real reconstruction)."""
    return shutil.which("colmap") is not None
