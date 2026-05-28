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
    PhaseStats,
    _derive_recommendation,
    _phase_stats,
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
    cfg = DetectorConfig(k_sigma=1.5, hyst_motion_ms=0, hyst_quiet_ms=0, invert=False)
    det = MotionDetector(baseline, cfg)
    assert det.threshold == pytest.approx(50.0 + 1.5 * 2.0)


def test_detector_quiet_to_motion_then_back(baseline: Baseline) -> None:
    cfg = DetectorConfig(k_sigma=1.0, hyst_motion_ms=1000, hyst_quiet_ms=1000, invert=False)
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
    cfg = DetectorConfig(k_sigma=1.0, hyst_motion_ms=2000, hyst_quiet_ms=2000, invert=False)
    det = MotionDetector(baseline, cfg)
    t = 0
    for v in [80.0, 45.0, 45.0, 45.0, 45.0]:
        det.push(v, now_ms=t)
        t += 200
    assert det.state == "quiet"


def test_detector_rolling_baseline_only_absorbs_quiet_samples(baseline: Baseline) -> None:
    cfg = DetectorConfig(
        k_sigma=2.0, hyst_motion_ms=0, hyst_quiet_ms=0, rolling_window_size=10, invert=False
    )
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


# ── Multi-phase calibration unit tests ───────────────────────────────────────


def test_phase_stats_basic() -> None:
    samples = [10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0, 26.0, 28.0]
    stats = _phase_stats(samples)
    assert stats is not None
    assert stats.n == 10
    assert stats.mean == pytest.approx(19.0)
    assert stats.min == 10.0
    assert stats.max == 28.0
    assert stats.p5 <= stats.p95


def test_phase_stats_empty_returns_none() -> None:
    assert _phase_stats([]) is None


def test_derive_recommendation_clean_gap() -> None:
    # Empty noise floor sits below the walking distribution with a clear gap.
    empty = PhaseStats(n=60, mean=50.0, std=3.0, min=42.0, max=58.0, p5=44.0, p95=56.0)
    walk = PhaseStats(n=60, mean=80.0, std=5.0, min=68.0, max=92.0, p5=70.0, p95=89.0)
    k, thr, warn = _derive_recommendation(empty, walk)
    assert warn is None
    assert thr == pytest.approx((56.0 + 70.0) / 2.0)  # midpoint
    # k_sigma chosen so threshold = empty.mean + k * empty.std
    assert k == pytest.approx((thr - 50.0) / 3.0, abs=0.05)


def test_derive_recommendation_overlap_returns_warning() -> None:
    # Walking distribution dips below the empty noise ceiling — no clean gap.
    empty = PhaseStats(n=60, mean=55.0, std=10.0, min=35.0, max=75.0, p5=40.0, p95=72.0)
    walk = PhaseStats(n=60, mean=65.0, std=8.0, min=50.0, max=80.0, p5=52.0, p95=78.0)
    k, thr, warn = _derive_recommendation(empty, walk)
    assert warn is not None
    assert "overlap" in warn.lower()
    assert k == pytest.approx(1.3)  # fallback


def test_motion_detector_uses_baseline_recommended_k_sigma(baseline: Baseline) -> None:
    # Baseline has a recommendation; detector cfg leaves k_sigma=None.
    base_with_rec = baseline.model_copy(update={"recommended_k_sigma": 0.8})
    det = MotionDetector(base_with_rec, DetectorConfig())  # k_sigma=None
    assert det.effective_k_sigma == 0.8
    # Explicit override still wins.
    det2 = MotionDetector(base_with_rec, DetectorConfig(k_sigma=2.5))
    assert det2.effective_k_sigma == 2.5


def test_motion_detector_falls_back_to_default_when_no_recommendation(baseline: Baseline) -> None:
    det = MotionDetector(baseline, DetectorConfig())
    # Room default: 0.7 (was 1.3 pre-hackathon tuning; see DetectorConfig docstring).
    assert det.effective_k_sigma == 0.7


def test_inverted_detector_fires_on_value_drop(baseline: Baseline) -> None:
    # Baseline μ=50 σ=2 → inverted threshold = 50 − 1·2 = 48.
    cfg = DetectorConfig(k_sigma=1.0, invert=True, hyst_motion_ms=0, hyst_quiet_ms=0)
    det = MotionDetector(baseline, cfg)
    assert det.threshold == pytest.approx(48.0)

    # Value ABOVE inverted threshold = quiet (signal still "present").
    assert det.push(60.0, now_ms=0) is None
    assert det.state == "quiet"

    # Value DROPS below threshold → motion transition.
    tr = det.push(40.0, now_ms=100)
    assert tr is not None
    assert tr["from"] == "quiet" and tr["to"] == "motion"

    # Recover above threshold → back to quiet.
    tr2 = det.push(60.0, now_ms=200)
    assert tr2 is not None
    assert tr2["from"] == "motion" and tr2["to"] == "quiet"
