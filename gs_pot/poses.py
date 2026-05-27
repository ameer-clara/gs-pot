"""COLMAP pose estimation via pycolmap (Python bindings).

We use pycolmap rather than the `colmap` CLI because Homebrew's macOS arm64
COLMAP build has a deterministic use-after-free in the SIFT matcher that
crashes with SIGSEGV/SIGABRT in `Creating SIFT CPU feature matcher`.
pycolmap's wheels ship their own COLMAP binary compiled via cibuildwheel
for darwin-arm64 and don't hit the same bug.

Pipeline is unchanged: extract_features → match_exhaustive → incremental_mapping.
On-disk workspace layout is unchanged too, so Brush keeps reading it.
"""

import logging
from pathlib import Path
from typing import Any, Literal

import pycolmap

log = logging.getLogger(__name__)

Quality = Literal["low", "medium", "high", "extreme"]

_QUALITY_PRESETS: dict[Quality, dict[str, Any]] = {
    "low": {
        "camera_model": "SIMPLE_RADIAL",
        "max_image_size": 1000,
        "max_num_features": 2048,
        "guided_matching": False,
        "ba_local_max_num_iterations": 12,
        "ba_global_max_num_iterations": 30,
    },
    "medium": {
        "camera_model": "SIMPLE_RADIAL",
        "max_image_size": 1600,
        "max_num_features": 8192,
        # guided_matching off at medium — it over-prunes weak-texture scenes
        # (bathrooms, white walls). High+ assume textured scenes where it helps.
        "guided_matching": False,
        "ba_local_max_num_iterations": 16,
        "ba_global_max_num_iterations": 50,
    },
    "high": {
        "camera_model": "SIMPLE_RADIAL",
        "max_image_size": 2400,
        "max_num_features": 16384,
        "guided_matching": True,
        "ba_local_max_num_iterations": 25,
        "ba_global_max_num_iterations": 75,
    },
    "extreme": {
        "camera_model": "OPENCV",
        "max_image_size": 3200,
        "max_num_features": 32768,
        "guided_matching": True,
        "ba_local_max_num_iterations": 40,
        "ba_global_max_num_iterations": 100,
    },
}

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}


def _stage_images(image_dir: Path, workspace_images: Path) -> int:
    """Per-file symlink every real image from `image_dir` into `workspace_images`.

    Skips hidden files (`.DS_Store`), hidden dirs (`.omc/`), and anything
    without an image extension. Returns the number of images staged.
    """
    if workspace_images.is_symlink():
        workspace_images.unlink()
    workspace_images.mkdir(parents=True, exist_ok=True)

    n = 0
    for src in sorted(image_dir.iterdir()):
        if not src.is_file() or src.name.startswith("."):
            continue
        if src.suffix.lower() not in _IMG_EXTS:
            continue
        dst = workspace_images / src.name
        if dst.exists() or dst.is_symlink():
            continue
        dst.symlink_to(src.resolve())
        n += 1
    return n


def run_colmap(
    workspace: Path,
    image_dir: Path,
    *,
    quality: Quality = "medium",
    single_camera: bool = False,
) -> Path:
    """Run COLMAP via pycolmap. Returns the sparse model dir.

    Workspace layout after success:
        workspace/
            images/           ← per-file symlinks of actual jpg/png files
            database.db
            sparse/0/
                cameras.bin
                images.bin
                points3D.bin
    """
    workspace.mkdir(parents=True, exist_ok=True)
    workspace_images = workspace / "images"
    n_linked = _stage_images(image_dir, workspace_images)
    if n_linked == 0:
        raise RuntimeError(
            f"no image files found in {image_dir} (looked for: {sorted(_IMG_EXTS)})"
        )
    log.info("staged %d images at %s", n_linked, workspace_images)

    preset = _QUALITY_PRESETS[quality]
    database = workspace / "database.db"
    if database.exists():
        database.unlink()

    # 1. Feature extraction
    log.info(
        "pycolmap: extract_features (max_image_size=%d, max_features=%d, camera=%s)",
        preset["max_image_size"], preset["max_num_features"], preset["camera_model"],
    )
    reader_opts = pycolmap.ImageReaderOptions()
    reader_opts.camera_model = preset["camera_model"]
    ext_opts = pycolmap.FeatureExtractionOptions()
    ext_opts.max_image_size = preset["max_image_size"]
    ext_opts.sift.max_num_features = preset["max_num_features"]
    pycolmap.extract_features(
        database_path=database,
        image_path=workspace_images,
        camera_mode=(
            pycolmap.CameraMode.SINGLE if single_camera else pycolmap.CameraMode.AUTO
        ),
        reader_options=reader_opts,
        extraction_options=ext_opts,
        device=pycolmap.Device.cpu,
    )

    # 2. Exhaustive matching
    log.info("pycolmap: match_exhaustive (guided=%s)", preset["guided_matching"])
    match_opts = pycolmap.FeatureMatchingOptions()
    match_opts.guided_matching = preset["guided_matching"]
    pycolmap.match_exhaustive(
        database_path=database,
        matching_options=match_opts,
        device=pycolmap.Device.cpu,
    )

    # 3. Incremental SfM
    sparse_root = workspace / "sparse"
    sparse_root.mkdir(exist_ok=True)
    log.info("pycolmap: incremental_mapping")
    map_opts = pycolmap.IncrementalPipelineOptions()
    map_opts.ba_local_max_num_iterations = preset["ba_local_max_num_iterations"]
    map_opts.ba_global_max_num_iterations = preset["ba_global_max_num_iterations"]
    reconstructions = pycolmap.incremental_mapping(
        database_path=database,
        image_path=workspace_images,
        output_path=sparse_root,
        options=map_opts,
    )

    models = sorted(p for p in sparse_root.iterdir() if p.is_dir())
    if not models or not reconstructions:
        raise RuntimeError(
            f"COLMAP produced no sparse models under {sparse_root}. "
            "Common causes: too few overlapping images, repetitive textures, "
            "mirrors/glass, motion blur."
        )
    log.info(
        "COLMAP sparse model: %s (%d reconstruction(s) total)",
        models[0], len(reconstructions),
    )
    return models[0]


def colmap_available() -> bool:
    """Always True now — pycolmap is a hard dependency installed with the package."""
    return True
