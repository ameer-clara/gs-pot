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
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .models import Property, ScanInfo, ScanSource, ScanStatus
from .pipeline import run_scan
from .store import get_property_store, get_store


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

    result = run_scan(
        scan_id=scan_id,
        images_dir=args.images,
        scenes_dir=args.scenes_dir,
        steps=args.steps,
        quality=args.quality,
    )

    print()
    print(f"✓ DONE — status={result.status.value}")
    print(f"  ply:   {args.scenes_dir / scan_id / 'scene.ply'}")
    print(f"  thumb: {args.scenes_dir / scan_id / 'thumb.jpg'}")
    print(f"  view:  http://localhost:8000/web/?scene=/scenes/{scan_id}.ply")
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
