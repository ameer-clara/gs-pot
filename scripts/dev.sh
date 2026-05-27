#!/usr/bin/env bash
# Dev server: serves the API + static viewer at http://localhost:8000.
#  - Viewer:        http://localhost:8000/web/
#  - Open a scan:   http://localhost:8000/web/?scene=/scenes/<scan_id>.ply
#  - API contract:  http://localhost:8000/docs   (or /openapi.json)
set -euo pipefail
cd "$(dirname "$0")/.."

# Auto-load .env (gitignored secrets — GS_POT_INGEST_TOKEN, GS_POT_ROBOHACK_BASE).
# `set -a` exports every var we source; turn it off before exec so uvicorn
# doesn't inherit anything unexpected.
if [ -f .env ]; then
    set -a; . ./.env; set +a
fi

exec uv run uvicorn gs_pot.server:app --host 127.0.0.1 --port 8000 "$@"
