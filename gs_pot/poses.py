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

# Minimum SfM output we'll accept before handing off to Brush. Below these
# thresholds the reconstruction is too degenerate to train on, and Brush
# panics deep in Rust with `min > max, NaN` (clamp on zero scene scale).
# Fail loudly with a useful message here instead.
_MIN_REG_IMAGES = 10
_MIN_3D_POINTS = 50
_MIN_SCENE_DIAGONAL = 0.01


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

    down_world = np.stack([p.rotation.matrix()[1, :] for p in poses])
    gravity = down_world.mean(axis=0)
    norm = float(np.linalg.norm(gravity))
    if norm < 0.3:
        # Cameras pointing in wildly different directions — gravity not
        # recoverable from the up-axis heuristic. Bail without rotating.
        log.warning(
            "gravity-align: weak signal (|mean down|=%.2f over %d poses); skipping",
            norm, len(poses),
        )
        return
    gravity /= norm

    R = _rotation_to_align(gravity, np.array([0.0, -1.0, 0.0]))
    sim3d = pycolmap.Sim3d(1.0, pycolmap.Rotation3d(R), np.zeros(3))
    rec.transform(sim3d)
    rec.write(str(sparse_dir))
    log.info(
        "gravity-align: rotated %d cameras + %d points (|mean down|=%.2f)",
        len(poses), len(rec.points3D), norm,
    )


def _check_reconstruction_sane(sparse_dir: Path, input_image_count: int) -> None:
    """Raise with a useful message if the SfM output is too degenerate for Brush."""
    rec = pycolmap.Reconstruction(str(sparse_dir))
    reg = rec.num_reg_images()
    n_pts = rec.num_points3D()
    diag = 0.0
    if n_pts > 0:
        coords = np.array([p.xyz for p in rec.points3D.values()])
        diag = float(np.linalg.norm(coords.max(axis=0) - coords.min(axis=0)))
    if reg < _MIN_REG_IMAGES or n_pts < _MIN_3D_POINTS or diag < _MIN_SCENE_DIAGONAL:
        raise RuntimeError(
            "SfM reconstruction too degenerate to train a splat from. "
            f"registered={reg}/{input_image_count} images, "
            f"3D points={n_pts}, scene diagonal={diag:.4f}. "
            "Likely causes: pure-rotation capture (no baseline → can't "
            "triangulate), weak texture (mirrors / glass / blank walls), "
            "too few angles per stop, low-resolution frames, or motion "
            "blur. Try a textured scene with more overlap between "
            "consecutive shots."
        )
    log.info(
        "SfM sanity: registered=%d/%d points=%d diagonal=%.3f — passing to trainer",
        reg, input_image_count, n_pts, diag,
    )


def run_colmap(
    workspace: Path,
    image_dir: Path,
    *,
    quality: Quality = "medium",
    single_camera: bool = False,
    go2_mode: bool = False,
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

    preset = dict(_QUALITY_PRESETS[quality])
    # Go2 mode: same physical camera every shot, wider FOV, 640×360 frames.
    # Override the preset to the proven scan-go2.py settings. Keeps existing
    # phone-photo flows untouched.
    if go2_mode:
        preset["camera_model"] = "OPENCV"
        preset["max_image_size"] = max(preset["max_image_size"], 1024)
        preset["max_num_features"] = max(preset["max_num_features"], 16384)
        preset["guided_matching"] = True
        single_camera = True
        log.info("Go2 mode: OPENCV/SINGLE camera, 16k features, guided matching")

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
    if go2_mode:
        # Narrow-baseline rotation-heavy captures need lenient registration
        # thresholds; mirrors scripts/scan-go2.py.
        map_opts.min_model_size = 3
        map_opts.min_num_matches = 10
        map_opts.mapper.init_min_num_inliers = 30
        map_opts.mapper.abs_pose_min_num_inliers = 10
        map_opts.mapper.init_min_tri_angle = 1.0
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
    # COLMAP can output multiple disjoint reconstructions; pick the largest by
    # registered-camera count. Brush reads `sparse/0/` by convention, so the
    # picked model must live there.
    best = max(models, key=lambda d: len(pycolmap.Reconstruction(str(d)).images))
    if best.name != "0":
        target = sparse_root / "0"
        if target.exists():
            target.rename(sparse_root / f"0.discarded_{target.stat().st_mtime_ns}")
        best.rename(target)
        best = target
    log.info(
        "COLMAP sparse model: %s (%d reconstruction(s) total, kept largest)",
        best, len(reconstructions),
    )
    _check_reconstruction_sane(best, n_linked)
    _align_to_gravity(best)
    return best


def colmap_available() -> bool:
    """Always True now — pycolmap is a hard dependency installed with the package."""
    return True
