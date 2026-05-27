# gs-pot

**Robot-scanned Gaussian Splats → VR walkthroughs in the browser.**

A Unitree Go2 (or just a phone) captures images of a room, the pipeline trains
a 3D Gaussian Splat, and a buyer/renter views it in any browser — with WebXR
for Quest 3 / Vision Pro / Cardboard.

Built for the [DIMENSIONAL Hackathon Shanghai][robohack] (May 26–28, 2026) as
a submodule at `packages/gs-pot`.

[robohack]: https://github.com/grmkris/robohack

## Why
The China real-estate VR-tour market is real and proven — Beike/Lianjia ships
~1M VR-shot apartments. The bottleneck is the human shooter with the rig. A
quadruped is the natural shooter: walks itself, runs every day, no schedule.
See [CLAUDE.md](./CLAUDE.md) for the full strategy.

## Module shape

`gs-pot` is the **producer** side. Input: images. Output: a `.ply` Gaussian
splat plus a thumbnail, served over HTTP. A separate front-end (built by a
teammate) consumes the `.ply` URL via [Spark][] or any compatible viewer.

```
images  ──▶  COLMAP poses  ──▶  Brush train  ──▶  .ply + thumb.jpg
                                                      │
                                                      ▼
                                    HTTP: /scenes/<scan_id>.ply
                                          ▲
                            (teammate's front-end loads here)
```

Domain model:

- **Property** (apartment / listing) groups N **Scans** (rooms).
- One Scan → one `.ply` at `/scenes/<scan_id>.ply`.
- Filesystem is flat (`scenes/<scan_id>/`); Property is the logical grouping.

[Spark]: https://github.com/sparkjsdev/spark

## Install

Requirements: macOS or Linux, Python 3.12, [uv][uv].

```bash
# 1. Python deps (creates .venv)
uv sync --extra dev

# 2. COLMAP (sparse SfM for camera poses)
brew install colmap                 # Mac
# Linux:  sudo apt install colmap

# 3. Brush 0.3.0 release binary (the trainer)
cd bin/
curl -sL -o brush.tar.xz https://github.com/ArthurBrussee/brush/releases/download/v0.3.0/brush-app-aarch64-apple-darwin.tar.xz
tar -xJf brush.tar.xz && rm brush.tar.xz
ln -sf brush-app-aarch64-apple-darwin/brush_app brush
./brush --version                   # → brush-cli 0.3.0
cd ..

# 4. Verify
uv run pytest                       # → 18 passed
```

For Linux x86_64 / Windows, swap the Brush release filename — see
[Brush releases][brush-rel]. Override the binary path with
`BRUSH_BIN=/path/to/brush` if you install elsewhere.

[uv]: https://docs.astral.sh/uv/
[brush-rel]: https://github.com/ArthurBrussee/brush/releases

## Quick start: generate a splat from phone photos

### 1. Capture (50–150 photos of one room)

- Slow walk in a circle; one photo every 1–2 steps (50–70% overlap).
- Two height passes (waist + head) if you can — boosts coverage.
- Landscape orientation, consistent across all shots.
- No moving people / pets. Good even lighting.
- Drop them in `./photos/living_room/` (or anywhere).

### 2. Run the pipeline

```bash
# Default (Brush trainer, medium COLMAP):
uv run python -m gs_pot scan \
    --images ./photos/living_room \
    --property-name "My Apt" \
    --scene-name "living_room"

# Same thing via the wrapper script (also converts HEIC):
./scripts/scan-room.sh living_room "My Apt"

# Faster trainer (once OpenSplat is built — see bin/README.md):
./scripts/scan-room.sh living_room "My Apt" --trainer opensplat --steps 2000
```

Knobs:

| Flag | Default | Notes |
|---|---|---|
| `--trainer {brush,opensplat}` | `brush` | See [Trainer choice](#trainer-choice-brush-vs-opensplat) below. |
| `--quality {low,medium,high,extreme}` | `medium` | COLMAP preset. See [Quality presets](#quality-presets). |
| `--steps N` | trainer default | Brush: `7000`. OpenSplat: `2000`. |
| `--scenes-dir PATH` | `./scenes` | Where the workspace + outputs land. |

Output:

- `scenes/<scan_id>/scene.ply` — the Gaussian splat
- `scenes/<scan_id>/thumb.jpg` — thumbnail
- The CLI prints a viewer URL **and a summary table** with registered-image
  count, 3D-point count, splat count, .ply size, and wall time.

Expected wall time on an M4 Max with ~30–100 photos, `--quality medium`:
- Brush, 7k steps: ~10–30 min
- OpenSplat, 2k steps: ~3–8 min  (3–5× faster)

COLMAP poses are fast (<10 s for 30 photos at `low`); training dominates total time.

## Trainer choice (Brush vs OpenSplat)

Both trainers consume the same COLMAP workspace (`images/` + `sparse/0/*.bin`)
and emit a standard `.ply` Spark can read. They're swappable per-scan via
`--trainer`.

| Concern | Brush 0.3.0 | OpenSplat |
|---|---|---|
| Backend | Rust / WGPU → Metal | C++ / libtorch native Metal |
| Install | release binary in `bin/brush` (works out of the box) | `brew install cmake opencv pytorch` + cmake build, ~10–20 min one-time (see `bin/README.md`) |
| Cross-platform | macOS / Linux / Windows | macOS (MPS) + Linux/Windows (CUDA, ROCm) |
| Speed on M4 Max | 15k steps ≈ 29 min (measured on 9 photos) | 2k steps ≈ ~5 min (3–5× faster reported) |
| Step semantics | gradient steps; converges at 5k–15k | "n iterations"; converges at 2k–5k |
| Default `--steps` | 7000 | 2000 |
| License | Apache-2.0 + MIT | AGPLv3 (commercial use OK) |

**When to use which:**
- **Brush** is the default — always works, no build step. Use it on a fresh
  laptop, in CI, or for sanity-check scans.
- **OpenSplat** is the speed path. Use it once you're iterating (more photos,
  more reruns, more demos). It's also what we'd ship to the venue laptop on
  hackathon day 1 — same Metal backend on Mac, native CUDA on the Linux box.

Override the binary location with `BRUSH_BIN=` / `OPENSPLAT_BIN=` env vars
if you put them somewhere other than `bin/`.

## Quality presets

The `--quality` flag drives COLMAP's SfM, not the splat trainer. It controls
how aggressive feature extraction + bundle adjustment are. Tuned for the
real-estate scan use case (room-scale, mixed lighting):

| Preset | Image size | Max SIFT features | Guided matching | BA iters (local / global) | When |
|---|---|---|---|---|---|
| `low`     | 1000 px | 2048  | off | 12 / 30  | first-pass smoke test; **safe on low-texture scenes (bathrooms, white walls)** |
| `medium`  | 1600 px | 8192  | off | 16 / 50  | default; what most rooms want |
| `high`    | 2400 px | 16384 | on  | 25 / 75  | textured rooms with many photos; slower SfM |
| `extreme` | 3200 px | 32768 | on  | 40 / 100 | maximum quality, OPENCV (5-param) camera model |

**Important counter-intuitive note:** for weak-texture scenes (bathrooms,
white walls, tile, mirrors), `low` often **registers more images than
`medium`+`high`**. The higher presets enable `guided_matching`, which prunes
matches that don't fit the initial epipolar geometry — but the initial
geometry on weak features is noisy, so guided matching over-prunes and the
mapper can fail to find an initial image pair. Start with `low` on tricky
scenes, then promote if everything registers cleanly.

### 3. View it

In another terminal:

```bash
./scripts/dev.sh                    # serves /web/ + the API on :8000
```

Open the URL the CLI printed:

```
http://localhost:8000/web/?scene=/scenes/<scan_id>.ply
```

- Desktop: Chrome 134+ on Mac (Spark uses modern WebGL2). Drag to look, WASD to move.
- VR: open the same URL on Quest 3 / Vision Pro browser, click "Enter VR" (top-right).
- Phone: open in iOS Safari or Android Chrome — viewer works, just no head tracking.

## How the pipeline works

`scan-room.sh` orchestrates the full producer side:

```
./scripts/scan-room.sh <room> "<property-name>" [--trainer brush|opensplat] [--steps N] [--quality low|medium|high|extreme]
    │
    ├─ HEIC → JPG conversion (sips, ffmpeg fallback)  ── skipped if no .HEIC
    ├─ count images, fail loudly if 0
    └─ exec  uv run python -m gs_pot scan ...
                │
                ▼  Python pipeline (gs_pot/pipeline.py)
        ┌───────────────────────────────────────────────────────┐
        │ A. POSES   — gs_pot/poses.py                          │
        │    • symlink every real image (.jpg/.png) into        │
        │      scenes/<id>/images/ (filters out .DS_Store etc.) │
        │    • pycolmap.extract_features                        │
        │    • pycolmap.match_exhaustive                        │
        │    • pycolmap.incremental_mapping                     │
        │    → scenes/<id>/sparse/0/{cameras,images,points3D}.bin│
        ├───────────────────────────────────────────────────────┤
        │ B. TRAINING — gs_pot/train.py (dispatched by trainer) │
        │    --trainer brush     →  bin/brush <ws>  --total-steps N            │
        │    --trainer opensplat →  bin/opensplat <ws>  -n N  -o scene.ply     │
        │    → scenes/<id>/scene.ply                            │
        ├───────────────────────────────────────────────────────┤
        │ C. PUSH    — gs_pot/ingest.py (optional)              │
        │    if GS_POT_INGEST_URL + TOKEN set:                  │
        │      POST .ply to robohack /api/robot/splat (Bearer)  │
        │      → records ingest_id, ingest_key on the scan      │
        ├───────────────────────────────────────────────────────┤
        │ D. THUMB   — gs_pot/thumb.py                          │
        │    first image → scenes/<id>/thumb.jpg                │
        ├───────────────────────────────────────────────────────┤
        │ E. READY   — store status flipped to "ready",         │
        │              scene_url=/scenes/<id>.ply               │
        └───────────────────────────────────────────────────────┘
```

Status flow (read by `GET /scans/<id>`):

```
queued ──▶ poses ──▶ training ──▶ pushing* ──▶ ready
                │                              ↑
                └─────────── error ◀───────────┘   (* skipped if no ingest config)
```

Output sitting on disk for each scan:

```
scenes/<scan_id>/
  database.db    ← COLMAP SfM database (intermediate)
  images/        ← per-file symlinks of the inputs (filtered, no .DS_Store etc.)
  sparse/0/      ← cameras.bin, images.bin, points3D.bin
  scene.ply      ← the Gaussian splat — this is what Spark loads
  thumb.jpg      ← first frame, served at /scenes/<id>/thumb.jpg
```

The `scene.ply` is the boundary file between producer (us) and consumer (the
teammate's front-end / Spark). When `GS_POT_INGEST_URL` is set, the same
`.ply` is also POSTed to robohack's ingest endpoint — see
[Pushing splats to robohack](#pushing-splats-to-robohack-production-integration)
below.

### Why pycolmap, not the colmap CLI

We use [pycolmap](https://github.com/colmap/pycolmap) (pip-installable COLMAP
Python bindings) instead of invoking the `colmap` CLI as a subprocess.
Reason: Homebrew's macOS arm64 build of COLMAP has a deterministic
use-after-free in `Creating SIFT CPU feature matcher` that crashes the
matcher with SIGSEGV. pycolmap's wheels ship their own COLMAP binary built
via cibuildwheel for darwin-arm64 and don't hit the bug. The Python API is
also cleaner — no flag-name-by-version churn.

## End-to-end walkthrough (in detail)

This is the full production loop — from a robot capturing frames to a buyer
viewing the trained splat in VR. Every API call, every env var, every log
line you'd see. The Mermaid diagrams in [FLOWS.md](./FLOWS.md) are the
visual counterpart.

### Cast of characters

| Service | Where | Role |
|---|---|---|
| **robohack `apps/server`** | Railway, behind Caddy gateway | Frames + splats source of truth (Hono on Bun, Postgres for metadata, S3 for blobs) |
| **robohack `apps/gateway`** | Railway | Single public host `https://gateway-production-…railway.app`. Caddy routes `/api/robot/*`, `/api/scans*`, `/api/auth/*`, `/rpc/*`, `/api/upload/*` to apps/server; everything else to apps/web. |
| **robohack `apps/web`** | Railway, behind same gateway | Next.js front-end. "Build splat" button on `/scans` |
| **Go2 + DimOS** | On the venue LAN | Captures images; POSTs each to apps/server `/api/robot/frame` with `run`/`position`/`angle` |
| **gs-pot** *(this repo)* | Your Mac, via ngrok | The reconstruction box. Receives the webhook, downloads frames, runs COLMAP + Brush + gravity-align, pushes the `.ply` back |
| **ngrok** | Your Mac | Public HTTPS tunnel from `https://twilight-getting-possum.ngrok-free.dev` → `localhost:8000` |

### Setup checklist (one-time per dev box)

**On your Mac (running gs-pot):**

```bash
# 1. Python deps + Brush binary — see Install section above.

# 2. .env (gitignored). dev.sh auto-sources it.
cat > .env <<EOF
GS_POT_INGEST_TOKEN=<ROBOT_INGEST_TOKEN from robohack/apps/server>
GS_POT_ROBOHACK_BASE=https://gateway-production-94e2.up.railway.app
EOF

# 3. Start the server.
./scripts/dev.sh                       # FastAPI on 127.0.0.1:8000

# 4. Expose via ngrok in a separate terminal.
ngrok http --url=https://twilight-getting-possum.ngrok-free.dev 8000
```

**On Railway (web service env):**

```
NEXT_PUBLIC_GS_POT_URL=https://twilight-getting-possum.ngrok-free.dev
```

Then trigger a redeploy of the web service so Next.js inlines the var at
build time. Without it the "Build splat" button is silently hidden.

### The flow, step by step

**1. User opens `/scans` in robohack.**

`apps/web` mounts the `ScansBrowser` component which polls oRPC
`frames.scans` every 5s. Page renders each run with its position thumbnails.
If `NEXT_PUBLIC_GS_POT_URL` was baked in at build time, a "Build splat"
button appears on each run header.

**2. Robot captures frames (concurrent with the page being open).**

For every image the Go2 (or any capture client) takes:

```http
POST https://gateway-production-94e2.up.railway.app/api/robot/frame
Authorization: Bearer <ROBOT_INGEST_TOKEN>
Content-Type: multipart/form-data

  file:     <jpeg bytes>
  run:      <session-uuid>
  position: <stop index>     0, 1, 2, …
  angle:    <heading deg>    0.0, 15.0, 30.0, …
  poseX:    <robot x>        optional
  poseY:    <robot y>        optional
```

apps/server validates the token, writes the JPEG to S3, inserts a row in
the `frames` table with `run`/`position`/`angle`/`poseX`/`poseY`/`embedding`.
Responds `200 { "key": "robot/frame_….jpg" }`. The web page picks the new
frames up on its next 5s poll.

**3. User clicks "Build splat" on a run.**

The React component fires:

```http
POST https://twilight-getting-possum.ngrok-free.dev/api/runs/<run_id>/process
Content-Type: application/json
ngrok-skip-browser-warning: true

{}
```

Empty body is fine — gs-pot reads `GS_POT_ROBOHACK_BASE` and
`GS_POT_INGEST_TOKEN` from its `.env`. Browser cannot see those secrets.
`ngrok-skip-browser-warning` bypasses ngrok-free's HTML interstitial that
otherwise returns no CORS headers and confuses the React app.

**4. gs-pot accepts the request.**

`gs_pot/server.py`:
- CORSMiddleware answers the OPTIONS preflight (CORS is open `*`).
- `process_run(run_id, req)`:
  - Reads `GS_POT_ROBOHACK_BASE` + `GS_POT_INGEST_TOKEN` from env (body was empty).
  - Validates both are present — returns `400` with a clear message otherwise.
  - Auto-creates a `Property` in the in-process store keyed off the run id
    (`prop_run_<run_id[:12]>`, name `run:<run_id>`).
  - Mints `scan_id = "scn_<12hex>"`, writes a `ScanInfo{status=queued}` row.
  - Picks trainer-aware default `steps` (Brush:7000, OpenSplat:2000) if the
    body didn't override.
  - Submits the job to `_JobQueue` (single-worker FIFO).
  - Responds `202 { "scan_id": "scn_…", "queue_depth": N }`.

**5. Front-end starts polling.**

The component flips to "queued" state and polls every 3 s:

```http
GET https://twilight-getting-possum.ngrok-free.dev/scans/<scan_id>
ngrok-skip-browser-warning: true
```

gs-pot returns the live `ScanInfo`:

```json
{
  "scan_id": "scn_…", "property_id": "prop_run_…", "scene_name": "run-…",
  "status": "capturing",
  "progress": 0.05,
  "detail": "downloading 12/20",
  "scene_url": null,
  "thumb_url": null,
  "ingest_id": null, "ingest_key": null,
  "error": null,
  "created_at": "2026-05-27T…"
}
```

The button text follows `status` + `detail`:
`queued… → capturing… 5% · downloading 12/20 → poses… 10% → training… 40% → pushing… 85% · uploading 6.1 MB`.

**6. Worker thread picks up the job (single-worker FIFO).**

`_process_run_job(scan_id, run_id, robohack_base, ingest_token, …)`:

  a. Patches status to `capturing`, `progress=0.05`, `detail="downloading"`.

  b. Calls `runs.fetch_run(robohack_base, run_id, scenes/<scan_id>/images_src/)`:
     - `GET <gateway>/api/scans/<run_id>` → JSON tree of positions/images with presigned S3 URLs (6h TTL).
     - For each image: `GET <presigned-url>` → stream to disk as `p<pos>_a<angle>_<id>.jpg`.
     - Calls `on_progress(i, n)` after each → patches `detail="downloading 5/20"`.
     - Skips files already on disk (resume after crash).

  c. Patches `detail=None`, then calls `pipeline.run_scan(scan_id, images_src, …)`:

     **c.1 Poses (`gs_pot/poses.py`)** — patches `status=poses`, `progress=0.1`:
       - Per-file symlinks every real `.jpg/.png` into `scenes/<scan_id>/images/`,
         skipping `.DS_Store` and hidden dirs.
       - `pycolmap.extract_features` (CPU, SIMPLE_RADIAL camera, 2048 SIFT features at `low`).
       - `pycolmap.match_exhaustive` (CPU).
       - `pycolmap.incremental_mapping` → `sparse/0/{cameras,images,points3D}.bin`.
       - `_align_to_gravity(sparse/0)` rotates so cameras' mean "down" maps to world −Y.
       - We use **pycolmap, not the `colmap` CLI** — homebrew's macOS arm64
         COLMAP has a deterministic SIFT-matcher SIGSEGV.

     **c.2 Training (`gs_pot/train.py`)** — patches `status=training`, `progress=0.4`:
       - Spawns `bin/brush <workspace> --total-steps N --export-name scene.ply`.
       - Wall time on M4 Max: ~3 min for 5k steps, ~30 min for 15k steps
         (the per-step cost grows as Brush densifies).
       - Writes `scenes/<scan_id>/scene.ply`.

     **c.3 Push back to robohack (`gs_pot/ingest.py`)** — patches
     `status=pushing`, `progress=0.85`:
       - Builds `name = "<property name> · <scene name>"`, e.g. `"run:scan-…" · "run-scan-1779879441"`.
       - For each attempt (up to 4):
         - Patches `detail="uploading 6.1 MB"` (or `"… (retry 2/4)"`).
         - `POST <gateway>/api/robot/splat` multipart `{file, format, name}` with `Authorization: Bearer <token>`.
         - **Retries** on 502/503/504, ConnectError, ReadTimeout, RemoteProtocolError with exponential backoff (2 s, 4 s, 8 s).
         - **Does not retry** on 401/403/400/413/415 (deliberate refusals).
         - On 200 `{ id: "splat_…", key: "splats/….ply" }`: patches `ingest_id` and `ingest_key` on the scan.

     **c.4 Thumbnail (`gs_pot/thumb.py`)** — first image → `scenes/<scan_id>/thumb.jpg` (512px JPEG).

     **c.5 Done** — patches `status=ready`, `progress=1.0`,
     `scene_url=/scenes/<scan_id>.ply`, `thumb_url=/scenes/<scan_id>/thumb.jpg`.

**7. Front-end sees `status=ready`.**

The polling effect stops, button flips to a green
`View splat ↗` link pointing at
`https://twilight-getting-possum.ngrok-free.dev/web/?scene=/scenes/<scan_id>.ply`.

**8. User clicks "View splat".**

New tab loads gs-pot's static viewer (`web/index.html` + `web/viewer.js`).
Spark + Three.js + WebXR load from CDN via importmap. The viewer reads
`?scene=…` and calls `new SplatMesh({ url })` against gs-pot's
`/scenes/<scan_id>.ply` route, which is a `FileResponse` of the binary.
On Quest 3 / Vision Pro browser the "Enter VR" button appears top-right.

**9. Meanwhile, the splat also lives in robohack.**

Because step 6.c.3 pushed it, apps/server stored it in S3 (`splats/<id>.ply`)
and inserted a row in the `splats` table. The oRPC `splats.list` procedure
serves it to wherever the front-end lists splats — typically with a long
presigned URL TTL so embedded viewers stay valid.

### What you should see in `./scripts/dev.sh`

Restart the server first so it picks up `logging.basicConfig` — without that
the `gs_pot.*` loggers silently drop (uvicorn only configures its own
`uvicorn.*` loggers).

```
2026-05-27 08:45:00 INFO  uvicorn.error           Uvicorn running on http://127.0.0.1:8000
2026-05-27 08:45:01 INFO  uvicorn.access          204.188.233.66:0 - "OPTIONS /api/runs/scan-…/process HTTP/1.1" 200 OK
2026-05-27 08:45:01 INFO  uvicorn.access          204.188.233.66:0 - "POST /api/runs/scan-…/process HTTP/1.1" 202 Accepted
2026-05-27 08:45:01 INFO  gs_pot.runs             fetching run scan-… from https://gateway-…/api/scans/scan-…
2026-05-27 08:45:02 INFO  gs_pot.runs             run scan-…: 120 frames to fetch
2026-05-27 08:45:14 INFO  gs_pot.runs             run scan-…: 120 frames in scenes/scn_…/images_src
2026-05-27 08:45:14 INFO  gs_pot.poses            staged 120 images at scenes/scn_…/images
2026-05-27 08:45:14 INFO  gs_pot.poses            pycolmap: extract_features (max_image_size=1000, max_features=2048, camera=SIMPLE_RADIAL)
2026-05-27 08:45:18 INFO  gs_pot.poses            pycolmap: match_exhaustive (guided=False)
2026-05-27 08:45:22 INFO  gs_pot.poses            pycolmap: incremental_mapping
2026-05-27 08:45:25 INFO  gs_pot.poses            COLMAP sparse model: scenes/scn_…/sparse/0 (1 reconstruction(s) total)
2026-05-27 08:45:25 INFO  gs_pot.poses            gravity-align: rotated 120 cameras + N points (|mean down|=0.97)
2026-05-27 08:45:25 INFO  gs_pot.train            running: bin/brush scenes/scn_… --total-steps 7000 …
2026-05-27 08:50:30 INFO  gs_pot.train            Brush exported: scenes/scn_…/scene.ply
2026-05-27 08:50:30 INFO  gs_pot.ingest           pushing scene.ply (6.1 MB) → https://gateway-…/api/robot/splat  [up to 4 attempts]
2026-05-27 08:50:33 WARN  gs_pot.ingest           push got 502 on attempt 1/4, retrying in 2.0s
2026-05-27 08:50:37 INFO  gs_pot.ingest           ingest accepted on attempt 2/4: id=splat_… key=splats/….ply
2026-05-27 08:50:37 INFO  gs_pot.thumb            thumbnail: scenes/scn_…/thumb.jpg
2026-05-27 08:50:37 INFO  gs_pot.pipeline         [scn_…] DONE
```

### Failure modes + recovery (lessons learned)

| Symptom | Cause | Fix |
|---|---|---|
| `502 Bad Gateway` mid-push | Railway edge proxy hiccup on large multipart upload | Retry-with-backoff in `push_splat` handles it; if it survives 4 attempts, manually re-push the on-disk `.ply` with curl |
| `GET /api/scans/<run>` returns Next.js 404 HTML | Old Caddyfile didn't route `/api/scans*` to apps/server | Fixed on robohack master; redeploy gateway |
| Brush panic `min > max, or either was NaN` | Webhook defaulted `steps=2000`; Brush's lr schedule needs ≥ ~5000 | Fixed: trainer-aware default (Brush:7000, OpenSplat:2000) |
| `httpx.ConnectError: connection refused` in worker logs during tests | Tests post to a fake `localhost:9999`; queue worker logs the expected failure after the test's assertion | Cosmetic; ignore |
| CORS error in browser, response is HTML | ngrok-free interstitial; missing `ngrok-skip-browser-warning` header | Already set in `scans-browser.tsx` fetches |
| Reconstruction is rotated 90° | iPhone photos with EXIF rotation not baked, or COLMAP's gravity prior wrong | `scan-room.sh` runs `PIL.ImageOps.exif_transpose`; `poses._align_to_gravity` rotates the sparse model |
| Bathroom / mirror / tile scene shows mostly floaters | SfM-hostile scene; only a couple images registered | Pick a textured room. Bathrooms are the worst case |
| `--quality medium` rejects all images | At medium we enable `guided_matching`; on weak texture it over-prunes | Drop to `--quality low` or rely on the default we ship |
| Worker is silent in dev.sh | `gs_pot.*` loggers not configured under uvicorn | `server.py` calls `logging.basicConfig` at import; pin level via `GS_POT_LOG_LEVEL` |

### Endpoint quick-reference

```
POST /api/runs/{run_id}/process              ← the webhook
  body (all optional, env fallback exists):
    robohack_base, ingest_token,
    trainer ("brush"|"opensplat"), steps, quality, scene_name
  → 202 { scan_id, queue_depth }

GET  /scans/{scan_id}                        ← live status (poll every 3s)
  → 200 ScanInfo {
      status: queued|capturing|poses|training|pushing|ready|error,
      progress: 0..1,
      detail: "downloading 5/20" | "uploading 6.1 MB (retry 2/4)" | null,
      scene_url, thumb_url, ingest_id, ingest_key, error
    }

GET  /scenes/{scan_id}.ply                   ← static binary (Spark loads this)
GET  /scenes/{scan_id}/thumb.jpg
GET  /web/?scene=/scenes/{scan_id}.ply       ← built-in Spark+WebXR viewer
```

## Pushing splats to robohack (env-based, single scan)

In production gs-pot is **the reconstruction box** for
[robohack](https://github.com/grmkris/robohack). Their `apps/server` exposes
a token-guarded `POST /api/robot/splat` endpoint; their `apps/web` lists the
ingested splats via oRPC and renders them with Spark. gs-pot pushes finished
`.ply` files there.

Set two env vars and the pipeline auto-pushes after every Brush export:

```bash
export GS_POT_INGEST_URL="https://<robohack-server>/api/robot/splat"
export GS_POT_INGEST_TOKEN="<ROBOT_INGEST_TOKEN from robohack/apps/server>"

# Now scans auto-upload:
uv run python -m gs_pot scan \
    --images ./photos/living_room \
    --property-name "Apt 3F" \
    --scene-name "living_room"
# → produces scene.ply, then POSTs to /api/robot/splat with
#   Authorization: Bearer <token>
#   multipart: file=scene.ply, format=ply, name="Apt 3F · living_room"
```

The scan's `ingest_id` and `ingest_key` fields (visible via
`GET /scans/{scan_id}`) record what robohack assigned. If either env var is
missing, the push step is skipped silently — handy for local development.

## Multi-property workflow (server mode)

The CLI is one-shot and process-local. For persistent multi-apartment
tracking — what the teammate's front-end uses — keep the server running
and drive the HTTP API:

```bash
./scripts/dev.sh                    # keep this terminal running

# Create an apartment:
curl -sX POST localhost:8000/properties \
    -H 'content-type: application/json' \
    -d '{"name":"Apt 3F · 123 Main St","address":"123 Main St"}'
# → {"property_id":"prop_abc123def456"}

# Submit a room scan under that property:
curl -sX POST localhost:8000/scans \
    -H 'content-type: application/json' \
    -d '{
      "property_id":"prop_abc123def456",
      "scene_name":"living_room",
      "source":"images",
      "images_dir":"/absolute/path/to/photos/living_room"
    }'
# → {"scan_id":"scn_..."}

# Poll status:
curl -s localhost:8000/scans/scn_... | jq

# Get the whole property + its scans (the front-end's main read):
curl -s localhost:8000/properties/prop_abc123def456 | jq
```

The interactive OpenAPI explorer lives at `http://localhost:8000/docs`. The
raw spec is at `/openapi.json` — point an OpenAPI client generator at it to
scaffold a typed client.

## API contract (summary)

| Verb | Path | Purpose |
|---|---|---|
| `POST` | `/properties` | Create an apartment / listing |
| `GET`  | `/properties` | List all + their scans |
| `GET`  | `/properties/{id}` | One property + its scans |
| `POST` | `/scans` | Start a scan (kicks off the pipeline in a background thread) |
| `GET`  | `/scans/{id}` | Status (`queued` → `poses` → `training` → `ready` / `error`) + `scene_url` once ready |
| `GET`  | `/scenes` | All `ready` scans (flat) |
| `GET`  | `/scenes/{id}.ply` | The splat asset |
| `GET`  | `/scenes/{id}/thumb.jpg` | Thumbnail |

`tests/test_contract.py` is the **live spec** — 18 tests run by
`uv run pytest`. If the teammate's client breaks, one of those should
have failed first.

## Project layout

```
gs_pot/
  models.py          # pydantic types — the contract
  store.py           # in-memory Property + Scan registries
  server.py          # FastAPI: /properties, /scans, /scenes, mounts /web
  pipeline.py        # orchestrator: poses → train → push → thumb → READY
  poses.py           # COLMAP subprocess wrapper (CPU mode, no CUDA)
  train.py           # Brush subprocess wrapper
  ingest.py          # POST .ply to robohack's /api/robot/splat (Bearer auth)
  thumb.py           # first-image → JPG thumbnail
  cli.py             # `python -m gs_pot scan ...`
tests/
  test_contract.py   # producer/consumer API contract tests
  test_ingest.py     # robohack push contract (multipart, Bearer, format/name)
web/                 # Spark 2.0 + WebXR smoke viewer (mounted at /web)
scripts/dev.sh       # uvicorn launcher
bin/                 # external binaries — gitignored, see bin/README.md
scenes/              # per-scan workspace + outputs — gitignored
```

## Stack picks (and why)

| Concern | Pick | Why |
|---|---|---|
| Trainer | [Brush][] 0.3.0 | Rust/WGPU runs on Mac (Metal) / Linux / Web; no CUDA dependency; "faster than gsplat" per their README. |
| Poses | COLMAP `automatic_reconstructor` | Sparse-only (`--dense 0`); GPU off (`--use_gpu 0`) so Mac works. |
| Viewer | [Spark][] 2.0 | Three.js + WebGL2; confirmed WebXR on Quest 3 + Vision Pro; LoD streaming to 100M splats. |
| API | FastAPI + uvicorn | The auto-generated OpenAPI spec is the teammate's client-gen input. |

These reflect an Apple-Silicon-first dev box (no CUDA). The venue laptop may
differ; see CLAUDE.md for the CUDA-path swap.

[Brush]: https://github.com/ArthurBrussee/brush

## See also

- [FLOWS.md](./FLOWS.md) — Mermaid sequence diagrams for the CLI, HTTP, and webhook flows + the status state machine.
- [CLAUDE.md](./CLAUDE.md) — full build plan, pipeline diagram, open decisions, collaboration rules.
- [robohack][robohack] — parent repo with hackathon strategy + research papers.
