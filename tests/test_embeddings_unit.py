"""Embedding index and passages (no network)."""
import numpy as np

from paperless_ask.embeddings import EmbeddingIndex, passages


def test_passages_cover_the_text_with_overlap_and_a_cap():
    text = " ".join(f"word{i}" for i in range(400))
    ps = passages(text, size=200, max_chunks=40)
    assert ps[0].startswith("word0") and ps[-1].endswith("word399")
    assert all(len(p) <= 200 for p in ps)
    assert len(passages(text, size=200, max_chunks=3)) == 3
    assert passages("   ") == []


def test_index_round_trip_staleness_and_scores(tmp_path):
    idx = EmbeddingIndex(tmp_path / "e.npz")
    a = np.array([[1, 0], [0.6, 0.8]], dtype=np.float32)
    idx.put(1, a, "m1", "mini")
    idx.put(2, np.array([[0, 1]], dtype=np.float32), "m1", "mini")
    idx2 = EmbeddingIndex(tmp_path / "e.npz")
    assert not idx2.stale(1, "m1", "mini") and idx2.stale(1, "m2", "mini") and idx2.stale(1, "m1", "nomic")
    s = idx2.scores(np.array([1, 0], dtype=np.float32), "mini", {1: "m1", 2: "m1"})
    assert s == {1: 1.0, 2: 0.0}
    assert idx2.scores(np.array([1, 0], dtype=np.float32), "mini", {1: "changed"}) == {}
    idx2.prune([2])
    assert list(EmbeddingIndex(tmp_path / "e.npz").docs) == [2]


def test_damaged_index_is_rebuilt(tmp_path):
    (tmp_path / "e.npz").write_bytes(b"junk")
    assert EmbeddingIndex(tmp_path / "e.npz").docs == {}
