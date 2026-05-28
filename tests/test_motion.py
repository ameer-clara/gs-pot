"""Tests for the motion-alert bridge.

Network paths (sensing-server poll, webhook POST) are not exercised here —
they're better covered by an integration test against a live sensing-server.
What we test:

  - The detector's hysteresis state machine (the bit most likely to regress).
  - The fire_bark dispatcher's mode handling, with the actual subprocess
    suppressed via monkeypatch.
  - The HTTP shape of /api/motion/* endpoints (sans network) via FastAPI's
    TestClient.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from gs_pot.motion import (
    Baseline,
    BarkConfig,
    DetectorConfig,
    MotionDetector,
    fire_bark,
    stop_monitor,
)
from gs_pot.server import app


@pytest.fixture(autouse=True)
def _stop_monitor_after_each_test():
    yield
    stop_monitor()


@pytest.fixture
def baseline() -> Baseline:
    return Baseline(
        mean=50.0,
        std=2.0,
        n_samples=60,
        captured_at=datetime.now(UTC),
        source_url="http://localhost:3000/api/v1/sensing/latest",
    )


def test_detector_threshold_uses_k_sigma(baseline: Baseline) -> None:
    cfg = DetectorConfig(k_sigma=1.5, hyst_motion_ms=0, hyst_quiet_ms=0)
    det = MotionDetector(baseline, cfg)
    assert det.threshold == pytest.approx(50.0 + 1.5 * 2.0)


def test_detector_quiet_to_motion_then_back(baseline: Baseline) -> None:
    cfg = DetectorConfig(k_sigma=1.0, hyst_motion_ms=1000, hyst_quiet_ms=1000)
    det = MotionDetector(baseline, cfg)
    # Threshold ≈ 52.0. Feed quiet samples first → stays quiet.
    t = 0
    for _ in range(5):
        assert det.push(48.0, now_ms=t) is None
        t += 200
    assert det.state == "quiet"

    # Sustained above-threshold samples → flip to motion after ≥1000ms.
    flips: list[dict] = []
    for _ in range(20):
        tr = det.push(80.0, now_ms=t)
        if tr is not None:
            flips.append(tr)
        t += 200
    assert any(tr["from"] == "quiet" and tr["to"] == "motion" for tr in flips)
    assert det.state == "motion"

    # Drop back to quiet samples → flip back after hyst_quiet_ms.
    flips2: list[dict] = []
    for _ in range(20):
        tr = det.push(45.0, now_ms=t)
        if tr is not None:
            flips2.append(tr)
        t += 200
    assert any(tr["from"] == "motion" and tr["to"] == "quiet" for tr in flips2)
    assert det.state == "quiet"


def test_detector_brief_above_threshold_doesnt_flip(baseline: Baseline) -> None:
    """A single short spike must not flip state — hysteresis exists for this."""
    cfg = DetectorConfig(k_sigma=1.0, hyst_motion_ms=2000, hyst_quiet_ms=2000)
    det = MotionDetector(baseline, cfg)
    t = 0
    for v in [80.0, 45.0, 45.0, 45.0, 45.0]:
        det.push(v, now_ms=t)
        t += 200
    assert det.state == "quiet"


def test_detector_rolling_baseline_only_absorbs_quiet_samples(baseline: Baseline) -> None:
    cfg = DetectorConfig(k_sigma=2.0, hyst_motion_ms=0, hyst_quiet_ms=0, rolling_window_size=10)
    det = MotionDetector(baseline, cfg)
    # All below-threshold samples — should rebuild rolling mean down toward 45.
    for _ in range(10):
        det.push(45.0)
    # After enough quiet samples, the detector's mean tracks the buffer
    # (not the seed). 6 sample warm-up is required by _refresh_stats.
    assert det.mean == pytest.approx(45.0)


def test_fire_bark_noop_mode_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        "gs_pot.motion.subprocess.Popen",
        lambda *a, **kw: spawned.append(list(a[0])) or _DummyProc(),
    )
    r = fire_bark(BarkConfig(mode="noop"))
    assert r == {"mode": "noop"}
    assert spawned == []


def test_fire_bark_say_invokes_say_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        "gs_pot.motion.subprocess.Popen",
        lambda *a, **kw: spawned.append(list(a[0])) or _DummyProc(),
    )
    monkeypatch.setattr("gs_pot.motion.shutil.which", lambda b: f"/usr/bin/{b}")
    r = fire_bark(BarkConfig(mode="say", say_phrase="hello", say_voice="Alex"))
    assert r["mode"] == "say"
    assert spawned and spawned[0][0] == "say"
    assert "hello" in spawned[0]
    assert "Alex" in spawned[0]


def test_motion_status_endpoint_when_idle() -> None:
    client = TestClient(app)
    r = client.get("/api/motion/status")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is False
    assert body["samples_seen"] == 0


def test_motion_start_without_baseline_returns_400(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # Point the baseline file at a non-existent path so load_baseline returns None.
    import gs_pot.motion as motion_mod

    monkeypatch.setattr(motion_mod, "BASELINE_FILE", tmp_path / "no-such-file.json")

    client = TestClient(app)
    r = client.post("/api/motion/start", json={"sensing_url": "http://localhost:3000/api/v1/sensing/latest"})
    assert r.status_code == 400
    assert "baseline" in r.json()["detail"].lower()


class _DummyProc:
    def __init__(self) -> None:
        self.pid = 0
