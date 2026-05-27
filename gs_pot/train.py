"""Brush training wrapper. COLMAP workspace → .ply Gaussian splat."""

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_BRUSH = _REPO_ROOT / "bin" / "brush"


def _brush_binary() -> Path:
    override = os.environ.get("BRUSH_BIN")
    if override:
        return Path(override)
    return _DEFAULT_BRUSH


def run_brush(
    dataset_dir: Path,
    output_dir: Path,
    *,
    steps: int = 7000,
    max_resolution: int = 1920,
    export_name: str = "scene.ply",
) -> Path:
    """Train a Gaussian splat with Brush.

    `dataset_dir` should be a COLMAP-format workspace (contains `images/` and `sparse/0/`).
    Returns the path to the exported .ply file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    brush = _brush_binary()
    if not brush.exists():
        raise RuntimeError(
            f"Brush binary not found at {brush}. See bin/README.md for install instructions."
        )

    cmd = [
        str(brush),
        str(dataset_dir),
        "--total-steps",
        str(steps),
        "--max-resolution",
        str(max_resolution),
        "--export-path",
        str(output_dir),
        "--export-name",
        export_name,
        "--export-every",
        str(steps),  # only export at the very end
    ]
    log.info("running: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)

    ply = output_dir / export_name
    if not ply.exists():
        # Brush sometimes templates {iter} into the name even when we pin export-every.
        candidates = sorted(output_dir.glob("*.ply"))
        if not candidates:
            raise RuntimeError(f"Brush produced no .ply in {output_dir}")
        ply = candidates[-1]
    log.info("Brush exported: %s", ply)
    return ply


def brush_available() -> bool:
    return _brush_binary().exists()
