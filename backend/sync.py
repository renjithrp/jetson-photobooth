"""Background upload worker: a durable queue with retry/backoff for Google Drive
(rclone) and FTP/FTPS.

Finished sessions are *enqueued*; a background thread uploads them, retrying with
exponential backoff when a destination or the internet is unreachable. The queue
persists to `data/sync_queue.json`, so uploads resume after a reboot or an offline
stretch — a capture is never blocked on a slow or missing network. This is the key
"works offline and online" guarantee: shoot now, sync whenever the link is back.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path

from . import config, uploaders

MAX_KEEP_DONE = 200        # trim completed jobs beyond this
BACKOFF_CAP_S = 600        # cap retry backoff at 10 minutes


class SyncWorker:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._run = False
        self._loaded = False
        self.path: Path | None = None
        self.jobs: list[dict] = []

    # ---- persistence ------------------------------------------------------
    def _load(self) -> None:
        self.path = config.data_dir() / "sync_queue.json"
        if self.path.exists():
            try:
                self.jobs = json.loads(self.path.read_text()).get("jobs", [])
            except Exception:
                self.jobs = []
        self._loaded = True

    def _save(self) -> None:
        if not self.path:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"jobs": self.jobs}))
        tmp.replace(self.path)

    # ---- API --------------------------------------------------------------
    def enqueue(self, session: str, files: list, settings) -> None:
        """Queue a finished session's files for whichever destinations are enabled."""
        dests = uploaders.enabled_dests(settings)
        if not dests:
            return
        with self._lock:
            if not self._loaded:
                self._load()
            self.jobs.append({
                "id": f"{session}.{int(time.time() * 1000)}",
                "session": session,
                "files": [str(f) for f in files],
                "dests": dests,
                "done": [],
                "attempts": 0,
                "next": 0.0,
                "last_error": "",
                "created": time.time(),
                "status": "pending",
            })
            self._save()

    def retry_now(self) -> None:
        """Clear backoff so all pending jobs are retried immediately (admin button)."""
        with self._lock:
            if not self._loaded:
                self._load()
            for j in self.jobs:
                if j["status"] == "pending":
                    j["next"] = 0.0
            self._save()

    def status(self) -> dict:
        with self._lock:
            if not self._loaded:
                self._load()
            pending = [j for j in self.jobs if j["status"] == "pending"]
            done = [j for j in self.jobs if j["status"] == "done"]
            failing = [{"session": j["session"], "attempts": j["attempts"],
                        "error": j["last_error"], "dests_left": [d for d in j["dests"] if d not in j["done"]]}
                       for j in pending if j["attempts"] > 0]
            return {
                "enabled": True,
                "pending": len(pending),
                "completed": len(done),
                "failing": failing[:10],
            }

    # ---- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self._run = True
        if not self._thread or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._run = False

    def _loop(self) -> None:
        with self._lock:
            if not self._loaded:
                self._load()
        while self._run:
            try:
                self._tick()
            except Exception as e:  # never let the worker die
                print(f"[sync] tick error: {e}")
            time.sleep(5)

    def _tick(self) -> None:
        s = config.load()
        now = time.time()
        # Snapshot the due jobs as deep COPIES under the lock, then run the slow
        # uploads (which we must NOT hold the lock across) mutating only the copies.
        # Previously the live job dicts were mutated lock-free while enqueue()/status()
        # ran json.dumps(self.jobs) under the lock — adding the "completed_at" key
        # mid-serialisation raised "dictionary changed size during iteration", which
        # surfaced to the guest as an ERROR screen right after a successful capture.
        with self._lock:
            pending = [copy.deepcopy(j) for j in self.jobs
                       if j["status"] == "pending" and j["next"] <= now]
        if not pending:
            return
        changed = False
        for j in pending:
            for dest in j["dests"]:
                if dest in j["done"]:
                    continue
                files = [Path(f) for f in j["files"] if Path(f).exists()]
                if not files:                       # source gone -> nothing to send
                    j["done"].append(dest)
                    changed = True
                    continue
                if dest not in uploaders.enabled_dests(s):   # turned off since queueing
                    j["done"].append(dest)
                    changed = True
                    continue
                res = uploaders.dispatch(dest, files, s, subdir=j["session"])
                if res.get("ok"):
                    j["done"].append(dest)
                    j["last_error"] = ""
                else:
                    j["last_error"] = f"{dest}: {res.get('error', 'failed')}"
                changed = True
            if all(d in j["done"] for d in j["dests"]):
                j["status"] = "done"
                j["completed_at"] = now
            else:
                j["attempts"] += 1
                j["next"] = now + min(BACKOFF_CAP_S, 10 * (2 ** min(j["attempts"], 6)))
        if not changed:
            return
        with self._lock:
            # Merge the upload results back onto the live job dicts (matched by id),
            # so any job enqueued while we were uploading is left untouched.
            by_id = {j["id"]: j for j in self.jobs}
            for pj in pending:
                live = by_id.get(pj["id"])
                if live is not None:
                    live.update(pj)
            # trim old completed jobs so the queue file stays small
            done = [j for j in self.jobs if j["status"] == "done"]
            if len(done) > MAX_KEEP_DONE:
                keep = set(id(j) for j in sorted(done, key=lambda x: x.get("completed_at", 0))[-MAX_KEEP_DONE:])
                self.jobs = [j for j in self.jobs if j["status"] != "done" or id(j) in keep]
            self._save()


worker = SyncWorker()
