"""Unit tests for camera stream health + the self-healing watchdog."""
import time

from backend.sony_hub import SonyFrameHub
from backend.watchdog import CameraWatchdog
import backend.watchdog as wd_mod


# ---- hub health / stall detection ----------------------------------------

def test_stalled_false_when_not_connected():
    h = SonyFrameHub()
    h.connected = False
    assert h.stalled(25) is False


def test_stalled_true_when_connected_but_no_frames():
    h = SonyFrameHub()
    h.connected = True
    h.connected_since = time.time() - 30   # connected 30s ago, never a frame
    h.last_frame = 0
    assert h.stalled(25) is True


def test_not_stalled_while_streaming():
    h = SonyFrameHub()
    h.connected = True
    h.connected_since = time.time() - 60
    h.last_frame = time.time()             # fresh frame just now
    assert h.stalled(25) is False


def test_health_reports_streaming_and_fps():
    h = SonyFrameHub()
    h.connected = True
    h.last_frame = time.time()
    h.fps = 29.5
    out = h.health()
    assert out["streaming"] is True
    assert out["fps"] == 29.5
    assert out["age_s"] is not None and out["age_s"] < 1


def test_health_not_streaming_when_stale():
    h = SonyFrameHub()
    h.connected = True
    h.last_frame = time.time() - 10
    out = h.health()
    assert out["streaming"] is False
    assert out["fps"] == 0.0


# ---- watchdog decision logic ---------------------------------------------

class _Cfg:
    def __init__(self, source="sony_http"):
        self.source = source

    def load(self):
        src = self.source
        return type("S", (), {"preview": type("P", (), {"source": src})()})()


class _Hub:
    def __init__(self, stalled):
        self._stalled = stalled

    def stalled(self, threshold):
        return self._stalled


def _watchdog(monkeypatch, hub, cfg, busy=False, cooldown=0.0):
    calls = []
    monkeypatch.setattr(wd_mod.subprocess, "Popen", lambda *a, **k: calls.append(a))
    w = CameraWatchdog(hub, cfg, lambda: busy, stall_s=1, cooldown_s=cooldown, check_s=0.01)
    return w, calls


def test_watchdog_restarts_on_stall(monkeypatch):
    w, calls = _watchdog(monkeypatch, _Hub(True), _Cfg("sony_http"))
    assert w._tick() is True
    assert len(calls) == 1 and w.restarts == 1


def test_watchdog_ignores_when_streaming(monkeypatch):
    w, calls = _watchdog(monkeypatch, _Hub(False), _Cfg("sony_http"))
    assert w._tick() is False
    assert calls == []


def test_watchdog_ignores_non_sony(monkeypatch):
    w, calls = _watchdog(monkeypatch, _Hub(True), _Cfg("mock"))
    assert w._tick() is False
    assert calls == []


def test_watchdog_skips_during_capture(monkeypatch):
    w, calls = _watchdog(monkeypatch, _Hub(True), _Cfg("sony_http"), busy=True)
    assert w._tick() is False
    assert calls == []


def test_watchdog_respects_cooldown(monkeypatch):
    w, calls = _watchdog(monkeypatch, _Hub(True), _Cfg("sony_http"), cooldown=999)
    assert w._tick() is True          # first acts
    assert w._tick() is False         # second blocked by cooldown
    assert len(calls) == 1
