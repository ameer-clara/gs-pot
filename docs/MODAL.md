# Deploying gs-pot on Modal

The product lives on Modal as the `murobo` app. This guide is the one-time
setup; once deployed, the agent (robohack) calls the Modal URL the same way
it called ngrok.

| Env | URL |
|---|---|
| staging | `https://ameer-clara-staging--murobo-web.modal.run` |
| prod    | `https://ameer-clara--murobo-web.modal.run` |

The bundled `modal_app.py` is a thin wrapper over the existing FastAPI app
(`gs_pot.server`). It mirrors the gigatime pattern (`/Users/x/code/gigatime/modal_app.py`).

---

## 1. One-time Modal setup

```bash
pip install --user modal
python3 -m modal setup           # browser-auth flow, links to ameer-clara workspace
modal environment create staging # if not already created
```

## 2. Create the secret (per env)

```bash
# staging
modal secret create murobo-staging \
  GS_POT_INGEST_TOKEN="$(grep GS_POT_INGEST_TOKEN .env | cut -d= -f2)" \
  GS_POT_ROBOHACK_BASE="https://gateway-production-94e2.up.railway.app" \
  --env staging

# prod (later, same shape, different secret name)
modal secret create murobo-prod \
  GS_POT_INGEST_TOKEN=… GS_POT_ROBOHACK_BASE=… \
  --env main
```

The secret name is auto-selected from `MODAL_ENVIRONMENT` (see
`_secret_name()` in `modal_app.py`).

## 3. Deploy

```bash
modal deploy modal_app.py --env staging
# → builds image (~3 min first time: apt + pip + Brush download),
#   prints the live URL, exits.
```

Re-deploy any time after editing `gs_pot/`:

```bash
modal deploy modal_app.py --env staging
# Image rebuilds incrementally — only `add_local_dir(gs_pot)` changes.
```

## 4. Point robohack at it

On Railway's web service:
```
NEXT_PUBLIC_GS_POT_URL=https://ameer-clara-staging--murobo-web.modal.run
```
Redeploy so Next.js inlines the var — the "Build splat" button now hits Modal.

---

## Smoke test the deploy

```bash
# 1. health check (Modal serves the existing FastAPI; / lists properties).
curl -s "https://ameer-clara-staging--murobo-web.modal.run/properties" | jq

# 2. Push a local dataset into the Volume so you can scan it without robohack:
modal volume put murobo-scenes ./images/b1-room b1-room/images_src --env staging

# 3. Create a Property, then submit a scan pointed at the Volume path.
BASE="https://ameer-clara-staging--murobo-web.modal.run"
PROP=$(curl -sX POST $BASE/properties -H 'content-type: application/json' \
   -d '{"name":"Ali B1 (Modal smoke)"}' | jq -r .property_id)
curl -sX POST $BASE/scans -H 'content-type: application/json' \
  -d "{
    \"property_id\":\"$PROP\",
    \"scene_name\":\"b1-room\",
    \"source\":\"images\",
    \"images_dir\":\"/data/scenes/b1-room/images_src\"
  }"
# → {"scan_id":"scn_…"} ; poll /scans/<id>

# 4. View
open "$BASE/web/?scene=/scenes/<scan_id>.ply"
```

---

## Operational notes

- **`max_containers=1`**: `gs_pot.store.Store` is an in-process dict, and the
  job worker is a single-threaded FIFO. Same constraint gigatime hit with
  SQLite-on-Volume. To scale past 1 container, migrate `Store` to Postgres
  (robohack already has one).
- **Scale-down loss**: if Modal scales the container to 0 with a job in
  flight, the work is lost. `scaledown_window=600` (10 min idle keep-warm)
  mitigates. Proper fix is to make the worker a separate `@app.function`
  spawned via `Function.spawn()` — deferred.
- **GPU choice**: default `T4` (~$0.59/hr). Brush uses Vulkan via WGPU and
  should pick up CUDA-side Vulkan on Modal's NVIDIA image. If it falls back
  to CPU, bump to `A10G` and verify with `brush --device-info`.
- **Trainer swap**: to swap Brush → gsplat (faster + better with CUDA),
  add `pip_install("gsplat torch torchvision")` to the image and write a
  `gsplat` arm in `gs_pot/train.py`. Mirror the `--trainer brush|opensplat`
  flag pattern already there.
- **Volume sync after a scan**: `scenes_volume.commit()` is auto-called at
  function exit; reads from a second container see the new `.ply` once the
  first container scales down or commits.

## Tear down

```bash
modal app stop murobo --env staging      # stop the deployment
modal volume rm murobo-scenes --env staging   # only if you want to nuke scenes
modal secret rm murobo-staging --env staging
```
