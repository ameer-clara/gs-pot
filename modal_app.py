"""Modal deployment for gs-pot — published as the `murobo` Modal app.

Architecture (option 2: spawned worker, persistent status):

    POST /scans  ──▶  web container  ──▶  Function.spawn(run_scan_worker, ...)
                          │                       │
                          │                       ▼
                          │              worker container (timeout=3600)
                          │                       │  COLMAP → train → push → thumb
                          │                       │  writes status to modal.Dict
                          ▼                       │
                     status_dict ◀────────────────┘
                          │
    GET /scans/<id> ──────┘   (web reads from the Dict so status survives
                               web-container scale-down OR a different web
                               container picking up the poll request)

The web FastAPI is intentionally slim — just enough to fire `Function.spawn()`,
read status from the Dict, and serve the .ply / thumb / static viewer from the
Volume. The existing `gs_pot.server` FastAPI app stays untouched for local
dev / ngrok setups.

Image stack:
  base   nvidia/cuda 12.4 devel + Python 3.12 (CUDA at build & runtime)
  apt    libopencv-dev, build-essential, cmake, ffmpeg, libgl1, …
  pip    fastapi, uvicorn, httpx, pillow, pycolmap, pydantic
  build  libtorch cu124 + OpenSplat (cmake, CUDA backend)
  bin    Brush v0.3.0 Linux x86_64 (Ubuntu 22.04 has glibc 2.35, so OK)
  env    GS_POT_SCENES_DIR=/data/scenes, OPENSPLAT_BIN, BRUSH_BIN, LD_LIBRARY_PATH
  vol    murobo-scenes mounted at /data/scenes (per-scan workspaces + outputs)
  gpu    T4 default on workers; web is GPU-less + cheap

Deploy:
    modal deploy modal_app.py --env staging
    # → https://ameer-clara-staging--murobo-web.modal.run
"""

import os
from datetime import datetime, timezone

import modal

app = modal.App("murobo")


def _secret_name() -> str:
    env = os.environ.get("MODAL_ENVIRONMENT", "staging")
    return "murobo-prod" if env in {"main", "production"} else "murobo-staging"


murobo_secret = modal.Secret.from_name(_secret_name())

# libtorch CUDA 12.4 prebuilt for the OpenSplat C++ build.
_LIBTORCH_URL = (
    "https://download.pytorch.org/libtorch/cu124/"
    "libtorch-cxx11-abi-shared-with-deps-2.5.1%2Bcu124.zip"
)

# Brush v0.3.0 Linux x86_64 release — needs glibc ≥ 2.32; Ubuntu 22.04 ships 2.35.
_BRUSH_URL = (
    "https://github.com/ArthurBrussee/brush/releases/download/v0.3.0/"
    "brush-app-x86_64-unknown-linux-gnu.tar.xz"
)

murobo_image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04",
        add_python="3.12",
    )
    .env({"DEBIAN_FRONTEND": "noninteractive", "TZ": "UTC"})
    .apt_install(
        "build-essential", "cmake", "git", "wget", "unzip", "pkg-config",
        "libopencv-dev", "libceres-dev", "libeigen3-dev",
        "libgl1", "ffmpeg",
    )
    .pip_install(
        "fastapi>=0.115.0",
        "uvicorn[standard]>=0.32.0",
        "httpx>=0.27.0",
        "pillow>=10.0.0",
        "pycolmap>=3.10",
        "pydantic>=2.5.0",
        "python-multipart>=0.0.9",
    )
    .run_commands(
        f"wget -q -O /tmp/libtorch.zip '{_LIBTORCH_URL}'",
        "unzip -q /tmp/libtorch.zip -d /opt",
        "rm /tmp/libtorch.zip",
        "git clone --depth 1 https://github.com/pierotofy/OpenSplat /opt/OpenSplat",
        "mkdir -p /opt/OpenSplat/build",
        "cd /opt/OpenSplat/build && "
        "cmake -DCMAKE_PREFIX_PATH=/opt/libtorch -DGPU_RUNTIME=CUDA -DCMAKE_BUILD_TYPE=Release .. && "
        "make -j$(nproc)",
        "install -m755 /opt/OpenSplat/build/opensplat /usr/local/bin/opensplat",
        "ldd /usr/local/bin/opensplat | head -8 || true",
        f"wget -q -O /tmp/brush.tar.xz '{_BRUSH_URL}'",
        "tar -xJf /tmp/brush.tar.xz -C /tmp",
        "install -m755 /tmp/brush-app-x86_64-unknown-linux-gnu/brush_app /usr/local/bin/brush",
        "rm -rf /tmp/brush.tar.xz /tmp/brush-app-x86_64-unknown-linux-gnu",
        "/usr/local/bin/brush --version",
    )
    .env(
        {
            "GS_POT_SCENES_DIR": "/data/scenes",
            "GS_POT_WEB_DIR": "/app/web",
            "GS_POT_LOG_LEVEL": "INFO",
            "GS_POT_SPLIT_LOGS": "0",
            "OPENSPLAT_BIN": "/usr/local/bin/opensplat",
            "BRUSH_BIN": "/usr/local/bin/brush",
            "LD_LIBRARY_PATH": "/opt/libtorch/lib",
        }
    )
    .add_local_dir("gs_pot", remote_path="/app/gs_pot")
    .add_local_dir("web", remote_path="/app/web")
)


scenes_volume = modal.Volume.from_name("murobo-scenes", create_if_missing=True)

# Cross-container status store. Each entry keyed by scan_id holds a JSON-shaped
# dict with status/progress/detail/scene_url/thumb_url/error/timestamps.
# Survives web-container scale-down and worker handoff.
status_dict = modal.Dict.from_name("murobo-status", create_if_missing=True)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# WORKER: the actual pipeline. Lives in its own container with a long timeout
# so web-side scale-down can't kill it.
# ─────────────────────────────────────────────────────────────────────────────
@app.function(
    image=murobo_image,
    gpu="T4",
    cpu=16.0,
    memory=16384,
    secrets=[murobo_secret],
    timeout=3600,
    volumes={"/data/scenes": scenes_volume},
)
def run_scan_worker(
    scan_id: str,
    images_dir: str,
    scene_name: str,
    trainer: str,
    steps: int,
    quality: str,
    push_to_robohack: bool = False,
) -> None:
    """Per-scan worker. Runs COLMAP + train + optional push + thumb.

    Status updates flow via `status_dict[scan_id]` so the web container can
    serve `GET /scans/<id>` from a different container (or after a scale-down).
    """
    import sys
    sys.path.insert(0, "/app")

    from pathlib import Path

    # Pick up writes that landed AFTER this container booted — e.g. when a
    # dataset was uploaded between deploy and scan launch.
    scenes_volume.reload()

    def _patch(**changes: object) -> None:
        cur = dict(status_dict.get(scan_id) or {})
        cur.update(changes)
        cur["updated_at"] = _utcnow_iso()
        status_dict[scan_id] = cur

    try:
        # Monkey-patch gs_pot.store so the existing pipeline.run_scan writes
        # status through this Dict-backed shim instead of an in-process dict.
        from gs_pot import pipeline as pipeline_mod
        from gs_pot.models import ScanInfo, ScanStatus

        class _DictStore:
            def get(self, sid: str):
                data = status_dict.get(sid)
                if data is None:
                    return None
                return ScanInfo(**data)

            def put(self, info: ScanInfo) -> None:
                status_dict[info.scan_id] = info.model_dump(mode="json")

        class _NoopProps:
            def get(self, _pid):
                return None

        pipeline_mod.get_store = lambda: _DictStore()
        pipeline_mod.get_property_store = lambda: _NoopProps()

        # Seed a fresh ScanInfo so pipeline._patch can find it.
        seed = ScanInfo(
            scan_id=scan_id,
            property_id="modal-spawned",
            scene_name=scene_name,
            source="images",
            status=ScanStatus.QUEUED,
            progress=0.0,
            created_at=datetime.now(timezone.utc),
        )
        status_dict[scan_id] = seed.model_dump(mode="json")

        push_url = None
        push_token = None
        if push_to_robohack:
            push_url = (os.environ["GS_POT_ROBOHACK_BASE"].rstrip("/")
                        + "/api/robot/splat")
            push_token = os.environ["GS_POT_INGEST_TOKEN"]

        pipeline_mod.run_scan(
            scan_id=scan_id,
            images_dir=Path(images_dir),
            scenes_dir=Path(os.environ["GS_POT_SCENES_DIR"]),
            steps=steps,
            quality=quality,
            trainer=trainer,
            ingest_url=push_url,
            ingest_token=push_token,
        )
        # pipeline.run_scan already wrote status=ready on success.
        scenes_volume.commit()
    except Exception as exc:
        import traceback
        _patch(
            status="error",
            error=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc()[-2000:],
        )
        scenes_volume.commit()
        raise


# ─────────────────────────────────────────────────────────────────────────────
# WEB: slim FastAPI. POST /scans spawns a worker; GET endpoints read from the
# Dict and Volume.
# ─────────────────────────────────────────────────────────────────────────────
@app.function(
    image=murobo_image,
    # No GPU on the web tier — it just routes.
    cpu=2.0,
    memory=4096,
    secrets=[murobo_secret],
    timeout=600,
    scaledown_window=120,
    volumes={"/data/scenes": scenes_volume},
)
@modal.asgi_app()
def web():
    import sys
    sys.path.insert(0, "/app")

    import uuid
    from pathlib import Path

    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel

    SCENES_DIR = Path(os.environ["GS_POT_SCENES_DIR"])
    WEB_DIR = Path(os.environ["GS_POT_WEB_DIR"])

    api = FastAPI(title="murobo")
    api.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    class ScanRequest(BaseModel):
        images_dir: str
        scene_name: str = "scene"
        trainer: str = "brush"
        steps: int | None = None
        quality: str = "medium"
        push: bool = False

    _DEFAULT_STEPS = {"brush": 7000, "opensplat": 2000}

    @api.post("/scans")
    async def post_scan(req: ScanRequest):
        scan_id = f"scn_{uuid.uuid4().hex[:12]}"
        steps = req.steps or _DEFAULT_STEPS.get(req.trainer, 5000)
        status_dict[scan_id] = {
            "scan_id": scan_id,
            "scene_name": req.scene_name,
            "trainer": req.trainer,
            "steps": steps,
            "quality": req.quality,
            "status": "queued",
            "progress": 0.0,
            "detail": None,
            "scene_url": None,
            "thumb_url": None,
            "error": None,
            "created_at": _utcnow_iso(),
            "updated_at": _utcnow_iso(),
        }
        run_scan_worker.spawn(
            scan_id=scan_id,
            images_dir=req.images_dir,
            scene_name=req.scene_name,
            trainer=req.trainer,
            steps=steps,
            quality=req.quality,
            push_to_robohack=req.push,
        )
        return {"scan_id": scan_id}

    @api.get("/scans/{scan_id}")
    async def get_scan(scan_id: str):
        rec = status_dict.get(scan_id)
        if rec is None:
            return JSONResponse({"detail": "scan not found"}, status_code=404)
        return rec

    @api.get("/scans")
    async def list_scans():
        # Modal Dict isn't natively iterable; return a hint instead.
        return {"detail": "GET /scans/{scan_id} for a specific record"}

    @api.get("/scenes/{scan_id}.ply")
    async def get_ply(scan_id: str):
        # Reload volume so we see writes from the worker container.
        scenes_volume.reload()
        p = SCENES_DIR / scan_id / "scene.ply"
        if not p.exists():
            raise HTTPException(404, detail=f"{p} not found")
        return FileResponse(p, media_type="application/octet-stream")

    @api.get("/scenes/{scan_id}/thumb.jpg")
    async def get_thumb(scan_id: str):
        scenes_volume.reload()
        p = SCENES_DIR / scan_id / "thumb.jpg"
        if not p.exists():
            raise HTTPException(404, detail=f"{p} not found")
        return FileResponse(p, media_type="image/jpeg")

    api.mount("/web", StaticFiles(directory=WEB_DIR, html=True), name="web")

    @api.get("/")
    async def root():
        return {
            "service": "murobo",
            "endpoints": {
                "POST /scans": "spawn a scan job",
                "GET /scans/{id}": "live status",
                "GET /scenes/{id}.ply": "trained splat",
                "GET /scenes/{id}/thumb.jpg": "thumbnail",
                "GET /web/?scene=/scenes/{id}.ply": "Spark + WebXR viewer",
            },
        }

    return api
