"""Self-healing camera watchdog.

The `cameraDaemon` can be 'up and serving :8080' yet produce no live-view frames
(a camera-state glitch we hit repeatedly). systemd can't catch that — the process
is alive — so the booth would sit with a frozen preview until someone restarted it
by hand. This watchdog detects the stall and restarts the daemon automatically: the
exact recovery an operator would do, but unattended.

It only acts when the daemon is reachable (hub.connected) but stalled. When the
camera is genuinely off, the daemon crash-loops and isn't serving HTTP, so
hub.connected is False and we leave systemd's own retry loop alone — no fighting.
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time

log = logging.getLogger("booth")


class CameraWatchdog:
    def __init__(self, hub, config_mod, busy_fn,
                 stall_s: float = 25.0, cooldown_s: float = 90.0, check_s: float = 8.0):
        self.hub = hub
        self.config = config_mod
        self.busy_fn = busy_fn
        self.stall_s = stall_s
        self.cooldown_s = cooldown_s
        self.check_s = check_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_action = 0.0
        self.restarts = 0
        self.last_reason = ""

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        log.info("[watchdog] started (stall>%ss -> restart camera daemon)", self.stall_s)
        while not self._stop.wait(self.check_s):
            try:
                self._tick()
            except Exception as e:  # never let the watchdog die
                log.warning("[watchdog] error: %s", e)

    def _tick(self) -> bool:
        """One health check. Returns True if it triggered a daemon restart."""
        s = self.config.load()
        if s.preview.source != "sony_http":
            return False
        if self.busy_fn():
            return False  # never restart mid-capture
        if not self.hub.stalled(self.stall_s):
            return False
        now = time.time()
        if now - self._last_action < self.cooldown_s:
            return False  # respect cooldown so we don't restart-storm
        self._last_action = now
        self.restarts += 1
        self.last_reason = f"live view stalled >{self.stall_s}s while daemon up"
        log.warning("[watchdog] %s — auto-restarting camera daemon (#%d)",
                    self.last_reason, self.restarts)
        # detached so a slow systemctl can't block this thread
        subprocess.Popen(["sh", "-c", "systemctl restart photobooth-liveview.service"])
        return True

    def stop(self) -> None:
        self._stop.set()
