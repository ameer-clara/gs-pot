# gs-pot — robot-scanned Gaussian Splat → VR walkthrough

This package is a submodule of [grmkris/robohack](https://github.com/grmkris/robohack)
mounted at `packages/gs-pot`. robohack is prep for the **DIMENSIONAL Hackathon
Shanghai (May 26–28, 2026)** — 48h, Unitree Go2 + DimOS, three tracks
(Autonomy & Navigation · Agents · Open/Creative). Read the parent repo's
`research/00-synthesis.md` for the master strategy and `research/01-dimos-codebase-map.md`
for verified DimOS internals before making stack changes.

## What we're building

> The Go2 walks an apartment, streams RGB + odometry, we train a 3D Gaussian
> Splat from the captured frames, and the buyer/renter views it in any browser
> — with WebXR for VR headsets (Quest, Vision Pro, Cardboard).

**Direction C** (parallel to robohack's Direction A patrol-dog / Direction B swarm):
**robot-scanned Gaussian Splat for real-estate VR walkthroughs.**

### Why this is a real wedge, not a hack
- China leads the world on VR home tours — **Beike/Lianjia** has shipped 1M+
  VR-shot apartments and credits VR for ~20% of lead conversion. The product
  category is proven; the bottleneck is **the human shooter with the tripod rig**.
- A quadruped is the natural shooter: walks itself, runs every day, no human
  schedule. Inspection/security companies (the Patrol-Dog buyer in the parent
  repo's synthesis) already own Go2s — VR-scanning is incremental revenue.
- Strong *落地* story + visually arresting demo + agentic framing
  ("robot autonomously produces an asset humans pay for"). Plays to Open/Creative
  judging (Grand-Prize swing) without the multi-dog dependency of Direction B.

## Pipeline (high level)

```
 ┌──────────────────────────┐    ┌────────────┐    ┌─────────────┐    ┌────────────┐
 │ Go2 WebRTC               │    │ Capture    │    │ Pose +      │    │ Train 3DGS │
 │  color_image  ─────────▶ │ ──▶│  @skill    │ ──▶│ frame prep  │ ──▶│ (gsplat)   │
 │  odom (pose)             │    │ (DimOS)    │    │             │    │            │
 └──────────────────────────┘    └────────────┘    └─────────────┘    └─────┬──────┘
                                                                            │
                ┌───────────────────────────────────────────────────────────┘
                ▼
       ┌────────────────┐    ┌──────────────────────────────┐
       │ .ply → .splat /│    │ Browser viewer + WebXR       │
       │ .spz           │ ──▶│ (Three.js + gaussian-splats- │
       │                │    │  3d, Quest/Vision Pro)       │
       └────────────────┘    └──────────────────────────────┘
```

## The three decisions that fork the build

### 1. Pose source — odometry vs. SfM (the critical-path call)

Gaussian splatting needs per-frame camera poses. Options:

| Approach | Speed | Quality | 48h fit |
|---|---|---|---|
| **Go2 odometry direct** | Instant | Drifts on long loops; fine for a single room | ✅ Best demo-loop time, ships tonight |
| **GLOMAP** | ~minutes | Near-COLMAP | ✅ Drop-in COLMAP replacement, ~10× faster |
| **MASt3R-SfM / DUSt3R** | Minutes | Strong on small image sets | ✅ Modern, dense, monocular-friendly |
| **InstantSplat** | Minutes | Joint pose + splat | ✅ Bypasses SfM entirely — interesting fallback |
| **COLMAP** | 10–30 min | Reference | ❌ Too slow for a live demo loop |

**Default plan:** start with Go2 odometry → write COLMAP-format `cameras.txt` /
`images.txt` directly. Add MASt3R-SfM as a refinement pass if odom drift is
visible in the splat. The robot already knows where it is — use it.

### 2. Training stack — CUDA vs. no-CUDA

| Library | Lang | GPU | Notes |
|---|---|---|---|
| **gsplat** (Nerfstudio) | Python | CUDA | Fastest, modern; current SOTA; ties into Nerfstudio data tools |
| **Brush** | Rust/WGPU | **CPU + any GPU (Metal/Vulkan/WebGPU)** | Runs on Mac, in-browser; killer fallback if the venue laptop has no CUDA |
| **inria/gaussian-splatting** | Python | CUDA | Reference impl; slower, more fragile |

The RUNBOOK calls GPU "optional but slow without it." **Confirm GPU at check-in.**
If yes → gsplat. If no → Brush (and a much shorter scene budget).

### 3. Browser viewer — pick one, ship the WebXR path

| Viewer | Stack | WebXR | Format |
|---|---|---|---|
| **@mkkellogg/gaussian-splats-3d** | Three.js | ✅ | `.ply`, `.splat` |
| **Spark** (Niantic) | Three.js | ✅ | `.spz` (compressed) |
| **gsplat.js** (antimatter15) | WebGL | partial | `.splat` |
| **SuperSplat viewer** | PlayCanvas | ✅ | `.ply` |

**Default plan:** `@mkkellogg/gaussian-splats-3d`. Most mature WebXR path,
best community examples for Quest. Convert .ply → .splat for transport.

## Stack we're shipping (default, override only with reason)

| Layer | Pick | Why |
|---|---|---|
| Capture | DimOS `@skill` on `color_image` + `odom_stream` | Aligned with the Patrol Dog architecture; one MCP call to start/stop |
| Poses | Go2 odom → COLMAP format (MASt3R-SfM as fallback) | Robot knows its pose; skip the SfM tax |
| Train | `gsplat` (Brush if no CUDA) | Fast + modern |
| Format | `.splat` (compressed from `.ply`) | Browser-friendly |
| Viewer | Three.js + `@mkkellogg/gaussian-splats-3d` | Mature WebXR |
| Serve | FastAPI + static `/scenes/<id>/scene.splat` | Same VPS that hosts the payment webhook from robohack/RUNBOOK |

## DimOS integration — the `@skill` seam

Per `research/01-dimos-codebase-map.md`, the live skill API is the
**`@skill` decorator** in `dimos/agents/annotation.py`. Skills must have a
docstring (the LLM tool description), typed params, and `str` returns.
**Don't stack `@rpc` + `@skill`.** Add a new `Module` and register it in
`dimos/robot/unitree/go2/blueprints/agentic/_common_agentic.py`.

Planned skill surface (`gs_pot/skills.py` — to be written):

```python
@skill
def start_scan(scene_name: str) -> str:
    """Begin capturing frames + poses for a Gaussian-splat scan.

    Args:
        scene_name: Human-readable scene id, e.g. "apt_3f_living_room".
    """

@skill
def stop_scan() -> str:
    """Finalize the active scan and write the dataset to disk."""

@skill
def train_splat(scene_name: str, max_iters: int = 7000) -> str:
    """Train a Gaussian splat from a captured scan.

    Args:
        scene_name: Scene id from a prior start_scan/stop_scan.
        max_iters: Optimization iterations. Default ~7k for a hackathon scene.
    """

@skill
def share_scan(scene_name: str) -> str:
    """Return a public viewer URL for the trained splat."""

@skill
def set_capture_pattern(pattern: str) -> str:
    """Select an autonomous capture trajectory: perimeter | spiral | raster."""
```

Capture-trajectory autonomy is optional v2 — for the first demo the user
teleops the dog while `start_scan` runs.

## Repo layout (target)

```
gs-pot/
├── CLAUDE.md                  ← this file
├── README.md
├── pyproject.toml             ← uv-managed; mirrors dimos's Python 3.12 pin
├── gs_pot/
│   ├── __init__.py
│   ├── skills.py              ← @skill container, registered into _common_agentic.py
│   ├── capture.py             ← color_image + odom_stream → frames + poses.json
│   ├── poses.py               ← odom → COLMAP cameras.txt/images.txt
│   ├── train.py               ← gsplat training wrapper (Brush variant behind a flag)
│   └── serve.py               ← FastAPI: static .splat + viewer page
├── web/
│   ├── index.html             ← Three.js + @mkkellogg/gaussian-splats-3d + WebXR
│   └── viewer.js
├── scenes/                    ← gitignored; per-scan datasets + trained .splat
└── scripts/
    ├── webcam_capture.py      ← non-Go2 fallback (laptop webcam → frames)
    └── colmap_export.py
```

## Open decisions (resolve before H4)

- **GPU at venue?** Gates gsplat vs. Brush. Ask at check-in (parent
  RUNBOOK Phase 1).
- **Pose strategy** — odom-only or odom + MASt3R refinement? Start odom-only;
  promote if visible artifacts.
- **Hosting** — local laptop server on the venue LAN, or push .splat to the
  VPS (public URL)? VPS is needed for a Quest/Vision Pro demo to a judge not
  on the venue WiFi. **Stand this up tonight.**
- **Real-estate framing vs. generic 3D-scan demo** — same code, different pitch.
  Lead with apartments for *落地*; mention museums/inspection as adjacent markets.
- **Submodule visibility** — gs-pot is currently a **private** GitHub repo.
  robohack is public; downstream cloners can't `submodule update` it. Decide
  whether to make gs-pot public before the demo.

## Non-goals (don't drift here)

- ❌ Don't write a new gaussian-splatting trainer from scratch. Use gsplat/Brush.
- ❌ Don't reimplement DimOS modules in TypeScript. TS = browser viewer only
  (see parent IDEAS.md "TypeScript role"). Skills are Python.
- ❌ Don't chase NeRF — splats render faster in the browser and have working
  WebXR libraries. The category is settled for this use case.
- ❌ Don't bundle a payment rail into v1. The robohack payments multiplier
  (Alipay / x402) is orthogonal and lives in robohack itself.

## References (parent-repo pointers)

- robohack root README and `IDEAS.md` — Direction A/B context, multipliers.
- `research/00-synthesis.md` — strategy synthesis, 落地 framing, the crypto-vs-Alipay fork.
- `research/01-dimos-codebase-map.md` — DimOS internals, verified Skill API.
- `research/02-go2-ecosystem-landscape.md` — `legion1581/go2_webrtc_connect`
  (raw WebRTC alternative to DimOS) and the trick command map.
- `learn/LEARNINGS.md` — hands-on `@skill` learnings, version-pin traps.
- `RUNBOOK.md` — pre-event laptop setup, venue WiFi reality, 3-decision matrix.

## External references (verify before depending on)

- gsplat — `https://github.com/nerfstudio-project/gsplat`
- Brush — `https://github.com/ArthurBrussee/brush`
- MASt3R-SfM — `https://github.com/naver/mast3r`
- GLOMAP — `https://github.com/colmap/glomap`
- @mkkellogg/gaussian-splats-3d — `https://github.com/mkkellogg/GaussianSplats3D`
- Spark — `https://github.com/sparkjs/spark`
- Beike VR product context — `https://vr.ke.com/` (the market we're targeting)
