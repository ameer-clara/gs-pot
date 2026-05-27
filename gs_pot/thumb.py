"""Scene thumbnail. Takes the first image in the input folder and saves a JPEG."""

import logging
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def make_thumbnail(image_dir: Path, output: Path, *, size: int = 512) -> Path:
    images = sorted(p for p in image_dir.iterdir() if p.suffix.lower() in _IMG_EXTS)
    if not images:
        raise FileNotFoundError(f"no images in {image_dir}")
    img = Image.open(images[0])
    img.thumbnail((size, size))
    output.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(output, "JPEG", quality=85)
    log.info("thumbnail: %s", output)
    return output
