"""One-shot CLI for running an end-to-end scan from a folder of images.

Usage:

    python -m gs_pot scan --images ./photos --property-name "Apt 3F" \\
        --scene-name "living_room"

This runs the pipeline in-process (COLMAP → Brush → .ply + thumb) and writes
outputs under ./scenes/<scan_id>/. State is process-local — for *persistent*
multi-property tracking across multiple scans/sessions, run the HTTP server
(`./scripts/dev.sh`) and use the API directly (POST /properties, POST /scans).
"""

import argparse
import logging
import re
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .models import Property, ScanInfo, ScanSource, ScanStatus
from .pipeline import run_scan
from .store import get_property_store, get_store


def _human_size(b: int) -> str:
    if b >= 1024 * 1024:
        return f"{b / 1024 / 1024:.1f} MB"
    if b >= 1024:
        return f"{b / 1024:.0f} KB"
    return f"{b} B"


def _human_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s"


def _count_input_images(image_dir: Path) -> int:
    exts = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
    return sum(
        1
        for p in image_dir.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in exts
    )


def _read_ply_vertex_count(ply_path: Path) -> int | None:
    try:
        with ply_path.open("rb") as f:
            head = f.read(4096).decode("latin-1", errors="ignore")
    except OSError:
        return None
    m = re.search(r"element vertex (\d+)", head)
    return int(m.group(1)) if m else None


def _scan_summary(scan_id: str, args: argparse.Namespace, elapsed_s: float) -> None:
    """Print a one-glance metric table at the end of the scan."""
    workspace = args.scenes_dir / scan_id
    sparse = workspace / "sparse" / "0"
    ply = workspace / "scene.ply"
    n_in = _count_input_images(args.images)

    reg_images = "—"
    points_3d = "—"
    if sparse.exists():
        try:
            import pycolmap

            rec = pycolmap.Reconstruction(str(sparse))
            reg_images = f"{rec.num_reg_images()} / {n_in}"
            points_3d = f"{rec.num_points3D():,}"
        except Exception:  # pycolmap import or read failure shouldn't block summary
            pass

    splats = "—"
    if ply.exists():
        n = _read_ply_vertex_count(ply)
        if n is not None:
            splats = f"{n:,}"

    ply_size = _human_size(ply.stat().st_size) if ply.exists() else "—"

    rows = [
        ("scene", f"{args.scene_name or args.images.name}"),
        ("property", args.property_name),
        ("quality", args.quality),
        ("steps", str(args.steps)),
        ("registered images", reg_images),
        ("3D points (SfM)", points_3d),
        ("splats", splats),
        (".ply size", ply_size),
        ("wall time", _human_duration(elapsed_s)),
    ]
    label_w = max(len(k) for k, _ in rows)

    print()
    print("┌─ scan summary " + "─" * (label_w + 18))
    for k, v in rows:
        print(f"│  {k:<{label_w}}  {v}")
    print("└" + "─" * (label_w + 33))


def _add_scan_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("scan", help="end-to-end: images → COLMAP → Brush → .ply")
    p.add_argument("--images", required=True, type=Path, help="folder of input images")
    p.add_argument(
        "--property-name",
        required=True,
        help="property (apartment / listing) name — grouped in-process for this run",
    )
    p.add_argument(
        "--scene-name",
        default=None,
        help="per-room label, e.g. 'living_room'. Defaults to the images folder name.",
    )
    p.add_argument(
        "--scenes-dir",
        type=Path,
        default=Path("scenes"),
        help="where to write workspaces + outputs (default: ./scenes)",
    )
    p.add_argument("--steps", type=int, default=7000, help="Brush training steps")
    p.add_argument(
        "--quality",
        default="medium",
        choices=["low", "medium", "high", "extreme"],
        help="COLMAP reconstruction quality preset",
    )


def _cmd_scan(args: argparse.Namespace) -> int:
    if not args.images.exists() or not args.images.is_dir():
        print(f"error: --images is not a directory: {args.images}", file=sys.stderr)
        return 2

    property_id = f"prop_{uuid.uuid4().hex[:12]}"
    get_property_store().put(
        Property(
            property_id=property_id,
            name=args.property_name,
            address=None,
            created_at=datetime.now(UTC),
        )
    )

    scan_id = f"scn_{uuid.uuid4().hex[:12]}"
    scene_name = args.scene_name or args.images.name
    get_store().put(
        ScanInfo(
            scan_id=scan_id,
            property_id=property_id,
            scene_name=scene_name,
            source=ScanSource.IMAGES,
            status=ScanStatus.QUEUED,
            created_at=datetime.now(UTC),
        )
    )

    print(f"▶ property: {property_id}  ({args.property_name})")
    print(f"  scan:     {scan_id}  ({scene_name})")
    print(f"  images:   {args.images}")
    print(f"  output:   {args.scenes_dir / scan_id}/scene.ply")
    print(f"  steps:    {args.steps}  quality: {args.quality}")
    print()

    t0 = time.monotonic()
    result = run_scan(
        scan_id=scan_id,
        images_dir=args.images,
        scenes_dir=args.scenes_dir,
        steps=args.steps,
        quality=args.quality,
    )
    elapsed = time.monotonic() - t0

    _scan_summary(scan_id, args, elapsed)

    print()
    print(f"  status: {result.status.value}")
    print(f"  ply:    {args.scenes_dir / scan_id / 'scene.ply'}")
    print(f"  thumb:  {args.scenes_dir / scan_id / 'thumb.jpg'}")
    print(f"  view:   http://localhost:8000/web/?scene=/scenes/{scan_id}.ply")
    print()
    print("Start the viewer server with: ./scripts/dev.sh")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gs_pot")
    sub = parser.add_subparsers(dest="cmd", required=True)
    _add_scan_parser(sub)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.cmd == "scan":
        return _cmd_scan(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
