"""Motion-alert bridge: WiFi-CSI sensing-server → bark on the robot.

A long-running background monitor polls a WiFi-CSI sensing-server's
`features.motion_band_power`, scores each sample against an adaptive
empty-room baseline, and on a `quiet → motion` transition (a) fires a
bark via the configured strategy and (b) POSTs the transition to an
optional caller-supplied webhook.

Architectural fit: matches gs-pot's existing background-worker pattern
(see `_JobQueue` in `server.py`) — a single thread, with `start_monitor`
/ `stop_monitor` keeping it idempotent so the same endpoint can safely
be re-called.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import statistics
import subprocess
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# ── Persistent baseline file ──────────────────────────────────────────────────

BASELINE_FILE = Path(
    os.environ.get("GS_POT_MOTION_BASELINE_FILE", "motion_baseline.json")
)

DEFAULT_SENSING_URL = "http://localhost:3000/api/v1/sensing/latest"
DEFAULT_FIELD_PATH = ("features", "motion_band_power")


# ── Pydantic API models ───────────────────────────────────────────────────────


class Baseline(BaseModel):
    mean: float
    std: float
    n_samples: int
    captured_at: datetime
    source_url: str
    note: str | None = None


class DetectorConfig(BaseModel):
    k_sigma: float = Field(default=1.3, gt=0)
    hyst_motion_ms: int = Field(default=1500, ge=0)
    hyst_quiet_ms: int = Field(default=5000, ge=0)
    rolling_window_size: int = Field(default=60, ge=6)
    poll_ms: int = Field(default=500, ge=100)
    std_floor: float = Field(default=2.0, gt=0)


class BarkConfig(BaseModel):
    mode: Literal["dimos", "afplay", "say", "noop"] = "say"
    audio_path: str | None = None
    say_phrase: str = "WOOF! WOOF! WOOF!"
    say_voice: str = "Fred"
    dimos_cmd: list[str] = Field(
        default_factory=lambda: ["dimos", "mcp", "call", "UnitreeSpeak", "--arg", "text=bark bark"]
    )
    cooldown_s: float = 3.0


class MotionStartRequest(BaseModel):
    sensing_url: str = DEFAULT_SENSING_URL
    webhook_url: str | None = None
    detector: DetectorConfig | None = None
    bark: BarkConfig | None = None
    baseline_override: Baseline | None = None
    max_duration_s: int | None = Field(default=None, ge=1, le=86400)
    """Auto-stop after this many seconds. Omit for no limit."""


class MotionStartResponse(BaseModel):
    status: Literal["started", "already_running"]
    started_at: datetime
    sensing_url: str
    baseline: Baseline
    threshold: float
    webhook_url: str | None
    bark_mode: str


class MotionStopResponse(BaseModel):
    status: Literal["stopped", "not_running"]
    stopped_at: datetime
    transitions: int
    barks: int


class MotionStatus(BaseModel):
    running: bool
    started_at: datetime | None
    sensing_url: str | None
    state: Literal["quiet", "motion"] | None
    state_since: datetime | None
    last_sample: float | None
    threshold: float | None
    baseline: Baseline | None
    samples_seen: int
    transitions: int
    barks: int
    last_webhook_status: str | None


class MotionCalibrateRequest(BaseModel):
    sensing_url: str = DEFAULT_SENSING_URL
    seconds: int = Field(default=30, ge=5, le=600)
    poll_ms: int = Field(default=500, ge=100)
    trim_fraction: float = Field(default=0.05, ge=0, lt=0.5)
    persist: bool = True
    note: str | None = None


class MotionCalibrateResponse(BaseModel):
    baseline: Baseline
    raw_n_samples: int
    trimmed_n_samples: int
    persisted_to: str | None


class BarkRequest(BaseModel):
    bark: BarkConfig | None = None


# ── Detector (port of the validated JS rolling-baseline state machine) ────────


class MotionDetector:
    def __init__(self, baseline: Baseline, cfg: DetectorConfig) -> None:
        self._seed_mean = baseline.mean
        self._seed_std = max(baseline.std, 1.0)
        self._cfg = cfg

        self._quiet_buf: deque[float] = deque(maxlen=cfg.rolling_window_size)
        self.mean = self._seed_mean
        self.std = self._seed_std

        self.state: Literal["quiet", "motion"] = "quiet"
        now_ms = _now_ms()
        self.state_since_ms = now_ms
        self._cand_state: Literal["quiet", "motion"] = "quiet"
        self._cand_since_ms = now_ms

    @property
    def threshold(self) -> float:
        return self.mean + self._cfg.k_sigma * self.std

    def _refresh_stats(self) -> None:
        if len(self._quiet_buf) < 6:
            self.mean = self._seed_mean
            self.std = self._seed_std
            return
        self.mean = statistics.fmean(self._quiet_buf)
        # statistics.pstdev is population stdev — what we want for our buffered window
        self.std = max(statistics.pstdev(self._quiet_buf), self._cfg.std_floor)

    def push(self, value: float, now_ms: int | None = None) -> dict[str, Any] | None:
        """Feed one sample. Returns a transition dict on state flip, else None."""
        now_ms = _now_ms() if now_ms is None else now_ms
        above = value > self.threshold
        obs: Literal["quiet", "motion"] = "motion" if above else "quiet"

        if obs != self._cand_state:
            self._cand_state = obs
            self._cand_since_ms = now_ms

        transition: dict[str, Any] | None = None
        if self._cand_state != self.state:
            need_ms = (
                self._cfg.hyst_motion_ms if self._cand_state == "motion" else self._cfg.hyst_quiet_ms
            )
            if now_ms - self._cand_since_ms >= need_ms:
                prev_state = self.state
                prev_dwell_s = (now_ms - self.state_since_ms) / 1000.0
                self.state = self._cand_state
                self.state_since_ms = now_ms
                transition = {
                    "from": prev_state,
                    "to": self.state,
                    "at": now_ms / 1000.0,
                    "prev_dwell_s": prev_dwell_s,
                    "value": value,
                    "threshold": self.threshold,
                    "baseline": {"mean": self.mean, "std": self.std, "n": len(self._quiet_buf)},
                }

        # Only consistently-quiet samples feed the rolling baseline.
        if self.state == "quiet" and obs == "quiet":
            self._quiet_buf.append(value)
            self._refresh_stats()

        return transition


def _now_ms() -> int:
    return int(time.time() * 1000)


# ── Bark strategies ───────────────────────────────────────────────────────────


def fire_bark(cfg: BarkConfig) -> dict[str, Any]:
    """Fire one bark using the configured mode. Non-blocking subprocess launch."""
    if cfg.mode == "noop":
        return {"mode": "noop"}
    if cfg.mode == "afplay":
        path = cfg.audio_path or ""
        if not path or not Path(path).exists():
            log.warning("bark: afplay path missing/empty (%r) — falling back to say", path)
            return _spawn_say(cfg)
        if shutil.which("afplay") is None:
            log.warning("bark: afplay binary not on PATH — falling back to say")
            return _spawn_say(cfg)
        subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"mode": "afplay", "path": path}
    if cfg.mode == "dimos":
        cmd = list(cfg.dimos_cmd)
        if shutil.which(cmd[0]) is None:
            log.warning("bark: %s not on PATH — falling back to say (dev mode)", cmd[0])
            return _spawn_say(cfg)
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"mode": "dimos", "cmd": cmd}
    # default: say
    return _spawn_say(cfg)


def _spawn_say(cfg: BarkConfig) -> dict[str, Any]:
    if shutil.which("say") is None:
        log.warning("bark: `say` not on PATH; bark suppressed")
        return {"mode": "noop", "reason": "no say binary"}
    subprocess.Popen(
        ["say", "-v", cfg.say_voice, "-r", "220", cfg.say_phrase],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"mode": "say", "phrase": cfg.say_phrase, "voice": cfg.say_voice}


# ── Sensing-server poll ───────────────────────────────────────────────────────


def _read_motion_power(client: httpx.Client, url: str) -> float | None:
    """Pull motion_band_power from a sensing-server's /sensing/latest JSON."""
    try:
        r = client.get(url, timeout=2.0)
        if r.status_code >= 400:
            return None
        j = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    cursor: Any = j
    for k in DEFAULT_FIELD_PATH:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(k)
    return cursor if isinstance(cursor, (int, float)) else None


# ── Baseline persistence ──────────────────────────────────────────────────────


def load_baseline(path: Path | None = None) -> Baseline | None:
    # Resolve at call time so test monkeypatch of BASELINE_FILE takes effect.
    p = path if path is not None else BASELINE_FILE
    if not p.exists():
        return None
    try:
        return Baseline.model_validate_json(p.read_text())
    except Exception:
        log.exception("failed to load baseline from %s", p)
        return None


def save_baseline(b: Baseline, path: Path | None = None) -> None:
    p = path if path is not None else BASELINE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(b.model_dump_json(indent=2) + "\n")


def calibrate_sync(req: MotionCalibrateRequest) -> MotionCalibrateResponse:
    """Block on the calling thread, sample for `seconds`, return a baseline."""
    samples: list[float] = []
    poll_s = req.poll_ms / 1000.0
    deadline = time.time() + req.seconds
    with httpx.Client() as client:
        while time.time() < deadline:
            v = _read_motion_power(client, req.sensing_url)
            if v is not None:
                samples.append(v)
            time.sleep(poll_s)

    if len(samples) < 10:
        raise ValueError(
            f"only {len(samples)} samples collected from {req.sensing_url}; "
            f"is the sensing-server up and emitting features.motion_band_power?"
        )

    samples_sorted = sorted(samples)
    trim = int(len(samples_sorted) * req.trim_fraction)
    core = samples_sorted[trim : len(samples_sorted) - trim] if trim else samples_sorted
    mean = round(statistics.fmean(core), 2)
    std = round(statistics.pstdev(core), 2)

    baseline = Baseline(
        mean=mean,
        std=std,
        n_samples=len(samples),
        captured_at=datetime.now(UTC),
        source_url=req.sensing_url,
        note=req.note or f"calibrated via POST /api/motion/calibrate ({req.seconds}s)",
    )
    persisted: str | None = None
    if req.persist:
        save_baseline(baseline)
        persisted = str(BASELINE_FILE)

    return MotionCalibrateResponse(
        baseline=baseline,
        raw_n_samples=len(samples),
        trimmed_n_samples=len(core),
        persisted_to=persisted,
    )


# ── Monitor (singleton, runs in a daemon thread) ─────────────────────────────


class _Monitor:
    def __init__(
        self,
        baseline: Baseline,
        sensing_url: str,
        detector: DetectorConfig,
        bark: BarkConfig,
        webhook_url: str | None,
        max_duration_s: int | None,
    ) -> None:
        self.baseline = baseline
        self.sensing_url = sensing_url
        self.detector = MotionDetector(baseline, detector)
        self.detector_cfg = detector
        self.bark_cfg = bark
        self.webhook_url = webhook_url
        self.max_duration_s = max_duration_s

        self.started_at = datetime.now(UTC)
        self.samples_seen = 0
        self.transitions = 0
        self.barks = 0
        self.last_sample: float | None = None
        self.last_webhook_status: str | None = None
        self._last_bark_ms = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="gs-pot-motion", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def _run(self) -> None:
        log.info(
            "motion monitor: polling %s every %dms (μ=%.2f σ=%.2f thr≈%.2f, webhook=%s, bark=%s)",
            self.sensing_url,
            self.detector_cfg.poll_ms,
            self.baseline.mean,
            self.baseline.std,
            self.detector.threshold,
            self.webhook_url or "-",
            self.bark_cfg.mode,
        )
        deadline = (
            time.time() + self.max_duration_s if self.max_duration_s is not None else None
        )
        poll_s = self.detector_cfg.poll_ms / 1000.0
        with httpx.Client() as client:
            while not self._stop.is_set():
                if deadline is not None and time.time() >= deadline:
                    log.info("motion monitor: max_duration_s reached, stopping")
                    break
                v = _read_motion_power(client, self.sensing_url)
                if v is None:
                    self._stop.wait(poll_s)
                    continue
                self.samples_seen += 1
                self.last_sample = v
                tr = self.detector.push(v)
                if tr is not None:
                    self.transitions += 1
                    log.info(
                        "motion transition %s -> %s (value=%.2f thr=%.2f dwell=%.1fs)",
                        tr["from"], tr["to"], tr["value"], tr["threshold"], tr["prev_dwell_s"],
                    )
                    if tr["to"] == "motion" and self._cooldown_ok():
                        result = fire_bark(self.bark_cfg)
                        self.barks += 1
                        self._last_bark_ms = _now_ms()
                        log.info("bark fired: %s", result)
                    self._post_webhook(tr, client)
                self._stop.wait(poll_s)
        log.info(
            "motion monitor stopped: samples=%d transitions=%d barks=%d",
            self.samples_seen, self.transitions, self.barks,
        )

    def _cooldown_ok(self) -> bool:
        if self._last_bark_ms == 0:
            return True
        return (_now_ms() - self._last_bark_ms) >= int(self.bark_cfg.cooldown_s * 1000)

    def _post_webhook(self, tr: dict[str, Any], client: httpx.Client) -> None:
        if not self.webhook_url:
            self.last_webhook_status = "skipped (no webhook_url)"
            return
        payload = {
            "type": "gs_pot.motion_transition",
            "transition": tr,
            "source": {"url": self.sensing_url, "field": ".".join(DEFAULT_FIELD_PATH)},
            "saved_baseline": json.loads(self.baseline.model_dump_json()),
        }
        try:
            r = client.post(self.webhook_url, json=payload, timeout=3.0)
            self.last_webhook_status = f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            self.last_webhook_status = f"error: {e!s}"
            log.warning("webhook POST failed: %s", e)

    def status(self) -> MotionStatus:
        return MotionStatus(
            running=self._thread.is_alive(),
            started_at=self.started_at,
            sensing_url=self.sensing_url,
            state=self.detector.state,
            state_since=datetime.fromtimestamp(self.detector.state_since_ms / 1000.0, UTC),
            last_sample=self.last_sample,
            threshold=self.detector.threshold,
            baseline=self.baseline,
            samples_seen=self.samples_seen,
            transitions=self.transitions,
            barks=self.barks,
            last_webhook_status=self.last_webhook_status,
        )


# Module-level singleton + lock.
_lock = threading.Lock()
_monitor: _Monitor | None = None


def start_monitor(req: MotionStartRequest) -> tuple[Literal["started", "already_running"], _Monitor]:
    global _monitor
    with _lock:
        if _monitor is not None and _monitor._thread.is_alive():
            return ("already_running", _monitor)

        baseline = req.baseline_override or load_baseline()
        if baseline is None:
            raise ValueError(
                f"no baseline available — pass `baseline_override` or run "
                f"POST /api/motion/calibrate first (looking for {BASELINE_FILE})"
            )
        m = _Monitor(
            baseline=baseline,
            sensing_url=req.sensing_url,
            detector=req.detector or DetectorConfig(),
            bark=req.bark or BarkConfig(),
            webhook_url=req.webhook_url,
            max_duration_s=req.max_duration_s,
        )
        _monitor = m
        m.start()
        return ("started", m)


def stop_monitor() -> MotionStopResponse:
    global _monitor
    with _lock:
        if _monitor is None or not _monitor._thread.is_alive():
            return MotionStopResponse(
                status="not_running",
                stopped_at=datetime.now(UTC),
                transitions=0,
                barks=0,
            )
        m = _monitor
        m.stop()
        resp = MotionStopResponse(
            status="stopped",
            stopped_at=datetime.now(UTC),
            transitions=m.transitions,
            barks=m.barks,
        )
        _monitor = None
        return resp


def current_status() -> MotionStatus:
    with _lock:
        if _monitor is None or not _monitor._thread.is_alive():
            return MotionStatus(
                running=False,
                started_at=None,
                sensing_url=None,
                state=None,
                state_since=None,
                last_sample=None,
                threshold=None,
                baseline=load_baseline(),
                samples_seen=0,
                transitions=0,
                barks=0,
                last_webhook_status=None,
            )
        return _monitor.status()
