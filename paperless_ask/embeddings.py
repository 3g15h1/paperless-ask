"""Search by meaning: passage embeddings from an Ollama embedding model, saved in DATA_DIR.

Each document's text is cut into passages, embedded once, and stored with the document's
`modified` time and the model name, so changed documents are redone and a model change rebuilds
the index. A document's score for a question is its best passage's cosine similarity.
"""
import io
import json
import logging
import os
import tempfile
from pathlib import Path

import numpy as np

log = logging.getLogger("paperless-ask")


def passages(content: str, size: int = 600, max_chunks: int = 40) -> list[str]:
    """~size-character passages on whitespace boundaries, overlapping a little."""
    flat = " ".join(content.split())
    out, start = [], 0
    while start < len(flat) and len(out) < max_chunks:
        end = min(len(flat), start + size)
        if end < len(flat):
            cut = flat.rfind(" ", start + size // 2, end)
            end = cut if cut > 0 else end
        out.append(flat[start:end])
        start = max(end - 80, start + 1) if end < len(flat) else end
    return out


class ModelMissing(RuntimeError):
    pass


async def embed(client, ollama_url: str, model: str, texts: list[str], timeout: float = 60) -> np.ndarray:
    """Unit-length float32 vectors, one row per text."""
    r = await client.post(f"{ollama_url}/api/embed", json={"model": model, "input": texts, "keep_alive": "24h"},
                          timeout=timeout)
    if r.status_code == 404 or (r.status_code >= 400 and "not found" in r.text.lower()):
        raise ModelMissing(f"embedding model {model!r} isn't installed in Ollama (run: ollama pull {model})")
    r.raise_for_status()
    vecs = np.asarray(r.json()["embeddings"], dtype=np.float32)
    if vecs.ndim != 2 or len(vecs) != len(texts):
        raise RuntimeError(f"unexpected embedding response shape {vecs.shape}")
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    return vecs / np.where(norms == 0, 1, norms)


class EmbeddingIndex:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.docs: dict[int, dict] = {}  # id -> {"modified", "model", "vecs": ndarray}
        try:
            with np.load(self.path, allow_pickle=False) as z:
                meta = json.loads(str(z["meta"]))
                for k, m in meta.items():
                    self.docs[int(k)] = {**m, "vecs": z[f"d{k}"]}
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("embedding index %s unreadable (%s); rebuilding", self.path, e)
            self.docs = {}

    def stale(self, doc_id: int, modified: str, model: str) -> bool:
        d = self.docs.get(doc_id)
        return not d or d["modified"] != modified or d["model"] != model

    def put(self, doc_id: int, vecs: np.ndarray, modified: str, model: str, save: bool = True) -> None:
        self.docs[doc_id] = {"modified": modified, "model": model, "vecs": vecs.astype(np.float32)}
        if save:
            self.save()

    def prune(self, keep_ids) -> None:
        gone = set(self.docs) - set(keep_ids)
        for i in gone:
            del self.docs[i]
        if gone:
            self.save()

    def scores(self, qvec: np.ndarray, model: str, current: dict[int, str]) -> dict[int, float]:
        """Best-passage cosine similarity per document, for documents whose vectors are current."""
        out = {}
        for i, d in self.docs.items():
            if d["model"] == model and current.get(i) == d["modified"] and len(d["vecs"]):
                out[i] = float(np.max(d["vecs"] @ qvec))
        return out

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        meta = {str(i): {"modified": d["modified"], "model": d["model"]} for i, d in self.docs.items()}
        buf = io.BytesIO()
        np.savez(buf, meta=np.array(json.dumps(meta)), **{f"d{i}": d["vecs"] for i, d in self.docs.items()})
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".emb-", suffix=".npz")
        with os.fdopen(fd, "wb") as f:
            f.write(buf.getvalue())
        os.replace(tmp, self.path)
