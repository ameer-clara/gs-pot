"""Contract tests for the pipeline mode=lidar branch.

We don't run Brush here (heavy compute is off-limits in this env). We
assert the pipeline's *new* glue holds up under the contract we
expect robohack to ship (issue #6 / PR #20):
  * `_payload_to_frame_poses` correctly turns a robohack poses payload
    into FramePose objects, skipping frames without an on-disk
    filename and warning-not-crashing on malformed entries.
  * `_intrinsics_from_payload` accepts both the "intrinsics present"
    and "intrinsics absent" robohack response shapes.
  * `_quat_xyzw_to_rotation` round-trips against identity + a known
    90° z-rotation within float64 noise.
"""

from __future__ import annotations

import numpy as np
import pytest

from gs_pot.lidar_poses import CameraIntrinsics, FramePose
from gs_pot.pipeline import (
    _DEFAULT_GO2_INTRINSICS,
    _intrinsics_from_payload,
    _payload_to_frame_poses,
    _quat_xyzw_to_rotation,
)


def test_payload_to_frame_poses_pairs_filename_via_frame_id() -> None:
    payload = {
        "runId": "scan-abc",
        "intrinsics": None,
        "frames": [
            {
                "frameId": "frame_aaa",
                "tx": 0.0, "ty": 0.0, "tz": 0.0,
                "qx": 0.0, "qy": 0.0, "qz": 0.0, "qw": 1.0,
                "tsNs": "1700000000000000000",
            },
            {
                "frameId": "frame_bbb",
                "tx": 1.0, "ty": 2.0, "tz": 3.0,
                "qx": 0.0, "qy": 0.0, "qz": 0.7071, "qw": 0.7071,
            },
        ],
    }
    filenames = {
        "frame_aaa": "p000_a000_frame_aaa.jpg",
        "frame_bbb": "p001_a090_frame_bbb.jpg",
    }
    out = _payload_to_frame_poses(payload, filenames)
    assert len(out) == 2
    assert isinstance(out[0], FramePose)
    assert out[0].image_name == "p000_a000_frame_aaa.jpg"
    assert out[0].ts_ns == 1700000000000000000
    # identity quaternion → identity rotation, zero translation.
    np.testing.assert_allclose(out[0].T_world_cam, np.eye(4), atol=1e-6)
    # second frame translation populated.
    np.testing.assert_allclose(out[1].T_world_cam[:3, 3], [1, 2, 3], atol=1e-6)


def test_payload_to_frame_poses_drops_unknown_frames() -> None:
    """A pose for a frame with no on-disk filename is logged + skipped — we
    can't train on an image that didn't arrive."""
    payload = {
        "frames": [
            {
                "frameId": "frame_known",
                "tx": 0, "ty": 0, "tz": 0,
                "qx": 0, "qy": 0, "qz": 0, "qw": 1,
            },
            {
                "frameId": "frame_orphan",  # no entry in filenames
                "tx": 0, "ty": 0, "tz": 0,
                "qx": 0, "qy": 0, "qz": 0, "qw": 1,
            },
        ],
    }
    out = _payload_to_frame_poses(
        payload, {"frame_known": "frame_known.jpg"}
    )
    assert len(out) == 1
    assert out[0].image_name == "frame_known.jpg"


def test_payload_to_frame_poses_skips_malformed_entries() -> None:
    """Missing required fields → skip with warning, don't blow up the run."""
    payload = {
        "frames": [
            {"frameId": "good", "tx": 0, "ty": 0, "tz": 0,
             "qx": 0, "qy": 0, "qz": 0, "qw": 1},
            {"frameId": "missing_qw", "tx": 0, "ty": 0, "tz": 0,
             "qx": 0, "qy": 0, "qz": 0},
            "not a dict",
            {"no_frame_id": True},
        ],
    }
    out = _payload_to_frame_poses(
        payload, {"good": "good.jpg", "missing_qw": "missing_qw.jpg"}
    )
    assert len(out) == 1
    assert out[0].image_name == "good.jpg"


def test_payload_to_frame_poses_handles_int_tsns() -> None:
    """Some robohack callers may send tsNs as int instead of string."""
    payload = {
        "frames": [
            {"frameId": "x", "tx": 0, "ty": 0, "tz": 0,
             "qx": 0, "qy": 0, "qz": 0, "qw": 1,
             "tsNs": 1700000000000000000},
        ],
    }
    out = _payload_to_frame_poses(payload, {"x": "x.jpg"})
    assert out[0].ts_ns == 1700000000000000000


def test_payload_to_frame_poses_empty_payload() -> None:
    assert _payload_to_frame_poses({"frames": []}, {}) == []
    assert _payload_to_frame_poses({}, {}) == []


def test_intrinsics_from_payload_uses_default_when_absent() -> None:
    assert _intrinsics_from_payload({"intrinsics": None}) is _DEFAULT_GO2_INTRINSICS
    assert _intrinsics_from_payload({}) is _DEFAULT_GO2_INTRINSICS


def test_intrinsics_from_payload_parses_full_intrinsics() -> None:
    out = _intrinsics_from_payload(
        {
            "intrinsics": {
                "fx": 500.0, "fy": 501.0, "cx": 320.0, "cy": 180.0,
                "width": 640, "height": 360,
                "k1": 0.01, "k2": -0.01, "p1": 0.001, "p2": 0.0,
                "model": "OPENCV",
            }
        }
    )
    assert isinstance(out, CameraIntrinsics)
    assert out.fx == 500.0 and out.fy == 501.0
    assert out.width == 640 and out.height == 360
    assert out.k1 == 0.01


def test_intrinsics_from_payload_falls_back_on_malformed() -> None:
    out = _intrinsics_from_payload({"intrinsics": {"fx": "not-a-number"}})
    assert out is _DEFAULT_GO2_INTRINSICS


def test_quat_xyzw_to_rotation_identity() -> None:
    np.testing.assert_allclose(_quat_xyzw_to_rotation(0, 0, 0, 1), np.eye(3))


def test_quat_xyzw_to_rotation_90deg_about_z() -> None:
    R = _quat_xyzw_to_rotation(0, 0, 0.7071067811865475, 0.7071067811865476)
    expected = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]], dtype=np.float64)
    np.testing.assert_allclose(R, expected, atol=1e-6)


def test_quat_xyzw_to_rotation_normalizes_input() -> None:
    """Pass a deliberately non-unit quaternion; the helper should normalize
    rather than emit garbage (JSON round-tripping can lose a digit)."""
    R = _quat_xyzw_to_rotation(0, 0, 1.4142, 1.4142)
    # Same direction as the previous test (90° about z), pre-normalize.
    expected = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]], dtype=np.float64)
    np.testing.assert_allclose(R, expected, atol=1e-4)


def test_quat_xyzw_to_rotation_zero_quaternion_returns_identity() -> None:
    """Degenerate input (all-zero quaternion) returns identity instead of
    a NaN matrix — defensive against an upstream bug."""
    R = _quat_xyzw_to_rotation(0, 0, 0, 0)
    np.testing.assert_allclose(R, np.eye(3))


# ── runs.py helpers: contract checks against mocked robohack responses ───────


def test_list_frame_filenames_builds_id_to_filename_map(monkeypatch) -> None:
    """list_frame_filenames must produce the same filenames fetch_run writes:
    `p{position:03d}_a{angle:03d}_{id}.jpg`."""
    import httpx

    payload = {
        "scans": [
            {
                "run": "scan-foo",
                "positions": [
                    {
                        "position": 0,
                        "images": [
                            {"id": "frame_1", "angle": 0.0, "url": "x"},
                            {"id": "frame_2", "angle": 90.4, "url": "y"},
                        ],
                    },
                    {
                        "position": 7,
                        "images": [
                            {"id": "frame_3", "angle": None, "url": "z"},
                        ],
                    },
                ],
            },
            # An unrelated run that must NOT bleed into the result.
            {"run": "other", "positions": [
                {"position": 0, "images": [{"id": "noise", "angle": 0, "url": "n"}]}
            ]},
        ]
    }

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/scans/scan-foo"
        return httpx.Response(200, json=payload)

    # Snapshot the real httpx.Client BEFORE monkeypatching so the lambda
    # doesn't re-enter itself when called from the SUT.
    real_client = httpx.Client
    monkeypatch.setattr(
        "gs_pot.runs.httpx.Client",
        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)),
    )

    from gs_pot.runs import list_frame_filenames
    out = list_frame_filenames("https://r.example", "scan-foo")
    assert out == {
        "frame_1": "p000_a000_frame_1.jpg",
        "frame_2": "p000_a090_frame_2.jpg",
        "frame_3": "p007_axxx_frame_3.jpg",
    }


def test_fetch_poses_returns_payload_verbatim(monkeypatch) -> None:
    import httpx

    payload = {
        "runId": "scan-foo",
        "intrinsics": None,
        "frames": [
            {"frameId": "f1", "tx": 0, "ty": 0, "tz": 0,
             "qx": 0, "qy": 0, "qz": 0, "qw": 1},
        ],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/scans/scan-foo/poses"
        return httpx.Response(200, json=payload)

    # Snapshot the real httpx.Client BEFORE monkeypatching so the lambda
    # doesn't re-enter itself when called from the SUT.
    real_client = httpx.Client
    monkeypatch.setattr(
        "gs_pot.runs.httpx.Client",
        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)),
    )

    from gs_pot.runs import fetch_poses
    out = fetch_poses("https://r.example", "scan-foo")
    assert out == payload


def test_fetch_poses_raises_on_404(monkeypatch) -> None:
    import httpx

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    # Snapshot the real httpx.Client BEFORE monkeypatching so the lambda
    # doesn't re-enter itself when called from the SUT.
    real_client = httpx.Client
    monkeypatch.setattr(
        "gs_pot.runs.httpx.Client",
        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)),
    )

    from gs_pot.runs import fetch_poses
    with pytest.raises(httpx.HTTPStatusError):
        fetch_poses("https://r.example", "scan-missing")
