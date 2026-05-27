"""Ingest contract tests — verify push_splat() honors robohack's
`/api/robot/splat` shape (multipart, Bearer auth, format/name fields).
"""

from pathlib import Path

import httpx
import pytest

from gs_pot.ingest import ACCEPTED_FORMATS, push_splat


@pytest.fixture
def fake_ply(tmp_path: Path) -> Path:
    """Smallest-possible PLY header so we have a real file to upload."""
    p = tmp_path / "scene.ply"
    p.write_bytes(
        b"ply\nformat binary_little_endian 1.0\nelement vertex 0\nend_header\n"
    )
    return p


def _mock_client(handler):
    """Build an httpx.Client backed by a MockTransport so push_splat() doesn't hit the net."""
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


def test_push_splat_uses_bearer_auth(fake_ply: Path) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"key": "splats/x.ply", "id": "splat_x"})

    with _mock_client(handler) as client:
        push_splat(
            fake_ply,
            ingest_url="https://ingest.test/api/robot/splat",
            token="secret-token",
            client=client,
        )
    assert captured["auth"] == "Bearer secret-token"


def test_push_splat_sends_multipart_with_format_and_name(fake_ply: Path) -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["content_type"] = req.headers.get("content-type", "")
        body = req.content
        captured["body"] = body
        return httpx.Response(200, json={"key": "splats/x.ply", "id": "splat_x"})

    with _mock_client(handler) as client:
        push_splat(
            fake_ply,
            ingest_url="https://ingest.test/api/robot/splat",
            token="t",
            name="Apt 3F · living_room",
            client=client,
        )

    assert captured["content_type"].startswith("multipart/form-data")
    body = captured["body"]
    # Multipart bodies include the field names as `name="..."` chunks.
    assert b'name="file"' in body
    assert b'name="format"' in body
    assert b'name="name"' in body
    assert b"ply" in body  # the format field value
    assert b"Apt 3F" in body  # the name field value


def test_push_splat_returns_server_payload(fake_ply: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"key": "splats/abc.ply", "id": "splat_abc"})

    with _mock_client(handler) as client:
        result = push_splat(
            fake_ply,
            ingest_url="https://ingest.test/api/robot/splat",
            token="t",
            client=client,
        )
    assert result == {"key": "splats/abc.ply", "id": "splat_abc"}


def test_push_splat_raises_on_non_2xx(fake_ply: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    with _mock_client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            push_splat(
                fake_ply,
                ingest_url="https://ingest.test/api/robot/splat",
                token="wrong",
                client=client,
            )


def test_push_splat_rejects_unsupported_extension(tmp_path: Path) -> None:
    bad = tmp_path / "scene.txt"
    bad.write_bytes(b"not a splat")
    with pytest.raises(ValueError, match="unsupported splat format"):
        push_splat(bad, ingest_url="https://ingest.test", token="t")


def test_push_splat_404_when_file_missing(tmp_path: Path) -> None:
    missing = tmp_path / "ghost.ply"
    with pytest.raises(FileNotFoundError):
        push_splat(missing, ingest_url="https://ingest.test", token="t")


def test_accepted_formats_matches_robohack() -> None:
    """robohack's SPLAT_EXTS in app/apps/server/src/http/robot.ts:8."""
    assert ACCEPTED_FORMATS == {"ply", "spz", "splat", "ksplat", "sog"}
