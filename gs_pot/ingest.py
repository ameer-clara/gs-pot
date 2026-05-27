"""Push a trained splat to robohack's `apps/server` ingest endpoint.

Contract (verified against robohack `app/apps/server/src/http/robot.ts:68`):

    POST <GS_POT_INGEST_URL>           # e.g. https://robohack.example/api/robot/splat
    Authorization: Bearer <token>
    Content-Type: multipart/form-data
        file:    binary .ply/.spz/.splat/.ksplat/.sog  (max 256 MB server-side)
        format:  optional; else derived from filename extension
        name:    optional human label
    → 200 application/json
        { "key": "splats/<id>.ply", "id": "splat_..." }

This module is intentionally tiny — pipeline.py decides *whether* to push
(based on env vars) and *what name* to pass; this just executes the HTTP call.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Splat formats robohack's server accepts (SPLAT_EXTS in their robot.ts:8).
ACCEPTED_FORMATS: frozenset[str] = frozenset({"ply", "spz", "splat", "ksplat", "sog"})


def push_splat(
    ply_path: Path,
    *,
    ingest_url: str,
    token: str,
    name: str | None = None,
    timeout: float = 600.0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """POST `ply_path` to `ingest_url` with a Bearer token. Returns the parsed JSON.

    Raises:
        FileNotFoundError: ply_path doesn't exist.
        ValueError: file extension isn't in the accepted set.
        httpx.HTTPStatusError: server rejected the upload (non-2xx).
    """
    if not ply_path.exists():
        raise FileNotFoundError(f"splat file not found: {ply_path}")
    ext = ply_path.suffix.lstrip(".").lower()
    if ext not in ACCEPTED_FORMATS:
        raise ValueError(
            f"unsupported splat format '{ext}' — robohack accepts {sorted(ACCEPTED_FORMATS)}"
        )

    size_mb = ply_path.stat().st_size / 1_000_000
    log.info("pushing %s (%.1f MB) → %s", ply_path.name, size_mb, ingest_url)

    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=timeout)
    try:
        with ply_path.open("rb") as fh:
            files = {"file": (ply_path.name, fh, "application/octet-stream")}
            data: dict[str, str] = {"format": ext}
            if name:
                data["name"] = name
            r = client.post(
                ingest_url,
                headers={"Authorization": f"Bearer {token}"},
                files=files,
                data=data,
            )
            r.raise_for_status()
            payload = r.json()
            log.info("ingest accepted: id=%s key=%s", payload.get("id"), payload.get("key"))
            return payload
    finally:
        if own_client:
            client.close()
