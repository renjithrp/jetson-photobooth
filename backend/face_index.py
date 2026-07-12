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
        self._cmat = None                  # cached centroid matrix (rebuilt when stale)
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
        # atomic write — a power cut mid-save must never corrupt the whole index
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(
            {"clusters": self.clusters, "faces": self.faces, "next": self._next}))
        tmp.replace(self.path)

    def _matrix(self):
        """(N, D) float32 matrix of cluster centroids, cached between mutations."""
        np = _np()
        if self._cmat is None or len(self._cmat) != len(self.clusters):
            self._cmat = (np.asarray([c["centroid"] for c in self.clusters], dtype="float32")
                          if self.clusters else np.zeros((0, 1), dtype="float32"))
        return self._cmat

    def _best(self, emb) -> tuple[int | None, float]:
        """Highest-similarity cluster for an embedding (vectorized over all centroids)."""
        np = _np()
        m = self._matrix()
        if not len(m):
            return None, -1.0
        sims = m @ np.asarray(emb, dtype="float32")
        i = int(np.argmax(sims))
        return self.clusters[i]["id"], float(sims[i])

    def _assign(self, emb, threshold: float) -> int:
        np = _np()
        v = np.asarray(emb, dtype="float32")
        best_id, best_sim = self._best(v)
        if best_id is not None and best_sim >= threshold:
            c = next(x for x in self.clusters if x["id"] == best_id)
            n = c["count"]
            cen = (np.asarray(c["centroid"], dtype="float32") * n + v) / (n + 1)
            cen = cen / (float(np.linalg.norm(cen)) or 1.0)
            c["centroid"], c["count"] = cen.tolist(), n + 1
            self._cmat = None
            return best_id
        cid = self._next
        self._next += 1
        self.clusters.append({"id": cid, "centroid": list(map(float, v)), "count": 1})
        self._cmat = None
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
            best, sim = self._best(emb)
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
