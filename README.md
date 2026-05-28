# murobo

> **On-demand robots, powered by the network of minds.**
> Rent a robot. An agent decides what jobs to run. Two of those jobs:
> 1. **Splat** — robot walks a room, you get a 3D Gaussian Splat in VR.
> 2. **Sense** — WiFi-CSI motion bridge, robot barks when someone enters.

Built for the [DIMENSIONAL Hackathon Shanghai][robohack] (May 26–28, 2026).
murobo is the **producer**: agents POST to its HTTP API to dispatch a
robot job, and consume the result (a `.ply`, a webhook, a bark). The agent
side is the [robohack][robohack] repo.

[robohack]: https://github.com/grmkris/robohack

---

## The 60-second demo

**Splat → VR walkthrough**
1. Agent dispatches a `room_scan` job; the Unitree Go2 walks the room and uploads frames.
2. Agent calls `POST /api/runs/<run>/process` on murobo.
3. ~10–30 min later the run's status flips to `ready` and a `.ply` URL drops.
4. Open it on a Quest 3 / Vision Pro — tap **Enter VR**. Walk the room.

**Sense → motion alert**
1. Agent calls `POST /api/motion/calibrate`, then `POST /api/motion/start`.
2. Two ESP32-S3 nodes on the robot stream WiFi CSI through walls.
3. murobo runs an adaptive threshold; on quiet→motion the Go2 **barks** + a webhook fires.

## Why this exists

The pitch is **robots-as-a-service brokered by AI agents**: an operator owns
the fleet, a renter describes a job in natural language, and an agent picks
the right robot + the right pipeline to run on it. Two ready-made jobs:

**Splat side.** China's VR home-tour market is real — Beike/Lianjia ships ~1M
VR-shot apartments and credits VR for ~20% of lead conversion. The bottleneck
is **the human shooter with the tripod rig**. A quadruped is the natural
shooter: walks itself, runs every day, no human schedule. An agent that can
rent the shooter on demand closes the loop.

**Sense side.** The same rented robot doubles as an inspection / security
dog. WiFi-CSI sees through walls and doesn't need cameras pointed at the
renter — privacy-positive presence detection. Same hardware, second
recurring-revenue surface, same agent dispatching it.

[spark]: https://github.com/sparkjsdev/spark

---

## Pipelines

**Splat**
```
images → COLMAP poses → Brush train → gravity-align → .ply + thumb.jpg
                                                              │
                              ┌───────────────────────────────┤
                              ▼                               ▼
              local: /scenes/<scan_id>.ply       push to robohack
                                                 POST /api/robot/splat
                                                              │
                                                              ▼
                                            robohack.apps/web ← Spark + WebXR
```

**Sense**
```
ESP32-S3 CSI ──UDP──▶ sensing-server :3000 ──HTTP──▶ murobo detector
                                                          │ μ + k·σ adaptive
                                                          │ hysteresis
                                                          ▼
                                                  ┌───────┴────────┐
                                                  ▼                ▼
                                            POST webhook     Go2 barks
                                                             (`say` in dev)
```

Domain model (splat side):

- **Property** (apartment) groups N **Scans** (rooms).
- One Scan → one `.ply` at `/scenes/<scan_id>.ply`.

---

## Install

macOS or Linux, Python 3.12, [uv][uv].

```bash
# Python deps
uv sync --extra dev

# COLMAP (sparse SfM)
brew install colmap                 # mac
# Linux:  sudo apt install colmap

# Brush 0.3.0 trainer binary
cd bin/
curl -sL -o brush.tar.xz https://github.com/ArthurBrussee/brush/releases/download/v0.3.0/brush-app-aarch64-apple-darwin.tar.xz
tar -xJf brush.tar.xz && rm brush.tar.xz
ln -sf brush-app-aarch64-apple-darwin/brush_app brush
./brush --version                   # → brush-cli 0.3.0
cd ..

uv run pytest                       # → 18 passed
```

Linux x86_64 / Windows: swap the Brush release filename (see
[Brush releases][brush-rel]). Override with `BRUSH_BIN=/path/to/brush`.

[uv]: https://docs.astral.sh/uv/
[brush-rel]: https://github.com/ArthurBrussee/brush/releases

---

## Quickstart — robot frames to splat (local)

For end-to-end with the agent, see [The production loop](#the-production-loop--agent-webhook)
below. The CLI below is the fast inner loop: point it at a directory of
robot-captured frames and out comes a `.ply`.

```bash
# A directory of JPEGs the Go2 (or any capture client) dropped in scenes/<run>/images/
./scripts/scan-room.sh living_room "Apt 3F"
# → scenes/<scan_id>/scene.ply + viewer URL

# Serve + view:
./scripts/dev.sh                    # FastAPI + /web on :8000
# open  http://localhost:8000/web/?scene=/scenes/<scan_id>.ply
```

| Knob | Default | Notes |
|---|---|---|
| `--trainer {brush,opensplat}` | `brush` | OpenSplat is 3–5× faster once built ([bin/README](./bin/README.md)) |
| `--quality {low,medium,high,extreme}` | `medium` | COLMAP SfM preset |
| `--steps N` | Brush 7000, OpenSplat 2000 | trainer-aware |

For best results the robot's `room_scan` skill stops at multiple positions and
sweeps ~12–24 headings per stop (50–70% overlap between adjacent frames).
30–120 frames per room is the working range.

Wall time on M4 Max (30–120 frames, `--quality medium`):
- Brush 7k steps: ~10–30 min
- OpenSplat 2k steps: ~3–8 min

---

## The production loop — agent webhook

In production murobo runs on the operator's Mac behind ngrok. An agent
(robohack `apps/server` / `apps/web`) fires the webhook; murobo fetches the
captured frames from object storage, trains, pushes the splat back, and the
agent's "View splat" link goes live.

### Setup (one-time)

```bash
cat > .env <<EOF
GS_POT_INGEST_TOKEN=<ROBOT_INGEST_TOKEN from robohack/apps/server>
GS_POT_ROBOHACK_BASE=https://gateway-production-94e2.up.railway.app
EOF

./scripts/dev.sh                                              # localhost:8000
ngrok http --url=https://twilight-getting-possum.ngrok-free.dev 8000
```

On Railway's web service:
```
NEXT_PUBLIC_GS_POT_URL=https://twilight-getting-possum.ngrok-free.dev
```
Redeploy so Next.js inlines the var; without it the "Build splat" button is silently hidden.

### The flow

1. Renter asks the agent for a VR walkthrough; agent dispatches `room_scan` to the rented Go2.
2. Robot POSTs each frame to `<gateway>/api/robot/frame` (run/position/angle).
3. Agent calls `<ngrok>/api/runs/<run>/process {}` on murobo.
4. murobo 202s, queues the job, mints `scan_id`, polls visible at `GET /scans/<scan_id>`.
5. Worker: download frames → COLMAP → gravity-align → Brush → push `.ply`.
6. Status `ready` lands; agent surfaces the **View splat ↗** link to the renter.

```
queued → capturing (downloading 12/20) → poses (10%) → training (40%)
       → pushing (uploading 6.1 MB, retry 2/4 if 502) → ready (1.0)
```

The push to `/api/robot/splat` is multipart `{file, format, name}` with Bearer
auth. Retry-with-backoff (2s/4s/8s) on 502/503/504 + ConnectError/ReadTimeout
because Railway's edge occasionally hiccups on large uploads.

### Manual push of an existing .ply

```bash
uv run python -c "
from pathlib import Path; from gs_pot.ingest import push_splat; import os
r = push_splat(
    Path('scenes/<scan_id>/scene.ply'),
    ingest_url=os.environ['GS_POT_ROBOHACK_BASE'].rstrip('/') + '/api/robot/splat',
    token=os.environ['GS_POT_INGEST_TOKEN'],
    name='my-scene',
)
print(r)  # → {id: splat_…, key: splats/….ply}
"
```

---

## Motion alert — WiFi-CSI → robot bark

Invoked by the agent (or by a teammate-side button bound to the agent).
murobo polls `features.motion_band_power` from the sensing-server and runs its own
adaptive `μ + k·σ` threshold with hysteresis. On every state flip it POSTs
a webhook; on quiet→motion the robot also barks.

### Why not just `classification.presence` from the sensing-server?

The cooked classifier decays to `absent` when someone is sitting still, and
trips on HVAC/screen noise. Live test showed Δpresence = -25.8 %-points vs.
ground truth. Raw `motion_band_power` is clean — so we run our own
adaptive `μ + k·σ` threshold with hysteresis.

### Use it

```bash
# 1. Calibrate the empty room (stay OUT of the volume for 30s).
curl -sX POST localhost:8000/api/motion/calibrate \
  -H 'content-type: application/json' -d '{"seconds":30}' | jq

# 2. Start. `bark.mode`: "say" (mac dev) | "afplay" | "dimos" (real robot) | "noop".
curl -sX POST localhost:8000/api/motion/start \
  -H 'content-type: application/json' -d '{
    "sensing_url":"http://localhost:3000/api/v1/sensing/latest",
    "webhook_url":"https://example.com/hook",
    "bark":{"mode":"say"},
    "max_duration_s":600
  }' | jq

# 3. Status / stop / test bark.
curl -s    localhost:8000/api/motion/status | jq
curl -sX POST localhost:8000/api/motion/stop | jq
curl -sX POST localhost:8000/api/motion/bark -H 'content-type: application/json' -d '{}'
```

Webhook fires on both transitions; bark fires only on quiet→motion (with a 3s
cooldown). Full body shape and tuning knobs: `gs_pot/motion.py`.

> Recalibrate on every room / AP / channel / time-of-day change. The shipped
> `motion_baseline.json` is just a seed.

---

## Endpoints

```
POST /api/runs/{run_id}/process              ← splat webhook from robohack
GET  /scans/{scan_id}                        ← live ScanInfo (poll every 3s)
GET  /scenes/{scan_id}.ply                   ← static binary (Spark loads this)
GET  /scenes/{scan_id}/thumb.jpg
GET  /web/?scene=/scenes/{scan_id}.ply       ← built-in Spark + WebXR viewer

POST /properties · GET /properties · GET /properties/{id}
POST /scans      · GET /scenes

POST /api/motion/calibrate                   ← empty-room baseline
POST /api/motion/start  · POST /api/motion/stop
GET  /api/motion/status · POST /api/motion/bark
```

`/openapi.json` is the source of truth; `tests/test_contract.py` is the live spec.

---

## Stack picks

| Concern | Pick | Why |
|---|---|---|
| Trainer | [Brush 0.3.0][brush] | Rust/WGPU on Metal — no CUDA dep, ships on a Mac. |
| Poses | [pycolmap][] (not the CLI) | Homebrew's macOS arm64 COLMAP has a SIFT-matcher SIGSEGV. |
| Viewer | [Spark 2.0][spark] | Three.js + WebGL2; WebXR on Quest 3 + Vision Pro; LoD streams to 100M splats. |
| CSI nodes | ESP32-S3 + [RuView][ruview] | Off-the-shelf $5 boards; UDP CSI → laptop. Pinned to a 2.4 GHz AP (the S3 has no 5 GHz radio). |
| Detector | Custom `μ + k·σ` + hysteresis | The sensing-server's cooked `presence` field was -25.8 %-pts vs. ground truth; raw `motion_band_power` is clean. |
| API | FastAPI + uvicorn | OpenAPI spec is the teammate's client-gen input. |

Apple-Silicon-first; venue laptop with CUDA can swap to gsplat — see [CLAUDE.md](./CLAUDE.md).

[brush]: https://github.com/ArthurBrussee/brush
[pycolmap]: https://github.com/colmap/pycolmap
[ruview]: https://github.com/ruvnet/wifi-densepose

---

## Project layout

```
gs_pot/
  models.py    pipeline.py   poses.py      train.py
  ingest.py    motion.py     server.py     store.py
  runs.py      thumb.py      cli.py
tests/         test_contract.py · test_ingest.py · test_motion.py · …
web/           Spark 2.0 + WebXR smoke viewer (mounted at /web)
scripts/       dev.sh · scan-room.sh
bin/           Brush + OpenSplat binaries (gitignored — see bin/README.md)
scenes/        per-scan workspaces + outputs (gitignored)
```

---

<details>
<summary><b>COLMAP quality presets</b></summary>

| Preset | Image size | Max SIFT | Guided matching | BA iters (local/global) |
|---|---|---|---|---|
| `low`     | 1000 px | 2048  | off | 12 / 30  |
| `medium`  | 1600 px | 8192  | off | 16 / 50  |
| `high`    | 2400 px | 16384 | on  | 25 / 75  |
| `extreme` | 3200 px | 32768 | on  | 40 / 100 (OPENCV camera) |

**Counter-intuitive:** for weak-texture scenes (bathrooms, white walls,
tile, mirrors), `low` often registers **more** images than `medium`/`high`.
`guided_matching` prunes against a noisy initial epipolar geometry and
the mapper fails to find a seed pair. Start `low`, promote up.

</details>

<details>
<summary><b>Trainer choice — Brush vs OpenSplat</b></summary>

Both consume the same COLMAP workspace and emit a Spark-compatible `.ply`.
Swap with `--trainer`.

| | Brush 0.3.0 | OpenSplat |
|---|---|---|
| Backend | Rust / WGPU → Metal | C++ / libtorch native Metal |
| Install | drop-in binary | `brew install cmake opencv pytorch` + cmake (~15 min) |
| M4 Max speed | 15k ≈ 29 min (9 photos) | 2k ≈ ~5 min (3–5× faster) |
| Convergence | 5k–15k gradient steps | 2k–5k iterations |
| License | Apache-2.0 + MIT | AGPLv3 |

Brush is the default — always works, no build. OpenSplat is the speed path
once you're iterating.

</details>

<details>
<summary><b>What you'll see in <code>dev.sh</code></b></summary>

```
INFO  uvicorn.access     "POST /api/runs/scan-…/process HTTP/1.1" 202
INFO  gs_pot.runs        run scan-…: 120 frames to fetch
INFO  gs_pot.runs        run scan-…: 120 frames in scenes/scn_…/images_src
INFO  gs_pot.poses       pycolmap: extract_features (size=1000, features=2048)
INFO  gs_pot.poses       pycolmap: match_exhaustive
INFO  gs_pot.poses       pycolmap: incremental_mapping
INFO  gs_pot.poses       gravity-align: rotated 120 cameras (|mean down|=0.97)
INFO  gs_pot.train       running: bin/brush scenes/scn_… --total-steps 7000
INFO  gs_pot.train       Brush exported: scenes/scn_…/scene.ply
INFO  gs_pot.ingest      pushing scene.ply (6.1 MB) → /api/robot/splat
WARN  gs_pot.ingest      push got 502 on attempt 1/4, retrying in 2.0s
INFO  gs_pot.ingest      ingest accepted on attempt 2/4: id=splat_…
INFO  gs_pot.pipeline    [scn_…] DONE
```

</details>

<details>
<summary><b>Failure modes + fixes</b></summary>

| Symptom | Cause | Fix |
|---|---|---|
| `502 Bad Gateway` mid-push | Railway edge hiccup on large multipart | Retry-with-backoff handles it; otherwise re-push the on-disk `.ply` |
| `/api/scans/<run>` returns 404 HTML | Old Caddyfile didn't route `/api/scans*` to apps/server | Fixed; redeploy gateway |
| Brush panic `min > max, or NaN` | `steps=2000` too low for Brush's lr schedule | Fixed: Brush default 7000 |
| CORS error, response is HTML | ngrok-free interstitial | Send `ngrok-skip-browser-warning: true` |
| Reconstruction rotated 90° | EXIF rotation not baked / wrong COLMAP gravity prior | `scan-room.sh` calls `PIL.ImageOps.exif_transpose`; `poses._align_to_gravity` rotates the sparse model |
| Bathroom / mirror scene → all floaters | Only a couple images registered (SfM-hostile) | Pick a textured room; bathrooms are worst-case |
| `--quality medium` rejects all images | `guided_matching` over-prunes weak texture | Drop to `--quality low` |
| Worker silent in dev.sh | `gs_pot.*` loggers not configured | `server.py` calls `logging.basicConfig` at import |

</details>

<details>
<summary><b>RuView ESP32-S3 node provisioning</b> (CSI sensing nodes — first-time bring-up)</summary>

Two ESP32-S3 "AI" boards (16 MB flash), macOS Apple Silicon, RuView repo at `~/code/RuView`.

**0. Network — any 2.4 GHz hotspot (ESP32-S3 has no 5 GHz radio).**
- We used a personal hotspot with "Maximize Compatibility" forced to 2.4 GHz.
- Join Mac to the hotspot. `ipconfig getifaddr en0` → sink IP (e.g. `172.20.10.5`).
- Keep Mac on the hotspot for the whole session.

**1. Tools (one-time).**
```bash
python3 -m pip install --break-system-packages --user esptool esp-idf-nvs-partition-gen
```

**2. Per node — plug ONE board in, then:**
```bash
# Find port
ls /dev/cu.usbmodem*                # → /dev/cu.usbmodem1101

# Confirm chip
python3 -m esptool --chip esp32s3 --port <PORT> flash-id

# Flash RuView firmware (overwrites the xiaozhi 小智 AI firmware boards ship with)
python3 -m esptool --chip esp32s3 --port <PORT> --baud 460800 \
  write_flash --flash_mode dio --flash_size 8MB \
  0x0     firmware/esp32-csi-node/release_bins/bootloader.bin \
  0x8000  firmware/esp32-csi-node/release_bins/partition-table.bin \
  0xf000  firmware/esp32-csi-node/release_bins/ota_data_initial.bin \
  0x20000 firmware/esp32-csi-node/release_bins/esp32-csi-node.bin

# Provision WiFi / sink / identity (NVS write, no reflash)
python3 firmware/esp32-csi-node/provision.py --port <PORT> \
  --ssid murobo24 --password '<PASS>' \
  --target-ip 172.20.10.5 --target-port 5005 \
  --node-id <0|1> --tdm-slot <0|1> --tdm-total 2

# Verify boot
python3 -m serial.tools.miniterm <PORT> 115200
# → "wifi:state … -> run ch=6", "csi_collector: cb #… len=128", "stream → 172.20.10.5:5005"
# exit Ctrl-]
```

| Node | `--node-id` | `--tdm-slot` | MAC |
|---|---|---|---|
| 0 | 0 | 0 | `98:a3:16:f2:a7:80` |
| 1 | 1 | 1 | `9c:13:9e:a9:f2:0c` |

**3. Verify UDP arrives on the Mac.**
```bash
python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(('0.0.0.0',5005))
print('listening'); n=0
while True:
    d,a = s.recvfrom(65535); n+=1; print(f'#{n} {len(d)}B from {a[0]}:{a[1]}')
"
# → packets from two distinct 172.20.10.x source IPs.
```

**4. Run the sensing-server.**
```bash
pkill -f udprecv 2>/dev/null
docker run --rm -p 3000:3000 -p 3001:3001 -p 5005:5005/udp \
  -e CSI_SOURCE=esp32 ruvnet/wifi-densepose:latest
# Dashboard: http://localhost:3000/ui/index.html
# WS frames: ws://localhost:3001/ws/sensing  (~12 Hz)
# Useful fields: features.motion_band_power, classification.presence, vital_signs.*
# (persons[].pose is synthetic, confidence=0 — ignore)
```

**Re-provision** any time: just re-run step 2's `provision.py`. No reflash.

</details>

---

## See also

- [CLAUDE.md](./CLAUDE.md) — full build plan, decision matrix, collaboration rules
- [FLOWS.md](./FLOWS.md) — Mermaid sequence diagrams for the CLI / HTTP / webhook / status state machine
- [robohack][robohack] — parent hackathon repo
- [Spark][spark] · [Brush][brush] · [pycolmap][] — the load-bearing dependencies
