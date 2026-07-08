"""Persistent face index: stores per-photo embeddings and clusters them into people
with simple online cosine clustering (good for event-scale collections). Engine-agnostic
— it just consumes L2-normalized embeddings, so it's fully unit-testable without a model."""
from __future__ import annotations

import json
import threading

from . import config


def _np():
    import numpy as np
    return np


class FaceIndex:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.path = config.data_dir() / "faces.json"
        self.clusters: list[dict] = []     # {id, centroid:[...], count}
        self.faces: list[dict] = []        # {session, url, cluster}
        self._next = 1
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text())
                self.clusters = d.get("clusters", [])
                self.faces = d.get("faces", [])
                self._next = d.get("next", len(self.clusters) + 1)
            except Exception:
                pass

    def _save(self) -> None:
        self.path.write_text(json.dumps(
            {"clusters": self.clusters, "faces": self.faces, "next": self._next}))

    def _assign(self, emb, threshold: float) -> int:
        np = _np()
        v = np.asarray(emb, dtype="float32")
        best_id, best_sim = None, -1.0
        for c in self.clusters:
            sim = float(np.dot(v, np.asarray(c["centroid"], dtype="float32")))
            if sim > best_sim:
                best_sim, best_id = sim, c["id"]
        if best_id is not None and best_sim >= threshold:
            c = next(x for x in self.clusters if x["id"] == best_id)
            n = c["count"]
            cen = (np.asarray(c["centroid"], dtype="float32") * n + v) / (n + 1)
            cen = cen / (float(np.linalg.norm(cen)) or 1.0)
            c["centroid"], c["count"] = cen.tolist(), n + 1
            return best_id
        cid = self._next
        self._next += 1
        self.clusters.append({"id": cid, "centroid": list(map(float, v)), "count": 1})
        return cid

    def add_faces(self, session: str, url: str, embeddings: list, threshold: float) -> None:
        with self._lock:
            for emb in embeddings:
                cid = self._assign(emb, threshold)
                self.faces.append({"session": session, "url": url, "cluster": cid})
            self._save()

    def groups(self) -> list[dict]:
        with self._lock:
            by: dict[int, list[str]] = {}
            for f in self.faces:
                urls = by.setdefault(f["cluster"], [])
                if f["url"] not in urls:
                    urls.append(f["url"])
            out = [{"person": cid, "count": len(urls), "photos": urls} for cid, urls in by.items()]
            out.sort(key=lambda g: g["count"], reverse=True)
            return out

    def match(self, emb, threshold: float) -> dict:
        with self._lock:
            np = _np()
            v = np.asarray(emb, dtype="float32")
            best, sim = None, -1.0
            for c in self.clusters:
                s = float(np.dot(v, np.asarray(c["centroid"], dtype="float32")))
                if s > sim:
                    sim, best = s, c["id"]
            if best is None or sim < threshold:
                return {"matched": False, "similarity": round(sim, 3), "photos": []}
            urls: list[str] = []
            for f in self.faces:
                if f["cluster"] == best and f["url"] not in urls:
                    urls.append(f["url"])
            return {"matched": True, "person": best, "similarity": round(sim, 3), "photos": urls}

    def stats(self) -> dict:
        with self._lock:
            return {"people": len(self.clusters), "faces": len(self.faces)}


index = FaceIndex()
