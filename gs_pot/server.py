import logging
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .models import (
    CreatePropertyRequest,
    CreatePropertyResponse,
    CreateScanRequest,
    CreateScanResponse,
    Property,
    PropertyDetail,
    ScanInfo,
    ScanSource,
    ScanStatus,
)
from .pipeline import run_scan
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


def _kick_pipeline(scan_id: str, images_dir: Path, steps: int, quality: str) -> None:
    """Run the pipeline in a background thread. Errors land in the store."""

    def _worker() -> None:
        try:
            run_scan(
                scan_id=scan_id,
                images_dir=images_dir,
                scenes_dir=SCENES_DIR,
                steps=steps,
                quality=quality,  # type: ignore[arg-type]
            )
        except Exception:
            log.exception("[%s] background pipeline failed", scan_id)

    threading.Thread(target=_worker, name=f"pipeline-{scan_id}", daemon=True).start()


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
            _kick_pipeline(scan_id, images_dir, req.steps, req.quality)
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
