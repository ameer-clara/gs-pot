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
uv run python -m gs_pot scan \
    --images ./photos/living_room \
    --property-name "My Apt" \
    --scene-name "living_room"
```

Knobs:

| Flag | Default | Notes |
|---|---|---|
| `--quality {low,medium,high,extreme}` | `medium` | COLMAP preset. `low` is much faster, useful for first iterations. |
| `--steps N` | `7000` | Brush training iterations. `5000` faster; `15000` sharper. |
| `--scenes-dir PATH` | `./scenes` | Where the workspace + outputs land. |

Output:

- `scenes/<scan_id>/scene.ply` — the Gaussian splat
- `scenes/<scan_id>/thumb.jpg` — thumbnail
- The CLI prints a viewer URL when it's done.

Expected wall time on an M4 Max with ~100 photos, `--quality medium --steps 7000`: roughly **5–15 minutes** end-to-end (COLMAP dominates).

### 3. View it

In another terminal:

```bash
./scripts/dev.sh                    # serves /web/ + the API on :8000
```

Open the URL the CLI printed:

```
http://localhost:8000/web/?scene=/scenes/<scan_id>.ply
```

- Desktop: Chrome 134+ on Mac (Spark uses modern WebGL2).
- VR: open the same URL on Quest 3 / Vision Pro browser, click "Enter VR".

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
  pipeline.py        # orchestrator: poses → train → thumb → READY
  poses.py           # COLMAP subprocess wrapper (CPU mode, no CUDA)
  train.py           # Brush subprocess wrapper
  thumb.py           # first-image → JPG thumbnail
  cli.py             # `python -m gs_pot scan ...`
tests/
  test_contract.py   # 18 producer/consumer contract tests
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

- [CLAUDE.md](./CLAUDE.md) — full build plan, pipeline diagram, open decisions, collaboration rules.
- [robohack][robohack] — parent repo with hackathon strategy + research papers.
