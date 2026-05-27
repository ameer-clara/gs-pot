"""Push a trained splat to robohack's `apps/server` ingest endpoint.

Contract (verified against robohack `app/apps/server/src/http/robot.ts:68`):

    POST <GS_POT_INGEST_URL>           # e.g. https://robohack.example/api/robot/splat
    Authorization: Bearer <token>
    Content-Type: multipart/form-data
        file:    binary .ply/.spz/.splat/.ksplat/.sog  (max 256 MB server-side)
        format:  optional; else derived from filename extension
        name:    optional human label
        runId:   optional scan-run identifier — when present, robohack
                 persists it on splats.runId so the /scans page's "View
                 splat" button routes to the in-app viewer instead of
                 gs-pot's local viewer URL.
    → 200 application/json
        { "key": "splats/<id>.ply", "id": "splat_..." }

Retries on transient failures (502/503/504 + connection errors / timeouts /
broken responses) with exponential backoff. The Railway-fronted gateway in
front of robohack sometimes 502s mid-upload on slow networks; one retry
usually clears it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Splat formats robohack's server accepts (SPLAT_EXTS in their robot.ts:8).
ACCEPTED_FORMATS: frozenset[str] = frozenset({"ply", "spz", "splat", "ksplat", "sog"})

# HTTP statuses that suggest the upstream is temporarily unhappy. 401/403/400/
# 413/415 are deliberate refusals — retrying won't help, raise immediately.
_RETRY_HTTP_STATUSES: frozenset[int] = frozenset({502, 503, 504})
# Network-layer failures that are commonly transient.
_RETRY_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


def push_splat(
    ply_path: Path,
    *,
    ingest_url: str,
    token: str,
    name: str | None = None,
    run_id: str | None = None,
    timeout: float = 600.0,
    max_retries: int = 3,
    base_backoff: float = 2.0,
    on_progress: Callable[[str], None] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """POST `ply_path` to `ingest_url` with a Bearer token. Returns the parsed JSON.

    Retries up to `max_retries` additional times with exponential backoff
    (`base_backoff * 2**attempt` seconds: 2s, 4s, 8s by default) on transient
    failures only — see `_RETRY_HTTP_STATUSES` / `_RETRY_EXCEPTIONS`.
    Non-transient failures (401, 403, 400, 413, 415, ValueError, etc.) raise
    on the first attempt.

    Raises:
        FileNotFoundError: ply_path doesn't exist.
        ValueError: file extension isn't in the accepted set.
        httpx.HTTPStatusError: server rejected the upload after all retries.
    """
    if not ply_path.exists():
        raise FileNotFoundError(f"splat file not found: {ply_path}")
    ext = ply_path.suffix.lstrip(".").lower()
    if ext not in ACCEPTED_FORMATS:
        raise ValueError(
            f"unsupported splat format '{ext}' — robohack accepts {sorted(ACCEPTED_FORMATS)}"
        )

    size_mb = ply_path.stat().st_size / 1_000_000
    total_attempts = max_retries + 1
    log.info(
        "pushing %s (%.1f MB) → %s  [up to %d attempts]",
        ply_path.name, size_mb, ingest_url, total_attempts,
    )

    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=timeout)
    try:
        for attempt in range(total_attempts):
            attempt_label = f"{attempt + 1}/{total_attempts}"
            if on_progress:
                tag = f"uploading {size_mb:.1f} MB"
                if attempt > 0:
                    tag += f" (retry {attempt_label})"
                on_progress(tag)
            try:
                # Re-open the file each attempt — httpx consumes the stream.
                with ply_path.open("rb") as fh:
                    files = {"file": (ply_path.name, fh, "application/octet-stream")}
                    data: dict[str, str] = {"format": ext}
                    if name:
                        data["name"] = name
                    if run_id:
                        # Links the splat back to the originating scan run.
                        # Robohack's handleRobotSplat persists this on
                        # splats.runId so the /scans "View splat" button can
                        # route to the in-app viewer instead of gs-pot's
                        # local viewer URL.
                        data["runId"] = run_id
                    r = client.post(
                        ingest_url,
                        headers={"Authorization": f"Bearer {token}"},
                        files=files,
                        data=data,
                    )

                if r.status_code in _RETRY_HTTP_STATUSES and attempt + 1 < total_attempts:
                    delay = base_backoff * (2**attempt)
                    log.warning(
                        "push got %d on attempt %s, retrying in %.1fs",
                        r.status_code, attempt_label, delay,
                    )
                    time.sleep(delay)
                    continue

                r.raise_for_status()
                payload = r.json()
                log.info(
                    "ingest accepted on attempt %s: id=%s key=%s",
                    attempt_label, payload.get("id"), payload.get("key"),
                )
                return payload

            except _RETRY_EXCEPTIONS as exc:
                if attempt + 1 < total_attempts:
                    delay = base_backoff * (2**attempt)
                    log.warning(
                        "push %s on attempt %s, retrying in %.1fs",
                        type(exc).__name__, attempt_label, delay,
                    )
                    time.sleep(delay)
                    continue
                raise

        # Loop exits only via return / raise; this guards against future edits.
        raise RuntimeError("push_splat: unreachable — retry loop fell through")
    finally:
        if own_client:
            client.close()
