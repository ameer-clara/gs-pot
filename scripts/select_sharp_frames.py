"""Select the sharpest N frames from a directory, with temporal coverage.

Reads every frame_*.jpg in <dir>, scores each by the variance of its
Laplacian (a standard motion-blur metric: lower variance == more blur),
buckets them evenly across the timeline, and keeps the sharpest frame
per bucket. Below an absolute sharpness floor a frame is dropped even if
it's the best in its bucket.

The remaining frames are renumbered contiguously as frame_0001.jpg ...
so downstream COLMAP/Brush sees a clean sequence.

Usage:
    python scripts/select_sharp_frames.py <dir> --keep 150 [--min-score 50]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np


def laplacian_variance(path: Path) -> float:
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return 0.0
    return float(cv2.Laplacian(img, cv2.CV_64F).var())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("directory", type=Path)
    ap.add_argument("--keep", type=int, required=True, help="target frame count")
    ap.add_argument(
        "--min-score",
        type=float,
        default=50.0,
        help="absolute Laplacian-variance floor; frames below this are dropped",
    )
    ap.add_argument(
        "--pattern", default="frame_*.jpg", help="glob for input frames"
    )
    args = ap.parse_args(argv)

    frames = sorted(args.directory.glob(args.pattern))
    if not frames:
        print(f"error: no frames matching {args.pattern} in {args.directory}", file=sys.stderr)
        return 2

    scores = [(p, laplacian_variance(p)) for p in frames]
    n_in = len(scores)
    mean_score = sum(s for _, s in scores) / n_in

    # Step 1: drop frames below the absolute blur floor.
    sharp = [(p, s) for p, s in scores if s >= args.min_score]
    dropped_blur = n_in - len(sharp)

    # Step 2: bucket the sharp set into `keep` segments and pick the sharpest
    # per bucket. Bucketing AFTER filtering means we never lose a bucket to
    # motion blur — temporal coverage degrades gracefully when stretches of
    # the video are too blurry to contribute.
    keep = min(args.keep, len(sharp))
    selected: list[tuple[Path, float]] = []
    if keep == 0:
        pass
    elif keep == len(sharp):
        selected = sharp
    else:
        for i in range(keep):
            lo = (i * len(sharp)) // keep
            hi = ((i + 1) * len(sharp)) // keep
            bucket = sharp[lo:hi]
            if bucket:
                selected.append(max(bucket, key=lambda x: x[1]))

    keep_paths = {p for p, _ in selected}
    dropped_bucket = n_in - len(keep_paths) - dropped_blur

    # Delete unselected frames.
    for p, _ in scores:
        if p not in keep_paths:
            p.unlink()

    # Renumber kept frames contiguously, staged through a temp suffix to
    # avoid name collisions during rename.
    selected_sorted = sorted(selected, key=lambda x: x[0].name)
    tmp_paths: list[tuple[Path, Path]] = []
    for idx, (p, _) in enumerate(selected_sorted, start=1):
        tmp = p.with_name(f"frame_{idx:04d}.jpg.tmp")
        p.rename(tmp)
        tmp_paths.append((tmp, p.with_name(f"frame_{idx:04d}.jpg")))
    for tmp, final in tmp_paths:
        tmp.rename(final)

    kept_scores = np.array([s for _, s in selected])
    print(f"  input frames:    {n_in}")
    print(f"  mean sharpness:  {mean_score:.1f}")
    print(f"  dropped (blur):  {dropped_blur}  (score < {args.min_score})")
    print(f"  dropped (bucket):{dropped_bucket}")
    print(f"  kept:            {len(selected)}")
    if len(kept_scores):
        # Anything within 25% of the floor is "marginal" — useful for
        # answering "what fraction of my kept frames are still soft?".
        marginal_cut = args.min_score * 1.25
        marginal = int((kept_scores < marginal_cut).sum())
        pct = 100.0 * marginal / len(kept_scores)
        print(
            f"  kept sharpness:  min={kept_scores.min():.1f} "
            f"p25={np.percentile(kept_scores, 25):.1f} "
            f"mean={kept_scores.mean():.1f} "
            f"max={kept_scores.max():.1f}"
        )
        print(
            f"  marginal frames: {marginal}/{len(kept_scores)} ({pct:.1f}%) "
            f"within 25% of floor ({args.min_score:.0f}–{marginal_cut:.0f})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
