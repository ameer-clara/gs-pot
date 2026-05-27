from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ScanStatus(str, Enum):
    QUEUED = "queued"
    CAPTURING = "capturing"
    POSES = "poses"
    TRAINING = "training"
    PUSHING = "pushing"
    READY = "ready"
    ERROR = "error"


class ScanSource(str, Enum):
    IMAGES = "images"
    VIDEO = "video"
    GO2 = "go2"
    DIMOS_REPLAY = "dimos_replay"


class Trainer(str, Enum):
    """Gaussian splat trainer. Brush is the default — slow but ships in-repo.
    OpenSplat is the opt-in faster path; needs a one-time cmake build."""

    BRUSH = "brush"
    OPENSPLAT = "opensplat"


# ── Property: a group of scans (one apartment / building / listing) ───────────


class CreatePropertyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    address: str | None = Field(default=None, max_length=512)


class CreatePropertyResponse(BaseModel):
    property_id: str


class Property(BaseModel):
    property_id: str
    name: str
    address: str | None = None
    created_at: datetime


# ── Scan: one capture+train run, produces one .ply ────────────────────────────


class CreateScanRequest(BaseModel):
    property_id: str = Field(min_length=1, description="Parent Property; must already exist.")
    scene_name: str = Field(min_length=1, max_length=128, description="Per-room label, e.g. 'living_room'.")
    source: ScanSource = ScanSource.IMAGES
    images_dir: str | None = None
    video_path: str | None = None
    trainer: Trainer = Trainer.BRUSH
    # `steps` is trainer-specific. Brush converges around 5k–15k. OpenSplat
    # converges around 2k–5k. We don't auto-rescale; pass an appropriate value.
    steps: int = Field(default=7000, ge=500, le=60000)
    quality: str = Field(default="medium", pattern=r"^(low|medium|high|extreme)$")


class CreateScanResponse(BaseModel):
    scan_id: str


class ScanInfo(BaseModel):
    scan_id: str
    property_id: str
    scene_name: str
    source: ScanSource
    status: ScanStatus
    progress: float = 0.0
    scene_url: str | None = None
    thumb_url: str | None = None
    error: str | None = None
    # Robohack `/api/robot/splat` returns {key, id} after a successful push.
    # Populated only when GS_POT_INGEST_URL + GS_POT_INGEST_TOKEN are configured.
    ingest_id: str | None = None
    ingest_key: str | None = None
    created_at: datetime


class PropertyDetail(BaseModel):
    """A Property plus its scans — the natural unit a UI renders."""

    property_id: str
    name: str
    address: str | None
    created_at: datetime
    scans: list[ScanInfo]
