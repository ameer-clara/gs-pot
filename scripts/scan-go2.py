"""One-off pipeline tuned for Go2 panorama-style captures.

The default `scan-room.sh` quality presets target handheld phone walks: a few
hundred features per image, multi-camera intrinsics. That's wrong for the Go2's
3-positions × 36-angles pattern, where:
  - The camera is the same every shot (single_camera=True).
  - Native res is 640×360, so SIFT needs more features per image to find any
    cross-position matches.
  - Wider FOV → OPENCV model (k1,k2,p1,p2) > SIMPLE_RADIAL.
  - Guided matching helps when texture is repetitive (rooms, hallways).

Run: `uv run python scripts/scan-go2.py <room>`
"""

from __future__ import annotations

import logging
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pycolmap

from gs_pot.models import Property, ScanInfo, ScanSource, ScanStatus
from gs_pot.poses import _align_to_gravity, _stage_images
from gs_pot.store import get_property_store, get_store
from gs_pot.thumb import make_thumbnail
from gs_pot.train import run_brush

log = logging.getLogger("scan-go2")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: scan-go2.py <room> [property-name]", file=sys.stderr)
        return 64
    room = argv[1]
    property_name = argv[2] if len(argv) > 2 else "Go2 Test"

    images_dir = Path("images") / room
    if not images_dir.is_dir():
        print(f"error: {images_dir} not found", file=sys.stderr)
        return 66

    scan_id = f"scn_{uuid.uuid4().hex[:12]}"
    workspace = Path("scenes") / scan_id
    workspace.mkdir(parents=True, exist_ok=True)

    # Register with the in-process store so the viewer URL works.
    property_id = f"prop_{uuid.uuid4().hex[:12]}"
    get_property_store().put(
        Property(property_id=property_id, name=property_name, address=None, created_at=datetime.now(UTC))
    )
    get_store().put(
        ScanInfo(
            scan_id=scan_id, property_id=property_id, scene_name=room,
            source=ScanSource.IMAGES, status=ScanStatus.POSES,
            created_at=datetime.now(UTC),
        )
    )

    print(f"▶ scan_id: {scan_id}  property: {property_id}")
    print(f"  images:  {images_dir}")
    print(f"  output:  {workspace}/scene.ply")

    ws_images = workspace / "images"
    n = _stage_images(images_dir, ws_images)
    print(f"  staged:  {n} images")

    database = workspace / "database.db"
    if database.exists():
        database.unlink()

    # ── Feature extraction (Go2-tuned) ────────────────────────────────────
    reader_opts = pycolmap.ImageReaderOptions()
    reader_opts.camera_model = "OPENCV"  # k1,k2,p1,p2 — better for wide-FOV
    ext_opts = pycolmap.FeatureExtractionOptions()
    ext_opts.max_image_size = 1024  # don't downscale 640×360 input
    ext_opts.sift.max_num_features = 16384  # 8× the "low" preset

    print("▶ extract_features (OPENCV, single_camera, max_features=16384)")
    pycolmap.extract_features(
        database_path=database,
        image_path=ws_images,
        camera_mode=pycolmap.CameraMode.SINGLE,  # shared intrinsics — key for Go2
        reader_options=reader_opts,
        extraction_options=ext_opts,
        device=pycolmap.Device.cpu,
    )

    # ── Matching (guided helps with repetitive room textures) ─────────────
    match_opts = pycolmap.FeatureMatchingOptions()
    match_opts.guided_matching = True
    print("▶ match_exhaustive (guided=True)")
    pycolmap.match_exhaustive(
        database_path=database,
        matching_options=match_opts,
        device=pycolmap.Device.cpu,
    )

    # ── Incremental mapping ──────────────────────────────────────────────
    sparse_root = workspace / "sparse"
    sparse_root.mkdir(exist_ok=True)
    map_opts = pycolmap.IncrementalPipelineOptions()
    map_opts.ba_local_max_num_iterations = 20
    map_opts.ba_global_max_num_iterations = 75
    # Be lenient — we expect weak baselines from same-position rotations.
    # Default thresholds discard small reconstructions; the Go2 capture
    # produces multiple small clusters that we'd rather keep + merge.
    map_opts.min_model_size = 3
    map_opts.min_num_matches = 10
    map_opts.mapper.init_min_num_inliers = 30
    map_opts.mapper.abs_pose_min_num_inliers = 10
    map_opts.mapper.init_min_tri_angle = 1.0  # was 16 deg — allow narrower baselines

    print("▶ incremental_mapping (lenient registration thresholds)")
    reconstructions = pycolmap.incremental_mapping(
        database_path=database,
        image_path=ws_images,
        output_path=sparse_root,
        options=map_opts,
    )

    models = sorted(p for p in sparse_root.iterdir() if p.is_dir())
    if not models:
        print("error: COLMAP produced no sparse model", file=sys.stderr)
        return 1
    model = max(models, key=lambda d: len(pycolmap.Reconstruction(str(d)).images))
    if model.name != "0":
        target = sparse_root / "0"
        if target.exists():
            target.rename(sparse_root / f"0.discarded_{target.stat().st_mtime_ns}")
        model.rename(target)
        model = target
    rec = pycolmap.Reconstruction(str(model))
    regd = sum(1 for img in rec.images.values() if img.has_pose)
    print(f"  registered: {regd}/{len(rec.images)}  points: {len(rec.points3D)}  reconstructions: {len(reconstructions)}  (kept largest)")

    if regd < 10:
        print(f"⚠ only {regd} cameras registered — splat will be sparse but continuing")

    print("▶ gravity align")
    _align_to_gravity(model)

    print("▶ Brush train (5000 steps)")
    run_brush(workspace, workspace, steps=5000, export_name="scene.ply")

    make_thumbnail(images_dir, workspace / "thumb.jpg")

    get_store().put(
        get_store().get(scan_id).model_copy(update={
            "status": ScanStatus.READY,
            "progress": 1.0,
            "scene_url": f"/scenes/{scan_id}.ply",
            "thumb_url": f"/scenes/{scan_id}/thumb.jpg",
        })
    )

    print()
    print(f"✓ DONE  scan_id={scan_id}  registered={regd}/{len(rec.images)}")
    print(f"  view: http://localhost:8000/web/?scene=/scenes/{scan_id}.ply")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sys.exit(main(sys.argv))
