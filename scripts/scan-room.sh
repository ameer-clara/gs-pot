#!/usr/bin/env bash
# Convert iPhone HEIC photos in images/<room>/ to JPG, then run the gs-pot
# pipeline (COLMAP → Brush) and print the viewer URL.
#
# Usage:
#     ./scripts/scan-room.sh <room>
#     ./scripts/scan-room.sh <room> "<property-name>"
#     ./scripts/scan-room.sh <room> "<property-name>" --quality medium --steps 7000
#
# Anything after the two positional args is forwarded to `python -m gs_pot scan`,
# overriding the defaults below (argparse takes the last occurrence).
#
# Defaults: --quality low --steps 5000 (fast first iteration; ~3-5 min on M4 Max).

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "usage: $0 <room> [property-name] [-- python-m-gs_pot-scan-flags]" >&2
    exit 64
fi

ROOM="$1"; shift
PROPERTY="Test Apt"
# Optional 2nd positional: treat as property name only if it doesn't start with '-'.
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
    PROPERTY="$1"; shift
fi
# Allow a literal `--` separator before extra flags.
if [ "${1:-}" = "--" ]; then shift; fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
IMAGES="$REPO/images/$ROOM"

if [ ! -d "$IMAGES" ]; then
    echo "error: $IMAGES not found" >&2
    echo "       put photos in $REPO/images/$ROOM/ first" >&2
    exit 66
fi

cd "$IMAGES"
shopt -s nullglob
heic_files=(*.HEIC *.heic)
shopt -u nullglob

if [ ${#heic_files[@]} -gt 0 ]; then
    echo "▶ converting ${#heic_files[@]} HEIC → JPG in $IMAGES"
    for f in "${heic_files[@]}"; do
        out="${f%.*}.jpg"
        if command -v sips >/dev/null 2>&1; then
            sips -s format jpeg "$f" --out "$out" >/dev/null && rm "$f"
        elif command -v ffmpeg >/dev/null 2>&1; then
            ffmpeg -y -hide_banner -loglevel error -i "$f" -q:v 2 "$out" && rm "$f"
        else
            echo "error: need sips or ffmpeg in PATH for HEIC conversion" >&2
            exit 69
        fi
    done
fi

img_count=$(find . -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \) | wc -l | tr -d ' ')
echo "  $img_count image(s) ready in $IMAGES"

if [ "$img_count" -eq 0 ]; then
    echo "error: no images in $IMAGES — nothing to scan." >&2
    echo "       are your photos in the parent directory ($(dirname "$IMAGES"))?" >&2
    echo "       e.g.   mv $(dirname "$IMAGES")/*.{HEIC,heic,jpg,JPG} $IMAGES/" >&2
    exit 65
fi
if [ "$img_count" -lt 10 ]; then
    echo "warn: $img_count images is below the 50–150 recommended; COLMAP may fail to reconstruct" >&2
fi

cd "$REPO"

echo
echo "▶ running gs_pot scan  property=\"$PROPERTY\"  scene=\"$ROOM\""
exec uv run python -m gs_pot scan \
    --images "$IMAGES" \
    --property-name "$PROPERTY" \
    --scene-name "$ROOM" \
    --quality low \
    --steps 5000 \
    "$@"
