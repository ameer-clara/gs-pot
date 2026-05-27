import logging
import os
import queue as _queue_mod
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Configure gs_pot.* logging at module import. Uvicorn sets up its own
# uvicorn.* handlers but leaves the root logger alone, so without this our
# `log = logging.getLogger(__name__)` lines silently drop on the floor when
# the server runs in background-worker threads. Pin the level via
# GS_POT_LOG_LEVEL (default INFO).
logging.basicConfig(
    level=os.environ.get("GS_POT_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s  %(message)s",
)

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .models import (
    CreatePropertyRequest,
    CreatePropertyResponse,
    CreateScanRequest,
    CreateScanResponse,
    PoseMode,
    ProcessRunRequest,
    ProcessRunResponse,
    Property,
    PropertyDetail,
    ScanInfo,
    ScanSource,
    ScanStatus,
    Trainer,
)
from .pipeline import run_scan
from .runs import fetch_run
from .store import get_property_store, get_store

log = logging.getLogger(__name__)

SCENES_DIR = Path(os.environ.get("GS_POT_SCENES_DIR", "scenes"))
WEB_DIR = Path(os.environ.get("GS_POT_WEB_DIR", "web"))

app = FastAPI(
    title="gs-pot",
    version="0.1.0",
    description=(
        "Robot-scanned Gaussian splats — producer API.\n\n"
        "**Domain model.** A `Property` is one apartment/listing/building and "
        "groups N `Scan`s (one per room). Each `Scan` produces a single `.ply` "
        "served at `/scenes/{scan_id}.ply`. Front-end consumers should typically "
        "render at the `Property` level (`GET /properties/{id}` returns the property "
        "plus all its scans). The OpenAPI spec at `/openapi.json` is the contract."
    ),
)

# CORS open: this server runs locally + is exposed via ngrok to the robohack
# front-end. Hackathon trust model — lock down for production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Single-worker FIFO job queue ──────────────────────────────────────────────


class _JobQueue:
    """One daemon thread drains submitted jobs serially.

    We deliberately serialize because every scan saturates the same CPU/GPU.
    The `depth()` value lets callers report queue position to users.
    """

    def __init__(self) -> None:
        self._q: _queue_mod.Queue[tuple[Callable[..., Any], tuple, dict]] = _queue_mod.Queue()
        self._depth = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True, name="gs-pot-worker").start()

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> int:
        with self._lock:
            self._depth += 1
            depth_after_submit = self._depth
        self._q.put((fn, args, kwargs))
        return depth_after_submit

    def depth(self) -> int:
        with self._lock:
            return self._depth

    def _run(self) -> None:
        while True:
            fn, args, kwargs = self._q.get()
            try:
                fn(*args, **kwargs)
            except Exception:
                log.exception("worker job failed")
            finally:
                with self._lock:
                    self._depth -= 1


_job_queue = _JobQueue()


def get_job_queue() -> _JobQueue:
    """Tests + callers reach in via this accessor."""
    return _job_queue


# ── Property endpoints ────────────────────────────────────────────────────────


@app.post("/properties", response_model=CreatePropertyResponse, status_code=201)
def create_property(req: CreatePropertyRequest) -> CreatePropertyResponse:
    property_id = f"prop_{uuid.uuid4().hex[:12]}"
    prop = Property(
        property_id=property_id,
        name=req.name,
        address=req.address,
        created_at=datetime.now(UTC),
    )
    get_property_store().put(prop)
    return CreatePropertyResponse(property_id=property_id)


@app.get("/properties", response_model=list[PropertyDetail])
def list_properties() -> list[PropertyDetail]:
    out: list[PropertyDetail] = []
    for p in get_property_store().list_all():
        scans = get_store().list_for_property(p.property_id)
        out.append(PropertyDetail(**p.model_dump(), scans=scans))
    return out


@app.get("/properties/{property_id}", response_model=PropertyDetail)
def get_property(property_id: str) -> PropertyDetail:
    p = get_property_store().get(property_id)
    if p is None:
        raise HTTPException(status_code=404, detail="property not found")
    scans = get_store().list_for_property(property_id)
    return PropertyDetail(**p.model_dump(), scans=scans)


# ── Scan endpoints ────────────────────────────────────────────────────────────


def _scan_job(
    scan_id: str,
    images_dir: Path,
    steps: int,
    quality: str,
    trainer: str,
    ingest_url: str | None = None,
    ingest_token: str | None = None,
) -> None:
    try:
        run_scan(
            scan_id=scan_id,
            images_dir=images_dir,
            scenes_dir=SCENES_DIR,
            steps=steps,
            quality=quality,  # type: ignore[arg-type]
            trainer=trainer,  # type: ignore[arg-type]
            ingest_url=ingest_url,
            ingest_token=ingest_token,
        )
    except Exception:
        log.exception("[%s] background pipeline failed", scan_id)


@app.post("/scans", response_model=CreateScanResponse, status_code=202)
def create_scan(req: CreateScanRequest) -> CreateScanResponse:
    if get_property_store().get(req.property_id) is None:
        raise HTTPException(status_code=400, detail=f"property_id not found: {req.property_id}")

    scan_id = f"scn_{uuid.uuid4().hex[:12]}"
    info = ScanInfo(
        scan_id=scan_id,
        property_id=req.property_id,
        scene_name=req.scene_name,
        source=req.source,
        status=ScanStatus.QUEUED,
        created_at=datetime.now(UTC),
    )
    get_store().put(info)

    if req.source == ScanSource.IMAGES and req.images_dir:
        images_dir = Path(req.images_dir)
        if images_dir.is_dir():
            _job_queue.submit(
                _scan_job, scan_id, images_dir, req.steps, req.quality, req.trainer.value
            )
        else:
            get_store().put(
                info.model_copy(
                    update={
                        "status": ScanStatus.ERROR,
                        "error": f"images_dir not found: {images_dir}",
                    }
                )
            )

    return CreateScanResponse(scan_id=scan_id)


@app.get("/scans/{scan_id}", response_model=ScanInfo)
def get_scan(scan_id: str) -> ScanInfo:
    info = get_store().get(scan_id)
    if info is None:
        raise HTTPException(status_code=404, detail="scan not found")
    return info


# ── Robohack run-processing webhook ───────────────────────────────────────────


def _process_run_job(
    *,
    scan_id: str,
    run_id: str,
    robohack_base: str,
    ingest_token: str,
    trainer: str,
    steps: int,
    quality: str,
    mode: str = "colmap",
) -> None:
    """Pull a robohack run's frames, train a splat, push the .ply back.

    `mode` ∈ {"colmap", "lidar"} routes the pose-recovery step. `lidar`
    additionally pulls the LiDAR cloud + per-frame poses from robohack
    and skips pycolmap SfM; see `pipeline.run_scan` for details.
    """
    images_src = SCENES_DIR / scan_id / "images_src"

    def _patch_status(**changes: Any) -> None:
        info = get_store().get(scan_id)
        if info is not None:
            get_store().put(info.model_copy(update=changes))

    def _on_download(i: int, n: int) -> None:
        _patch_status(detail=f"downloading frame {i}/{n}")

    try:
        _patch_status(status=ScanStatus.CAPTURING, progress=0.05, detail="downloading")

        n = fetch_run(robohack_base, run_id, images_src, on_progress=_on_download)
        log.info("[%s] downloaded %d frames from run %s", scan_id, n, run_id)
        if n == 0:
            raise RuntimeError(f"no frames found for run {run_id}")
        _patch_status(detail=None)  # clear download detail before pipeline takes over

        run_scan(
            scan_id=scan_id,
            images_dir=images_src,
            scenes_dir=SCENES_DIR,
            steps=steps,
            quality=quality,  # type: ignore[arg-type]
            trainer=trainer,  # type: ignore[arg-type]
            ingest_url=f"{robohack_base.rstrip('/')}/api/robot/splat",
            ingest_token=ingest_token,
            run_id=run_id,
            mode=PoseMode(mode),
            robohack_base=robohack_base,
            go2_mode=True,  # webhook flow == Go2 capture: single camera, low-res, wide FOV
        )
    except Exception as exc:
        log.exception("[%s] run %s processing failed", scan_id, run_id)
        info = get_store().get(scan_id)
        if info is not None:
            get_store().put(
                info.model_copy(update={"status": ScanStatus.ERROR, "error": str(exc)})
            )


@app.post(
    "/api/runs/{run_id}/process",
    response_model=ProcessRunResponse,
    status_code=202,
)
def process_run(run_id: str, req: ProcessRunRequest) -> ProcessRunResponse:
    """Front-end calls this on 'end run'.

    We respond 202 immediately and process in the background: download the
    run's frames from robohack, train a splat, POST the .ply back.
    A `Property` keyed off the run is auto-created in our in-process store so
    the scan threads through the existing Property/Scan model.

    `robohack_base` + `ingest_token` are read from the request body if present,
    otherwise from `GS_POT_ROBOHACK_BASE` / `GS_POT_INGEST_TOKEN` env vars.
    Keeping the token in the Mac's env (not the browser) avoids leaking it.
    """
    robohack_base = req.robohack_base or os.environ.get("GS_POT_ROBOHACK_BASE")
    ingest_token = req.ingest_token or os.environ.get("GS_POT_INGEST_TOKEN")
    if not robohack_base or not ingest_token:
        raise HTTPException(
            status_code=400,
            detail=(
                "robohack_base + ingest_token required (in request body, "
                "or via GS_POT_ROBOHACK_BASE + GS_POT_INGEST_TOKEN env on the server)"
            ),
        )

    property_id = f"prop_run_{run_id[:12]}"
    if get_property_store().get(property_id) is None:
        get_property_store().put(
            Property(
                property_id=property_id,
                name=f"run:{run_id}",
                address=None,
                created_at=datetime.now(UTC),
            )
        )

    scan_id = f"scn_{uuid.uuid4().hex[:12]}"
    scene_name = req.scene_name or f"run-{run_id[:12]}"
    get_store().put(
        ScanInfo(
            scan_id=scan_id,
            property_id=property_id,
            scene_name=scene_name,
            source=ScanSource.GO2,
            status=ScanStatus.QUEUED,
            created_at=datetime.now(UTC),
        )
    )

    # Trainer-aware steps default. Brush's learning-rate schedule panics
    # (`min > max`) when total_steps is much below ~5000; OpenSplat is fine
    # at 2000. Caller can still override either by passing `steps` in the body.
    steps = req.steps if req.steps is not None else (
        2000 if req.trainer == Trainer.OPENSPLAT else 7000
    )

    depth = _job_queue.submit(
        _process_run_job,
        scan_id=scan_id,
        run_id=run_id,
        robohack_base=robohack_base,
        ingest_token=ingest_token,
        trainer=req.trainer.value,
        steps=steps,
        quality=req.quality,
        mode=req.mode.value,
    )
    return ProcessRunResponse(scan_id=scan_id, queue_depth=depth)


# ── Scene asset endpoints ─────────────────────────────────────────────────────


@app.get("/scenes", response_model=list[ScanInfo])
def list_scenes() -> list[ScanInfo]:
    """Flat list of every READY scan across all properties."""
    return get_store().list_ready()


@app.get("/scenes/{scan_id}.ply")
def get_scene_ply(scan_id: str) -> FileResponse:
    path = SCENES_DIR / scan_id / "scene.ply"
    if not path.exists():
        raise HTTPException(status_code=404, detail="scene file not found")
    return FileResponse(path, media_type="application/octet-stream", filename=f"{scan_id}.ply")


@app.get("/scenes/{scan_id}/thumb.jpg")
def get_scene_thumb(scan_id: str) -> FileResponse:
    path = SCENES_DIR / scan_id / "thumb.jpg"
    if not path.exists():
        raise HTTPException(status_code=404, detail="thumbnail not found")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


# Static mounts last so they don't shadow the API routes above.
if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=WEB_DIR, html=True), name="web")
