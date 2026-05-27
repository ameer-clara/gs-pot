from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ScanStatus(str, Enum):
    QUEUED = "queued"
    CAPTURING = "capturing"
    POSES = "poses"
    # `SLAM` is the LiDAR-pose equivalent of `POSES` — it surfaces the
    # `mode=lidar` step where we write a COLMAP-binary workspace from a
    # SLAM pose dump instead of running pycolmap. Distinct so the UI can
    # show "writing colmap-bin from lidar" instead of "running colmap".
    SLAM = "slam"
    TRAINING = "training"
    PUSHING = "pushing"
    READY = "ready"
    ERROR = "error"


class PoseMode(str, Enum):
    """How the per-frame camera poses for a scan are produced.

    `colmap` runs pycolmap SfM over the staged images (the legacy path;
    10–40 min on apartments). `lidar` skips SfM and writes a
    COLMAP-binary workspace directly from a robohack-served LiDAR cloud
    + per-frame 6-DoF pose dump (the Tier-A path in
    research/07-lidar-splat-plan.md; ~3-5 min on M4).
    """

    COLMAP = "colmap"
    LIDAR = "lidar"


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
    # Sub-status string for the current stage — e.g. "downloading 5/20",
    # "uploading 6.1 MB", "uploading 6.1 MB (retry 2/4)". Front-end shows
    # this next to the coarse status for fine-grained progress.
    detail: str | None = None
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


# ── Robohack run-processing webhook (called by the front-end on "end run") ────


class ProcessRunRequest(BaseModel):
    """Front-end calls this when the user ends a capture session.

    gs-pot fetches the run's frames from robohack, trains a splat, and POSTs
    the finished `.ply` back to robohack's `/api/robot/splat`.

    `robohack_base` and `ingest_token` are optional in the body — if not
    provided, gs-pot falls back to `GS_POT_ROBOHACK_BASE` and
    `GS_POT_INGEST_TOKEN` env vars set on the Mac running gs-pot. That way
    the browser doesn't have to hold the ingest secret. If both env and body
    are unset, the endpoint returns 400.
    """

    robohack_base: str | None = Field(default=None, description="overrides GS_POT_ROBOHACK_BASE")
    ingest_token: str | None = Field(default=None, description="overrides GS_POT_INGEST_TOKEN")
    scene_name: str | None = Field(default=None, max_length=128)
    trainer: Trainer = Trainer.BRUSH
    # `steps` defaults to None — server picks a trainer-appropriate default
    # (Brush:7000, OpenSplat:2000). Brush panics with `min > max` in its lr
    # schedule when total_steps drops much below ~5000; OpenSplat converges
    # ~3× faster per iteration so 2000 is its sweet spot.
    steps: int | None = Field(default=None, ge=500, le=60000)
    quality: str = Field(default="low", pattern=r"^(low|medium|high|extreme)$")
    # `colmap` (default) preserves today's behavior; `lidar` activates the
    # research/07 Tier-A path: pull pointcloud + poses from robohack, write
    # a COLMAP-binary workspace via lidar_poses.write_colmap_workspace,
    # train with Brush. Skips pycolmap SfM entirely (~5-10× faster).
    mode: PoseMode = PoseMode.COLMAP


class ProcessRunResponse(BaseModel):
    scan_id: str
    queue_depth: int  # 0 = starts now; N = N other jobs are queued/running ahead
