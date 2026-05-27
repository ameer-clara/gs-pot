"""Round-trip + correctness tests for gs_pot.lidar_poses.

What we assert:
  * `write_colmap_workspace` produces a workspace that pycolmap can
    re-open without error (the most common silent-failure mode is a
    malformed rigs/frames file — pycolmap's reader is strict).
  * The number of cameras / frames / images / points round-trips.
  * Pose round-trip is accurate to float32 tolerance (we lose a little
    in the cam-from-world inversion + quaternion compression).
  * `voxel_downsample` collapses duplicates into one representative
    per voxel and is deterministic.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pycolmap
import pytest

from gs_pot.lidar_poses import (
    CameraIntrinsics,
    FramePose,
    _pose_to_rigid3d,
    load_ply_points,
    voxel_downsample,
    write_colmap_workspace,
)


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_fake_image(path: Path) -> None:
    """Write a minimum-viable JPEG so the staging loop accepts the file."""
    # 1-pixel JPEG header. Brush doesn't read images during workspace
    # validation, so content doesn't matter — only that the file exists
    # under the expected name and has a `.jpg` extension.
    path.write_bytes(b"\xff\xd8\xff\xd9")


def _rotation_about_z(theta: float) -> np.ndarray:
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float64)


def _make_pose(theta: float, tx: float, ty: float, tz: float) -> np.ndarray:
    T = np.eye(4, dtype=np.float64)
    T[:3, :3] = _rotation_about_z(theta)
    T[:3, 3] = (tx, ty, tz)
    return T


# ── tests ──────────────────────────────────────────────────────────────────────


def test_voxel_downsample_collapses_duplicates() -> None:
    pts = np.array(
        [
            [0.00, 0.00, 0.00],
            [0.01, 0.01, 0.01],   # same 0.05 voxel as the first
            [0.10, 0.00, 0.00],
            [0.10, 0.00, 0.04],   # same voxel as the third
            [1.00, 1.00, 1.00],
        ],
        dtype=np.float64,
    )
    out = voxel_downsample(pts, voxel_size=0.05)
    assert out.shape == (3, 3)
    # Deterministic: first hit per voxel is kept, input ordering preserved.
    np.testing.assert_allclose(out[0], pts[0])


def test_voxel_downsample_noop_on_zero_voxel() -> None:
    pts = np.random.RandomState(0).randn(10, 3)
    out = voxel_downsample(pts, voxel_size=0.0)
    assert out is pts or np.array_equal(out, pts)


def test_pose_to_rigid3d_inverts_transform() -> None:
    """A round-trip through Rigid3d should map a point in cam-frame back
    into world-frame via the original T_world_cam (within fp32 noise)."""
    T_world_cam = _make_pose(theta=0.3, tx=1.0, ty=2.0, tz=0.5)
    p_cam = np.array([0.1, 0.2, 1.0])
    p_world_expected = (T_world_cam[:3, :3] @ p_cam) + T_world_cam[:3, 3]

    rigid = _pose_to_rigid3d(T_world_cam)
    # Rigid3d.matrix() returns the cam_from_world 3x4 (R | t) form.
    Rt = np.asarray(rigid.matrix())
    R_cw = Rt[:3, :3]
    t_cw = Rt[:3, 3]
    # invert back: p_world = R_cw^T @ (p_cam - t_cw)
    p_world_actual = R_cw.T @ (p_cam - t_cw)
    np.testing.assert_allclose(p_world_actual, p_world_expected, atol=1e-6)


def test_write_colmap_workspace_roundtrips(tmp_path: Path) -> None:
    """Write a 3-frame, 100-point workspace; reopen with pycolmap; check counts."""
    intrinsics = CameraIntrinsics(
        fx=500.0, fy=500.0, cx=320.0, cy=180.0,
        width=640, height=360,
        k1=0.01, k2=-0.01, p1=0.001, p2=0.0,
    )

    # Three poses spaced along +x with slight rotations.
    poses = []
    src_imgs = tmp_path / "src"
    src_imgs.mkdir()
    for i, (theta, tx) in enumerate([(0.0, 0.0), (0.1, 0.5), (0.2, 1.0)], start=1):
        name = f"frame_{i:04d}.jpg"
        _make_fake_image(src_imgs / name)
        poses.append(FramePose(image_name=name, T_world_cam=_make_pose(theta, tx, 0, 0)))

    # 100 random points within a 1m^3 box.
    rng = np.random.default_rng(42)
    cloud = rng.uniform(-0.5, 0.5, size=(100, 3))

    workspace = tmp_path / "ws"
    sparse_dir = write_colmap_workspace(
        workspace=workspace,
        intrinsics=intrinsics,
        poses=poses,
        images_dir=src_imgs,
        cloud_points=cloud,
        voxel_size=0.0,                # 0 = keep all points for assert
        image_link_mode="copy",        # tmp_path may not allow symlinks under pytest
    )

    # On-disk layout matches what gs_pot.poses.run_colmap produces.
    assert (workspace / "images" / "frame_0001.jpg").exists()
    assert (sparse_dir / "cameras.bin").exists()
    assert (sparse_dir / "frames.bin").exists()
    assert (sparse_dir / "images.bin").exists()
    assert (sparse_dir / "points3D.bin").exists()
    assert (sparse_dir / "rigs.bin").exists()

    # Re-open via pycolmap.
    rec = pycolmap.Reconstruction(str(sparse_dir))
    assert len(rec.cameras) == 1
    assert len(rec.rigs) == 1
    assert len(rec.frames) == 3
    assert len(rec.images) == 3
    assert len(rec.points3D) == 100
    assert rec.num_reg_images() == 3

    # Pose accuracy: first frame's cam_from_world should invert to the
    # input T_world_cam within float32 noise. Pulls the pose off the
    # frame's rig_from_world (single-camera trivial rig → cam == rig).
    # Look up by frame_id explicitly — rec.frames is a dict and
    # iteration order is not insertion order in pycolmap.
    frame = rec.frames[1]
    assert frame.has_pose()
    Rt = np.asarray(frame.rig_from_world.matrix())
    R_cw, t_cw = Rt[:3, :3], Rt[:3, 3]
    # invert to get T_world_cam back
    R_wc = R_cw.T
    t_wc = -R_wc @ t_cw
    T_world_cam_rt = np.eye(4)
    T_world_cam_rt[:3, :3] = R_wc
    T_world_cam_rt[:3, 3] = t_wc
    np.testing.assert_allclose(T_world_cam_rt, poses[0].T_world_cam, atol=1e-5)


def test_write_colmap_workspace_voxel_downsampled(tmp_path: Path) -> None:
    """voxel_size>0 must downsample the seeded points; we get fewer rows
    in points3D.bin than we passed in."""
    intrinsics = CameraIntrinsics(
        fx=500.0, fy=500.0, cx=320.0, cy=180.0, width=640, height=360,
    )
    src_imgs = tmp_path / "src"
    src_imgs.mkdir()
    _make_fake_image(src_imgs / "frame_0001.jpg")
    poses = [FramePose(image_name="frame_0001.jpg", T_world_cam=np.eye(4))]

    # 1000 points within a 0.1 m cube → voxel 0.05 collapses to <= 8 cells.
    rng = np.random.default_rng(0)
    cloud = rng.uniform(0, 0.1, size=(1000, 3))

    workspace = tmp_path / "ws"
    sparse_dir = write_colmap_workspace(
        workspace=workspace,
        intrinsics=intrinsics,
        poses=poses,
        images_dir=src_imgs,
        cloud_points=cloud,
        voxel_size=0.05,
        image_link_mode="copy",
    )
    rec = pycolmap.Reconstruction(str(sparse_dir))
    # 0.1m cube / 0.05 voxel = at most 2x2x2 = 8 voxels.
    assert 1 <= len(rec.points3D) <= 8
    assert len(rec.points3D) < 1000


def test_write_colmap_workspace_rejects_both_cloud_inputs(tmp_path: Path) -> None:
    intrinsics = CameraIntrinsics(
        fx=500.0, fy=500.0, cx=320.0, cy=180.0, width=640, height=360,
    )
    src_imgs = tmp_path / "src"
    src_imgs.mkdir()
    _make_fake_image(src_imgs / "frame_0001.jpg")
    poses = [FramePose(image_name="frame_0001.jpg", T_world_cam=np.eye(4))]

    with pytest.raises(ValueError, match="cloud_points OR cloud_ply"):
        write_colmap_workspace(
            workspace=tmp_path / "ws",
            intrinsics=intrinsics,
            poses=poses,
            images_dir=src_imgs,
            cloud_points=np.zeros((3, 3)),
            cloud_ply=tmp_path / "fake.ply",
        )


def test_load_ply_points_rejects_truncated_body(tmp_path: Path) -> None:
    """Header claims 3 verts but only 1 line follows — must raise, not IndexError."""
    p = tmp_path / "bad.ply"
    p.write_text(
        "ply\nformat ascii 1.0\nelement vertex 3\n"
        "property float x\nproperty float y\nproperty float z\nend_header\n"
        "1.0 2.0 3.0\n"
    )
    with pytest.raises(ValueError, match="truncated"):
        load_ply_points(p)


def test_load_ply_points_rejects_short_line(tmp_path: Path) -> None:
    """A body line with only 2 floats — must raise with line number context."""
    p = tmp_path / "short.ply"
    p.write_text(
        "ply\nformat ascii 1.0\nelement vertex 1\n"
        "property float x\nproperty float y\nproperty float z\nend_header\n"
        "1.0 2.0\n"
    )
    with pytest.raises(ValueError, match=r"line \d+"):
        load_ply_points(p)


def test_load_ply_points_ascii(tmp_path: Path) -> None:
    """ASCII PLY reader handles the bare-minimum xyz body."""
    p = tmp_path / "cloud.ply"
    p.write_text(
        "ply\n"
        "format ascii 1.0\n"
        "element vertex 3\n"
        "property float x\n"
        "property float y\n"
        "property float z\n"
        "end_header\n"
        "1.0 2.0 3.0\n"
        "4.0 5.0 6.0\n"
        "7.0 8.0 9.0\n"
    )
    pts = load_ply_points(p)
    assert pts.shape == (3, 3)
    np.testing.assert_allclose(pts[0], [1, 2, 3])
    np.testing.assert_allclose(pts[2], [7, 8, 9])
