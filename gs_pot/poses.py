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
import re
from pathlib import Path
from typing import Any, Literal

import numpy as np
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
_VIDEO_FRAME_RE = re.compile(r"^frame[_-]?\d+\.(jpg|jpeg|png)$", re.IGNORECASE)


def _looks_like_video_frames(image_dir: Path) -> bool:
    """True if ≥90% of staged images match `frame_NNNN.jpg` — sequential matching
    is the right call (O(N·k) instead of O(N²))."""
    files = [p for p in image_dir.iterdir() if p.suffix.lower() in _IMG_EXTS]
    if len(files) < 10:
        return False
    hits = sum(1 for p in files if _VIDEO_FRAME_RE.match(p.name))
    return hits / len(files) >= 0.9


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


def _rotation_to_align(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Rotation matrix that maps unit vector `src` onto unit vector `dst` (Rodriguez)."""
    src = src / np.linalg.norm(src)
    dst = dst / np.linalg.norm(dst)
    v = np.cross(src, dst)
    c = float(np.dot(src, dst))
    if c > 1 - 1e-8:
        return np.eye(3)
    if c < -1 + 1e-8:
        # 180° — rotate around any axis perpendicular to src.
        axis = np.array([1.0, 0, 0]) if abs(src[0]) < 0.9 else np.array([0, 1.0, 0])
        axis = axis - np.dot(axis, src) * src
        axis /= np.linalg.norm(axis)
        return 2 * np.outer(axis, axis) - np.eye(3)
    k = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + k + k @ k * ((1 - c) / float(np.dot(v, v)))


def _align_to_gravity(sparse_dir: Path) -> None:
    """Rotate the reconstruction in-place so the cameras' average 'down' axis
    aligns with world -Y (Three.js / Spark up-convention).

    COLMAP/OpenCV camera frame: +X right, +Y down, +Z forward. So the camera's
    'down' direction in world space is the second row of R_w2c. Averaging
    across all registered images gives a robust gravity estimate when the
    user shot the scene roughly upright — which is the common case for both
    iPhone walkthroughs and a Go2 with a horizontally-mounted head.
    """
    rec = pycolmap.Reconstruction(str(sparse_dir))
    poses = [img.cam_from_world() for img in rec.images.values() if img.has_pose]
    if not poses:
        log.warning("gravity-align: no registered poses, skipping")
        return

    # A camera held with little roll keeps the axis pointing along gravity
    # consistent across views, while panning scrambles the horizontal + forward
    # axes. So of the three camera axes, the one with the largest mean magnitude
    # (in world space, = rows of the world→cam rotation) is 'down'. This
    # auto-detects the orientation instead of assuming camera +Y: landscape
    # shots resolve to +Y, portrait ones (e.g. iPhone EXIF orientation 6, where
    # COLMAP reports Gravity X≈1) resolve to +X — which previously rolled the
    # whole scene 90°.
    mats = np.stack([p.rotation.matrix() for p in poses])  # (N,3,3) world→cam
    axis_means = [mats[:, i, :].mean(axis=0) for i in range(3)]
    axis_norms = [float(np.linalg.norm(m)) for m in axis_means]
    axis = int(np.argmax(axis_norms))
    gravity = axis_means[axis]
    norm = axis_norms[axis]
    if norm < 0.3:
        # No camera axis is consistent — gravity not recoverable. Bail.
        log.warning(
            "gravity-align: weak signal (max |mean axis|=%.2f over %d poses); skipping",
            norm, len(poses),
        )
        return
    gravity /= norm

    R = _rotation_to_align(gravity, np.array([0.0, -1.0, 0.0]))
    sim3d = pycolmap.Sim3d(1.0, pycolmap.Rotation3d(R), np.zeros(3))
    rec.transform(sim3d)
    rec.write(str(sparse_dir))
    log.info(
        "gravity-align: rotated %d cameras + %d points (down=cam-axis-%d, |mean|=%.2f)",
        len(poses), len(rec.points3D), axis, norm,
    )


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
        device=pycolmap.Device.auto,
    )

    # 2. Matching — sequential for video frames (filename pattern frame_\d+),
    # exhaustive otherwise. Sequential is O(N·k) instead of O(N²); for 200
    # video frames that's ~2000 pairs vs ~20,000.
    match_opts = pycolmap.FeatureMatchingOptions()
    match_opts.guided_matching = preset["guided_matching"]
    use_sequential = _looks_like_video_frames(workspace_images)
    if use_sequential:
        log.info("pycolmap: match_sequential (video-frame pattern detected)")
        pair_opts = pycolmap.SequentialPairingOptions()
        pair_opts.overlap = 15  # match each frame against ±15 neighbours
        pycolmap.match_sequential(
            database_path=database,
            matching_options=match_opts,
            pairing_options=pair_opts,
            device=pycolmap.Device.auto,
        )
    else:
        log.info("pycolmap: match_exhaustive (guided=%s)", preset["guided_matching"])
        pycolmap.match_exhaustive(
            database_path=database,
            matching_options=match_opts,
            device=pycolmap.Device.auto,
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

    # COLMAP can split a scene into several disjoint sub-models (sparse/0,
    # sparse/1, …) and the first directory is NOT necessarily the largest —
    # guided matching on weakly-overlapping sets often buries the best
    # reconstruction in sparse/1. Pick the one with the most registered images.
    # Downstream consumers (Brush, OpenSplat, the scan summary) all read
    # sparse/0, so make the chosen model canonical there.
    best = max(models, key=lambda d: pycolmap.Reconstruction(str(d)).num_reg_images())
    canonical = sparse_root / "0"
    if best != canonical:
        swap = sparse_root / "_swap"
        canonical.rename(swap)
        best.rename(canonical)
        swap.rename(best)  # park the old sparse/0 at the now-free index
    log.info(
        "COLMAP: kept largest of %d sub-model(s) at sparse/0 (%d registered images)",
        len(models), pycolmap.Reconstruction(str(canonical)).num_reg_images(),
    )
    _align_to_gravity(canonical)
    return canonical


def colmap_available() -> bool:
    """Always True now — pycolmap is a hard dependency installed with the package."""
    return True
