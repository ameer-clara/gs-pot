#!/usr/bin/env bash
# Extract frames from a video into images/<room>/, then run the gs-pot
# pipeline (COLMAP → Brush) and print the viewer URL.
#
# Usage:
#     ./scripts/scan-video.sh <video-path>
#     ./scripts/scan-video.sh <video-path> <room>
#     ./scripts/scan-video.sh <video-path> <room> "<property-name>"
#     ./scripts/scan-video.sh <video-path> <room> "<property-name>" --target-frames 200
#     ./scripts/scan-video.sh <video-path> <room> "<property-name>" --fps 2 -- --steps 7000
#
# Behaviour:
#   - <room> defaults to the video filename stem (lower-cased, sans extension).
#   - <property-name> defaults to "Test Apt".
#   - Frames go into images/<room>/ (created if missing; fails if non-empty
#     unless --force is passed).
#   - By default we sample uniformly to land near --target-frames (default 150).
#     Override with --fps <N> for a fixed extraction rate.
#   - Anything after `--` is forwarded verbatim to `python -m gs_pot scan`.
#
# Defaults match scan-room.sh: --quality low --steps 5000.

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "usage: $0 <video-path> [room] [property-name] [--target-frames N | --fps N] [--force] [-- <gs_pot scan flags>]" >&2
    exit 64
fi

VIDEO="$1"; shift

if [ ! -f "$VIDEO" ]; then
    echo "error: video not found: $VIDEO" >&2
    exit 66
fi

VIDEO_ABS="$(cd "$(dirname "$VIDEO")" && pwd)/$(basename "$VIDEO")"

# Default room = video filename stem, lower-cased.
DEFAULT_ROOM="$(basename "$VIDEO")"
DEFAULT_ROOM="${DEFAULT_ROOM%.*}"
DEFAULT_ROOM="$(printf '%s' "$DEFAULT_ROOM" | tr '[:upper:]' '[:lower:]')"

ROOM="$DEFAULT_ROOM"
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
    ROOM="$1"; shift
fi

PROPERTY="Test Apt"
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
    PROPERTY="$1"; shift
fi

# Local flags consumed by this script (everything else goes to gs_pot scan).
TARGET_FRAMES=150
FPS=""
FORCE=0
PASSTHRU=()

while [ $# -gt 0 ]; do
    case "$1" in
        --target-frames)
            TARGET_FRAMES="$2"; shift 2 ;;
        --fps)
            FPS="$2"; shift 2 ;;
        --force)
            FORCE=1; shift ;;
        --)
            shift; PASSTHRU+=("$@"); break ;;
        *)
            PASSTHRU+=("$1"); shift ;;
    esac
done

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
    echo "error: ffmpeg and ffprobe must be in PATH" >&2
    exit 69
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
IMAGES="$REPO/images/$ROOM"

mkdir -p "$IMAGES"

# Refuse to overwrite an existing non-empty images dir unless --force.
existing=$(find "$IMAGES" -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \) | wc -l | tr -d ' ')
if [ "$existing" -gt 0 ] && [ "$FORCE" -ne 1 ]; then
    echo "error: $IMAGES already contains $existing image(s); pass --force to overwrite" >&2
    exit 73
fi
if [ "$FORCE" -eq 1 ] && [ "$existing" -gt 0 ]; then
    echo "▶ --force: removing $existing existing image(s) from $IMAGES"
    find "$IMAGES" -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \) -delete
fi

# Probe duration so we can pick an extraction fps that lands near TARGET_FRAMES.
DURATION="$(ffprobe -v error -show_entries format=duration -of default=nokey=1:noprint_wrappers=1 "$VIDEO_ABS")"
if [ -z "$DURATION" ] || [ "$DURATION" = "N/A" ]; then
    echo "error: ffprobe could not read duration of $VIDEO_ABS" >&2
    exit 70
fi

if [ -z "$FPS" ]; then
    # FPS = TARGET_FRAMES / duration, clamped to [0.5, 10].
    FPS="$(awk -v t="$TARGET_FRAMES" -v d="$DURATION" 'BEGIN {
        f = t / d;
        if (f < 0.5) f = 0.5;
        if (f > 10)  f = 10;
        printf "%.4f", f;
    }')"
fi

printf '▶ extracting frames from %s\n' "$VIDEO_ABS"
printf '  duration:      %ss\n' "$DURATION"
printf '  extraction fps: %s\n' "$FPS"
printf '  target frames:  ~%s\n' "$TARGET_FRAMES"
printf '  output:         %s/frame_%%04d.jpg\n' "$IMAGES"

# -vsync vfr drops duplicate frames; -q:v 2 = high-quality JPEG (~95).
ffmpeg -y -hide_banner -loglevel error \
    -i "$VIDEO_ABS" \
    -vf "fps=$FPS" \
    -vsync vfr \
    -q:v 2 \
    "$IMAGES/frame_%04d.jpg"

img_count=$(find "$IMAGES" -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \) | wc -l | tr -d ' ')
echo "  $img_count frame(s) extracted"

if [ "$img_count" -eq 0 ]; then
    echo "error: no frames extracted from $VIDEO_ABS" >&2
    exit 65
fi
if [ "$img_count" -lt 10 ]; then
    echo "warn: $img_count frames is below the 50–150 recommended; COLMAP may fail to reconstruct" >&2
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
    "${PASSTHRU[@]}"
