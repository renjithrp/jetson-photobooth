"""Orchestrates a capture session and emits UI events along the way."""
from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import qrcode

from . import config, processing, uploaders
from .camera import CaptureError, daemon_capture, make_camera
from .events import bus

# Hold captures until the system clock is sane. After a cold boot the Jetson's
# clock starts at the 1970 epoch until NTP syncs; capturing in that window
# produced 1969-timestamped sessions/files (mis-sorted galleries, zip quirks).
CLOCK_MIN_YEAR = 2025     # any "now" before this means NTP hasn't synced yet
CLOCK_WAIT_S = 10         # grace period for NTP to land before giving up


def _clock_synced() -> bool:
    return datetime.now().year >= CLOCK_MIN_YEAR


class CaptureService:
    def __init__(self, base_url_provider: Callable[[], str]) -> None:
        self._busy = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.base_url = base_url_provider
        self.last_finals: list = []      # most recent session outputs (for reprint)
        self.last_shots: list = []
        # Strong refs to in-flight background tasks. The event loop only holds WEAK
        # references to tasks, so without this a capture/print task can be garbage-
        # collected (and cancelled) mid-await. discard-on-done keeps the set bounded.
        self._tasks: set[asyncio.Task] = set()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def busy(self) -> bool:
        return self._busy.locked()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- entry points -----------------------------------------------------
    def trigger_threadsafe(self, source: str = "manual") -> None:
        """Callable from GPIO/gesture background threads."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(lambda: self._spawn(self.run_session(source)))

    async def run_session(self, source: str = "manual") -> None:
        if self._busy.locked():
            return  # already running a session
        async with self._busy:
            try:
                await self._session(source)
            except CaptureError as e:
                bus.publish({"type": "error", "message": str(e)})
                await asyncio.sleep(4)
                bus.publish({"type": "idle"})
            except Exception as e:  # pragma: no cover
                bus.publish({"type": "error", "message": f"unexpected: {e}"})
                await asyncio.sleep(4)
                bus.publish({"type": "idle"})

    # ---- the session ------------------------------------------------------
    async def _session(self, source: str) -> None:
        s = config.load()
        loop = asyncio.get_running_loop()

        if not _clock_synced():
            for _ in range(CLOCK_WAIT_S):     # give NTP a moment — it often lands fast
                await asyncio.sleep(1)
                if _clock_synced():
                    break
            else:
                raise CaptureError("The booth is still starting up (clock sync) — "
                                   "please try again in a minute")

        session_id = "session_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        sess_dir = config.captures_dir(s) / session_id
        sess_dir.mkdir(parents=True, exist_ok=True)

        # countdown
        for n in range(s.timer.countdown_seconds, 0, -1):
            bus.publish({"type": "countdown", "value": n, "source": source})
            await asyncio.sleep(1)

        # capture (blocking -> executor)
        bus.publish({"type": "capturing", "total": s.timer.num_shots})

        def on_shot(i: int) -> None:
            bus.publish({"type": "shot", "index": i, "total": s.timer.num_shots})
            if s.timer.num_shots > 1:
                # brief inter-shot pause is handled by the camera; nudge the UI
                pass

        # Unified daemon: when the Sony camera is also the live-view source, capture
        # via its /capture endpoint (one shared session — live view just pauses briefly).
        if s.camera.backend == "sony" and s.preview.source == "sony_http":
            shots = await loop.run_in_executor(
                None, lambda: daemon_capture(s, sess_dir, s.timer.num_shots, on_shot))
        else:
            cam = make_camera(s)
            shots = await loop.run_in_executor(
                None, lambda: cam.capture(sess_dir, s.timer.num_shots, on_shot))

        # processing
        bus.publish({"type": "processing"})

        def process() -> list[Path]:
            # Per shot: decode ONCE, run the enabled effects in memory, and re-encode
            # ONCE — only if something actually changed (so an un-effected shot keeps
            # its original camera JPEG instead of being recompressed). This replaces a
            # chain that decoded+re-encoded the full-res JPEG at each stage.
            from PIL import Image
            apply_each = s.overlay.enabled and s.overlay.apply_to in ("each", "both")
            for p in shots:
                try:
                    img = Image.open(p).convert("RGB")
                except Exception as e:
                    print(f"[process] cannot read {p}: {e}")
                    continue
                changed = False
                if s.gaze.enabled:                      # measure the ORIGINAL, before overlay
                    processing.measure_gaze(img, s)
                if apply_each:
                    out = processing.apply_overlay_img(img, s)
                    if out is not None:
                        img, changed = out, True
                if s.ai.enabled:
                    out = processing.apply_ai_img(img, s)
                    if out is not None:
                        img, changed = out, True
                if changed:
                    img.save(p, "JPEG", quality=92)
            final_list = shots
            if s.collage.enabled and len(shots) > 1:
                collage = sess_dir / "collage.jpg"
                processing.make_collage(shots, s, collage)
                if s.overlay.enabled and s.overlay.apply_to in ("collage", "both"):
                    processing.apply_overlay(collage, s)
                final_list = [collage]
            return final_list

        finals = await loop.run_in_executor(None, process)
        self.last_finals = list(finals)
        self.last_shots = list(shots)

        # auto-print (best-effort; never blocks or fails a capture)
        if s.printing.enabled and s.printing.auto_print:
            from . import printing
            targets = self._print_targets(finals, shots, s)
            await loop.run_in_executor(None, lambda: printing.print_files(
                targets, s.printing.printer, s.printing.copies, s.printing.media,
                s.printing.fit_to_page))

        # face grouping (best-effort; never blocks or fails a capture)
        if s.faces.enabled:
            try:
                from .faces import make_face_engine
                from .face_index import index as face_index
                eng = make_face_engine(s)
                ok, detail = eng.available()
                if not ok:
                    print(f"[faces] engine unavailable: {detail}")
                elif finals:
                    primary = self._url(finals[0], s)
                    embs = await loop.run_in_executor(
                        None, lambda: [e for shot in shots for e in eng.embed_image(shot)])
                    if embs:
                        face_index.add_faces(session_id, primary, embs, s.faces.match_threshold)
                        print(f"[faces] indexed {len(embs)} face(s) for {session_id}")
            except Exception as e:
                print(f"[faces] indexing failed: {e}")

        # uploads: enqueue to the background sync worker (offline-safe, retrying) so a slow
        # or missing network never blocks the review screen. Legacy inline upload when off.
        if s.storage.background_sync:
            from .sync import worker as sync_worker
            # Google Drive is guest-opt-in now (default off), so it's excluded from the
            # automatic per-session upload — only photos a guest opts in reach Drive.
            sync_worker.enqueue(session_id, finals, s, exclude={"gdrive"})
            queued = [d for d, on in (("ftp", s.storage.ftp.enabled),
                                      ("s3", s.storage.s3.enabled)) if on]
            upload_results = {"queued": queued} if queued else {}
        else:
            upload_results = await loop.run_in_executor(None, lambda: uploaders.upload_all(finals, s))

        # share URL + QR
        base = (s.share.base_url or self.base_url()).rstrip("/")
        share_url = f"{base}/s/{session_id}"
        qr_rel = None
        if s.share.qr_enabled:
            img = qrcode.make(share_url)
            qr_path = sess_dir / "qr.png"
            img.save(qr_path)
            qr_rel = self._url(qr_path, s)

        bus.publish({
            "type": "review",
            "session": session_id,
            "message": s.general.thanks_message,
            "images": [self._url(p, s) for p in finals],
            "all_shots": [self._url(p, s) for p in shots],
            "share_url": share_url,
            "qr": qr_rel,
            "uploads": upload_results,
            "review_seconds": s.timer.review_seconds,
        })

        self._prune(s)
        await asyncio.sleep(s.timer.review_seconds)
        bus.publish({"type": "idle"})

    # ---- printing ---------------------------------------------------------
    def _print_targets(self, finals, shots, s) -> list:
        """Which images to print: the shared output (final), each raw shot, or the collage."""
        if s.printing.which == "each":
            return list(shots)
        return list(finals)   # "final"/"collage": finals is already the collage when enabled

    def print_last_threadsafe(self, source: str = "arduino") -> None:
        """Reprint the last session — called from the Arduino PRINT button thread."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(lambda: self._spawn(self._print_last()))

    async def _print_last(self) -> None:
        s = config.load()
        if not s.printing.enabled or not self.last_finals:
            return
        from . import printing
        loop = asyncio.get_running_loop()
        targets = self._print_targets(self.last_finals, self.last_shots, s)
        res = await loop.run_in_executor(None, lambda: printing.print_files(
            targets, s.printing.printer, s.printing.copies, s.printing.media, s.printing.fit_to_page))
        if not res.get("ok"):
            print(f"[print] reprint failed: {res.get('error')}")

    # ---- helpers ----------------------------------------------------------
    def _url(self, path: Path, s) -> str:
        root = config.captures_dir(s)
        rel = path.relative_to(root)
        return f"/captures/{rel.as_posix()}"

    def _prune(self, s) -> None:
        limit = s.storage.max_local_sessions
        if not limit or limit <= 0:
            return
        root = config.captures_dir(s)
        sessions = sorted([d for d in root.iterdir() if d.is_dir()],
                          key=lambda d: d.stat().st_mtime)
        import shutil
        from . import share
        from .face_index import index as face_index
        for d in sessions[:-limit]:
            shutil.rmtree(d, ignore_errors=True)
            # also drop the session's thumbnails and face-index entries, or guests
            # keep "matching" to photos that no longer exist (404s).
            try:
                share.remove_session_thumbs(d.name)
                face_index.remove_session(d.name)
            except Exception as e:
                print(f"[prune] cleanup for {d.name} failed: {e}")
