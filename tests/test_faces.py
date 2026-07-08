"""Face clustering/index tests (engine-agnostic; no model or camera needed)."""
import numpy as np

from backend.face_index import FaceIndex


def unit(*first):
    v = np.zeros(128, dtype="float32")
    for i, x in enumerate(first):
        v[i] = x
    return (v / np.linalg.norm(v)).tolist()


def fresh():
    idx = FaceIndex()
    idx.clusters, idx.faces, idx._next = [], [], 1   # isolate from any saved state
    return idx


def test_same_person_groups_together():
    idx = fresh()
    idx.add_faces("s1", "/captures/s1/p.jpg", [unit(1, 0, 0)], 0.45)
    idx.add_faces("s2", "/captures/s2/p.jpg", [unit(0.98, 0.05, 0)], 0.45)  # same person
    idx.add_faces("s3", "/captures/s3/p.jpg", [unit(0, 1, 0)], 0.45)        # different
    groups = idx.groups()
    assert len(groups) == 2
    assert groups[0]["count"] == 2                       # biggest group = the 2 matches


def test_match_finds_person():
    idx = fresh()
    idx.add_faces("s1", "/captures/s1/p.jpg", [unit(1, 0, 0)], 0.45)
    m = idx.match(unit(0.97, 0.06, 0), 0.45)
    assert m["matched"] and "/captures/s1/p.jpg" in m["photos"]


def test_no_match_for_stranger():
    idx = fresh()
    idx.add_faces("s1", "/captures/s1/p.jpg", [unit(1, 0, 0)], 0.45)
    m = idx.match(unit(0, 0, 1), 0.45)
    assert m["matched"] is False


def test_multiple_faces_in_one_photo():
    idx = fresh()
    idx.add_faces("s1", "/captures/s1/group.jpg", [unit(1, 0, 0), unit(0, 1, 0)], 0.45)
    assert idx.stats() == {"people": 2, "faces": 2}
