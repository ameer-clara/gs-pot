"""LiDAR-pose path — write a COLMAP-binary workspace directly from a SLAM
dump, skipping pycolmap SfM entirely.

The companion to `gs_pot.poses.run_colmap`. Same on-disk shape
(`<workspace>/sparse/0/{cameras,frames,images,points3D,rigs}.bin` +
`<workspace>/images/`), so `train.run_brush(workspace, ...)` consumes the
output unchanged — it doesn't know whether the poses came from COLMAP SfM
or from a LiDAR SLAM stack (KISS-ICP / FAST-LIO2).

Why we don't write COLMAP `.txt` files directly: pycolmap already speaks
the binary format with full version-skew handling (the file layout
recently grew rigs/frames for multi-sensor support; hand-rolling
`struct.pack` against the older spec would silently produce
unreadable workspaces on newer Brush builds). pycolmap is a runtime
dep anyway.

Coordinate conventions:
  * `T_world_cam` is the rigid transform that maps a point in CAMERA
    coordinates into WORLD coordinates (column-vector convention,
    p_world = T_world_cam @ p_cam_h).
  * Camera frame = OpenCV: x-right, y-down, z-forward.
  * If your SLAM emits world ↔ LiDAR poses, compose with the static
    `T_cam_lidar` extrinsic from the Go2 mount (handled by the caller
    via `gs_pot.go2_calib`).
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pycolmap

log = logging.getLogger(__name__)

# Image extensions accepted in the staged `images/` dir (mirrors poses._stage_images).
_IMG_EXTS = {".jpg", ".jpeg", ".png"}


@dataclass(frozen=True)
class CameraIntrinsics:
    """Single-camera intrinsics for a Go2-style fixed-mount RGB sensor.

    Only OPENCV camera model is supported here (4 distortion params:
    k1, k2, p1, p2). That matches what `scripts/scan-go2.py` already
    chooses for the Go2 in the COLMAP path, so the LiDAR and COLMAP
    workspaces are interchangeable for Brush.
    """

    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int
    k1: float = 0.0
    k2: float = 0.0
    p1: float = 0.0
    p2: float = 0.0
    model: Literal["OPENCV"] = "OPENCV"

    def to_params(self) -> list[float]:
        return [self.fx, self.fy, self.cx, self.cy, self.k1, self.k2, self.p1, self.p2]


@dataclass(frozen=True)
class FramePose:
    """One camera pose, named by the image file that captured it."""

    image_name: str            # filename inside `<workspace>/images/`, e.g. `frame_0001.jpg`
    T_world_cam: np.ndarray    # (4, 4) float64, world ← camera
    ts_ns: int = 0             # monotonic nanosecond timestamp (informational)


def _pose_to_rigid3d(T_world_cam: np.ndarray) -> pycolmap.Rigid3d:
    """COLMAP stores `cam_from_world` (the inverse of `T_world_cam`).

    pycolmap.Rigid3d wraps a unit quaternion + translation. We invert
    the 4×4 by transposing the 3×3 rotation and applying it to the
    negated translation — cheaper and numerically tighter than
    `np.linalg.inv` for rigid transforms.
    """
    if T_world_cam.shape != (4, 4):
        raise ValueError(f"T_world_cam must be (4,4), got {T_world_cam.shape}")
    R_wc = T_world_cam[:3, :3]
    t_wc = T_world_cam[:3, 3]
    R_cw = R_wc.T
    t_cw = -R_cw @ t_wc
    rot = pycolmap.Rotation3d(_rotation_matrix_to_quat_xyzw(R_cw))
    return pycolmap.Rigid3d(rotation=rot, translation=t_cw)


def _rotation_matrix_to_quat_xyzw(R: np.ndarray) -> np.ndarray:
    """Convert a 3×3 rotation matrix to a quaternion [x, y, z, w].

    pycolmap.Rotation3d takes (x, y, z, w). Standard branch-free
    Shepperd algorithm; works for any proper rotation.
    """
    m = np.asarray(R, dtype=np.float64)
    trace = m[0, 0] + m[1, 1] + m[2, 2]
    if trace > 0:
        s = 0.5 / np.sqrt(trace + 1.0)
        w = 0.25 / s
        x = (m[2, 1] - m[1, 2]) * s
        y = (m[0, 2] - m[2, 0]) * s
        z = (m[1, 0] - m[0, 1]) * s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = 2.0 * np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2])
        w = (m[2, 1] - m[1, 2]) / s
        x = 0.25 * s
        y = (m[0, 1] + m[1, 0]) / s
        z = (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = 2.0 * np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2])
        w = (m[0, 2] - m[2, 0]) / s
        x = (m[0, 1] + m[1, 0]) / s
        y = 0.25 * s
        z = (m[1, 2] + m[2, 1]) / s
    else:
        s = 2.0 * np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1])
        w = (m[1, 0] - m[0, 1]) / s
        x = (m[0, 2] + m[2, 0]) / s
        y = (m[1, 2] + m[2, 1]) / s
        z = 0.25 * s
    return np.array([x, y, z, w], dtype=np.float64)


def voxel_downsample(points: np.ndarray, voxel_size: float) -> np.ndarray:
    """Simple voxel-grid downsample, numpy-only (no Open3D dep).

    Args:
        points: (N, 3) float array.
        voxel_size: edge length in the same units as `points`.

    Returns:
        (M, 3) float array — one representative point (the first hit)
        per occupied voxel. M ≤ N; deterministic for a given input.
    """
    if points.size == 0:
        return points
    if voxel_size <= 0:
        return points
    keys = np.floor(points / voxel_size).astype(np.int64)
    # unique returns the first index per group with return_index=True (after
    # the input is treated as rows). Stable wrt insertion order.
    _, uniq_idx = np.unique(keys, axis=0, return_index=True)
    uniq_idx.sort()
    return points[uniq_idx]


def load_ply_points(path: Path) -> np.ndarray:
    """Tiny PLY reader for ASCII xyz (and uchar-rgb-tolerated) clouds.

    Returns (N, 3) float64. We deliberately avoid pulling Open3D /
    plyfile in just to read points — most SLAM dumps are tiny in
    practice, and we voxel-downsample before storing anyway.

    Binary PLYs are unsupported here. Callers writing binary clouds
    should convert to ASCII or pass an `np.ndarray` directly to
    `write_colmap_workspace` via the `cloud_points=` kwarg.
    """
    text = path.read_text(errors="replace").splitlines()
    if not text or text[0].strip().lower() != "ply":
        raise ValueError(f"{path} is not a PLY file")
    n_verts = 0
    body_start = -1
    for i, line in enumerate(text):
        s = line.strip()
        if s.startswith("element vertex"):
            n_verts = int(s.split()[-1])
        elif s == "end_header":
            body_start = i + 1
            break
    if body_start < 0:
        raise ValueError(f"{path} has no end_header")
    if len(text) - body_start < n_verts:
        raise ValueError(
            f"{path} body is truncated: header declares {n_verts} vertices "
            f"but only {len(text) - body_start} lines follow end_header"
        )
    pts = np.zeros((n_verts, 3), dtype=np.float64)
    for j in range(n_verts):
        parts = text[body_start + j].split()
        if len(parts) < 3:
            raise ValueError(
                f"{path} line {body_start + j + 1}: expected ≥3 floats "
                f"(x y z), got {len(parts)} fields"
            )
        pts[j] = (float(parts[0]), float(parts[1]), float(parts[2]))
    return pts


def write_colmap_workspace(
    workspace: Path,
    intrinsics: CameraIntrinsics,
    poses: list[FramePose],
    images_dir: Path,
    cloud_points: np.ndarray | None = None,
    cloud_ply: Path | None = None,
    voxel_size: float = 0.05,
    point_color: tuple[int, int, int] = (200, 200, 200),
    image_link_mode: Literal["symlink", "copy"] = "symlink",
) -> Path:
    """Build a COLMAP-binary workspace from a SLAM dump. Returns the
    sparse/0 directory.

    Layout produced (mirrors `gs_pot.poses.run_colmap`):

        <workspace>/sparse/0/cameras.bin
                            frames.bin
                            images.bin
                            points3D.bin
                            rigs.bin
        <workspace>/images/<image_name>...

    Args:
        workspace: directory to populate; created if missing.
        intrinsics: shared single-camera intrinsics (OPENCV model).
        poses: per-image camera pose; `image_name` MUST exist under
               `images_dir`.
        images_dir: source images. Linked/copied into `<workspace>/images/`.
        cloud_points: (N, 3) cloud to use as the `points3D.bin` seed.
                      Mutually exclusive with `cloud_ply`.
        cloud_ply: optional path to an ASCII PLY to load via
                   `load_ply_points`.
        voxel_size: voxel-grid downsample edge length in cloud units
                    (meters for KISS-ICP / FAST-LIO2). 0 disables it.
        point_color: uniform RGB for every seeded point (Brush will
                     learn per-splat color from the images anyway; the
                     seed only constrains geometry).
        image_link_mode: "symlink" (fast, no extra disk) or "copy"
                         (portable across filesystem boundaries).

    Returns the path to the sparse model dir (`<workspace>/sparse/0`)
    so callers can feed it back to pycolmap / Brush.
    """
    if cloud_points is not None and cloud_ply is not None:
        raise ValueError("pass cloud_points OR cloud_ply, not both")
    workspace.mkdir(parents=True, exist_ok=True)
    ws_images = workspace / "images"
    ws_images.mkdir(exist_ok=True)
    sparse_dir = workspace / "sparse" / "0"
    sparse_dir.mkdir(parents=True, exist_ok=True)

    # Stage images (verify each pose's image exists in the source dir).
    staged = _stage_images(images_dir, ws_images, [p.image_name for p in poses], image_link_mode)
    if staged != len(poses):
        log.warning("staged %d/%d images (missing pose targets are dropped)", staged, len(poses))

    # Build pycolmap reconstruction.
    rec = pycolmap.Reconstruction()
    camera_id = 1
    cam = pycolmap.Camera(
        model=intrinsics.model,
        width=intrinsics.width,
        height=intrinsics.height,
        params=intrinsics.to_params(),
    )
    cam.camera_id = camera_id
    rec.add_camera(cam)

    # One rig containing the single camera as the reference sensor.
    rig = pycolmap.Rig()
    rig.rig_id = 1
    rig.add_ref_sensor(
        pycolmap.sensor_t(type=pycolmap.SensorType.CAMERA, id=camera_id)
    )
    rec.add_rig(rig)

    # Per pose: a frame (carries the rig_from_world transform) plus an
    # image (carries the filename + camera_id). Frame's data_ids link to
    # the image. Brush reads the frame's pose, not the image's, on
    # rigs/frames-format reconstructions.
    for i, pose in enumerate(poses, start=1):
        if (ws_images / pose.image_name).exists() is False:
            log.warning("skip pose for missing image %s", pose.image_name)
            continue
        frame = pycolmap.Frame()
        frame.frame_id = i
        frame.rig_id = 1
        frame.rig_from_world = _pose_to_rigid3d(pose.T_world_cam)
        frame.add_data_id(
            pycolmap.data_t(
                sensor_id=pycolmap.sensor_t(type=pycolmap.SensorType.CAMERA, id=camera_id),
                id=i,
            )
        )
        rec.add_frame(frame)

        img = pycolmap.Image(name=pose.image_name, camera_id=camera_id)
        img.image_id = i
        img.frame_id = i
        rec.add_image(img)

    # Seed points from the LiDAR cloud.
    pts = _resolve_cloud(cloud_points, cloud_ply)
    if pts is not None and pts.size > 0:
        if voxel_size > 0:
            pts = voxel_downsample(pts, voxel_size)
        color = np.array(point_color, dtype=np.uint8)
        for p in pts:
            rec.add_point3D(p.astype(np.float64), track=pycolmap.Track(), color=color)
        log.info("seeded %d points (voxel=%.3f m)", len(pts), voxel_size)

    rec.write_binary(str(sparse_dir))
    log.info(
        "wrote COLMAP workspace: %d cameras, %d frames, %d images, %d points → %s",
        len(rec.cameras), len(rec.frames), len(rec.images), len(rec.points3D), sparse_dir,
    )
    return sparse_dir


def _stage_images(
    src_dir: Path,
    dst_dir: Path,
    wanted: list[str],
    mode: Literal["symlink", "copy"],
) -> int:
    """Stage only the images referenced by `wanted` into `dst_dir`. Returns count."""
    n = 0
    for name in wanted:
        src = src_dir / name
        if not src.exists():
            log.warning("source image missing: %s", src)
            continue
        if src.suffix.lower() not in _IMG_EXTS:
            log.warning("non-image extension, skipping: %s", src)
            continue
        dst = dst_dir / name
        if dst.exists():
            n += 1
            continue
        if mode == "symlink":
            dst.symlink_to(src.resolve())
        else:
            shutil.copy2(src, dst)
        n += 1
    return n


def _resolve_cloud(
    cloud_points: np.ndarray | None, cloud_ply: Path | None
) -> np.ndarray | None:
    if cloud_points is not None:
        return np.asarray(cloud_points, dtype=np.float64).reshape(-1, 3)
    if cloud_ply is not None:
        return load_ply_points(cloud_ply)
    return None
