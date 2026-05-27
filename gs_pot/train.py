"""Gaussian splat training. Two trainer backends, same input/output contract.

Both backends consume a COLMAP-style workspace (`images/` + `sparse/0/*.bin`)
and produce a `.ply` Gaussian splat. Pick a backend via the `trainer` argument
or the `--trainer` CLI flag.

Backends:
    brush      — Rust/WGPU, cross-platform, no build step (single binary in
                 bin/brush). Slower on M-series GPUs (WGPU→Metal overhead).
    opensplat  — C++/libtorch with native Metal/CUDA/HIP, built locally
                 (bin/opensplat). 3-5× faster than Brush on Apple Silicon
                 in real-world reports. Requires `brew install cmake opencv
                 pytorch` + cmake build (see bin/README.md).
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

TrainerName = Literal["brush", "opensplat"]

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_BRUSH = _REPO_ROOT / "bin" / "brush"
_DEFAULT_OPENSPLAT = _REPO_ROOT / "bin" / "opensplat"


# ── Brush ─────────────────────────────────────────────────────────────────────


def _brush_binary() -> Path:
    return Path(os.environ.get("BRUSH_BIN", _DEFAULT_BRUSH))


def run_brush(
    dataset_dir: Path,
    output_dir: Path,
    *,
    steps: int = 7000,
    max_resolution: int = 1920,
    export_name: str = "scene.ply",
) -> Path:
    """Train with Brush. Returns the exported .ply path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    brush = _brush_binary()
    if not brush.exists():
        raise RuntimeError(
            f"Brush binary not found at {brush}. See bin/README.md for install."
        )

    cmd = [
        str(brush),
        str(dataset_dir),
        "--total-steps", str(steps),
        "--max-resolution", str(max_resolution),
        "--export-path", str(output_dir),
        "--export-name", export_name,
        "--export-every", str(steps),
    ]
    log.info("running: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)

    ply = output_dir / export_name
    if not ply.exists():
        candidates = sorted(output_dir.glob("*.ply"))
        if not candidates:
            raise RuntimeError(f"Brush produced no .ply in {output_dir}")
        ply = candidates[-1]
    log.info("Brush exported: %s", ply)
    return ply


def brush_available() -> bool:
    return _brush_binary().exists()


# ── OpenSplat ─────────────────────────────────────────────────────────────────


def _opensplat_binary() -> Path:
    return Path(os.environ.get("OPENSPLAT_BIN", _DEFAULT_OPENSPLAT))


def run_opensplat(
    dataset_dir: Path,
    output_dir: Path,
    *,
    steps: int = 2000,
    export_name: str = "scene.ply",
) -> Path:
    """Train with OpenSplat. Returns the exported .ply path.

    OpenSplat consumes the same COLMAP workspace layout as Brush (`images/` +
    `sparse/0/*.bin`). Step counts are trainer-specific: 2000 is OpenSplat's
    own default and roughly matches Brush at ~7000.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    opensplat = _opensplat_binary()
    if not opensplat.exists():
        raise RuntimeError(
            f"OpenSplat binary not found at {opensplat}. "
            "Build it with the steps in bin/README.md (`brew install cmake "
            "opencv pytorch` then cmake + make)."
        )

    output_path = output_dir / export_name
    cmd = [
        str(opensplat),
        str(dataset_dir),
        "-n", str(steps),
        "-o", str(output_path),
    ]
    log.info("running: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)

    if not output_path.exists():
        # OpenSplat sometimes writes to cwd by default; pick up any .ply.
        candidates = sorted(output_dir.glob("*.ply"))
        if not candidates:
            raise RuntimeError(f"OpenSplat produced no .ply in {output_dir}")
        output_path = candidates[-1]
    log.info("OpenSplat exported: %s", output_path)
    return output_path


def opensplat_available() -> bool:
    return _opensplat_binary().exists()


# ── Dispatcher ────────────────────────────────────────────────────────────────


def run_trainer(
    trainer: TrainerName,
    dataset_dir: Path,
    output_dir: Path,
    *,
    steps: int,
    export_name: str = "scene.ply",
) -> Path:
    """Single entry point pipeline.run_scan() calls."""
    if trainer == "brush":
        return run_brush(dataset_dir, output_dir, steps=steps, export_name=export_name)
    if trainer == "opensplat":
        return run_opensplat(dataset_dir, output_dir, steps=steps, export_name=export_name)
    raise ValueError(f"unknown trainer: {trainer!r}")
