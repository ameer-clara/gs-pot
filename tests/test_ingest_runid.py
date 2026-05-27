"""push_splat run_id contract — robohack's splats.runId FK depends on this
field being on the wire when (and only when) the caller supplies it.
"""

from pathlib import Path

import httpx
import pytest

from gs_pot.ingest import push_splat


@pytest.fixture
def fake_ply(tmp_path: Path) -> Path:
    p = tmp_path / "scene.ply"
    p.write_bytes(
        b"ply\nformat binary_little_endian 1.0\nelement vertex 0\nend_header\n"
    )
    return p


def _mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_push_splat_includes_runid_when_supplied(fake_ply: Path) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = req.content
        return httpx.Response(200, json={"key": "splats/x.ply", "id": "splat_x"})

    with _mock_client(handler) as client:
        push_splat(
            fake_ply,
            ingest_url="https://ingest.test/api/robot/splat",
            token="t",
            run_id="scan-1779883830",
            client=client,
        )

    body = captured["body"]
    # Multipart field present + value reaches the server verbatim.
    assert b'name="runId"' in body
    assert b"scan-1779883830" in body


def test_push_splat_omits_runid_when_none(fake_ply: Path) -> None:
    """Default call (no run_id) must not put a runId form field on the wire —
    keeps backward-compat for legacy / non-scan splat uploads.
    """
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = req.content
        return httpx.Response(200, json={"key": "splats/x.ply", "id": "splat_x"})

    with _mock_client(handler) as client:
        push_splat(
            fake_ply,
            ingest_url="https://ingest.test/api/robot/splat",
            token="t",
            client=client,
        )

    assert b'name="runId"' not in captured["body"]


def test_push_splat_omits_runid_when_empty_string(fake_ply: Path) -> None:
    """Empty string is treated as 'not supplied' (mirrors the `if name:` /
    `if run_id:` truthy gates in ingest.py)."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = req.content
        return httpx.Response(200, json={"key": "splats/x.ply", "id": "splat_x"})

    with _mock_client(handler) as client:
        push_splat(
            fake_ply,
            ingest_url="https://ingest.test/api/robot/splat",
            token="t",
            run_id="",
            client=client,
        )

    assert b'name="runId"' not in captured["body"]
