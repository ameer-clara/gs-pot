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

## End-to-end with robohack — `/api/runs/<run_id>/process` webhook

Production flow. Robohack's front-end drives a capture session, then calls
gs-pot via ngrok to trigger processing on "End run":

```
[Front-end]   pick run_id (uuid); tell robot to begin

[Go2 + DimOS] POST <robohack>/api/robot/frame   run=<id> position=N angle=θ   (×N images)

[Front-end]   user clicks "End run"
              │
              ▼  POST <YOUR_NGROK_URL>/api/runs/<run_id>/process
                     { robohack_base, ingest_token,
                       [trainer, steps, quality, scene_name] }

[gs-pot]      202 → { scan_id, queue_depth }
              ├─ download all frames for run_id from <robohack>/api/scans/<run_id>
              │  (presigned S3 URLs, 6h TTL — no auth needed)
              ├─ existing pipeline: HEIC-skip → COLMAP → gravity-align → train
              └─ POST scene.ply → <robohack>/api/robot/splat   Bearer <ingest_token>
                     name="run:<run_id>"

[Front-end]   GET <robohack>/api/scans renders the new splat with the run's frames
```

Endpoint:

```
POST /api/runs/{run_id}/process
  body:
    robohack_base:  string (required)  — e.g. "https://robohack.example"
    ingest_token:   string (required)  — Bearer for /api/robot/splat
    scene_name:     string?            — defaults to "run-<run_id[:12]>"
    trainer:        "brush" | "opensplat"     (default brush)
    steps:          int                       (default 2000)
    quality:        "low"|"medium"|"high"|"extreme"  (default "low")
  → 202 { scan_id, queue_depth }
```

Then poll `GET /scans/{scan_id}` for status (`queued` → `capturing` →
`poses` → `training` → `pushing` → `ready`).

A **single-worker FIFO queue** serializes every scan (whether it came in
via `POST /scans` or `POST /api/runs/.../process`) since they all saturate
the same CPU/GPU. `queue_depth` in the response tells the front-end "you're
#N in line."

CORS is open (`*`) for hackathon convenience — the front-end at robohack's
domain can call the ngrok URL cross-origin. Lock down for production.

**Local setup:**

```bash
./scripts/dev.sh                    # FastAPI on :8000
# in another shell:
ngrok http 8000                     # → https://<random>.ngrok.app
# paste that URL into the robohack front-end; "End run" hits it
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
