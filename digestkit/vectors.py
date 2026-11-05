"""Vectors for items and stories.

Default: hashed word-stem vectors (no model, no key, deterministic). Optional: any OpenAI-compatible
/embeddings endpoint (EMBED_BASE_URL, EMBED_API_KEY, EMBED_MODEL). Embeddings are cached in SQLite and
make "is this the same story / the same thread" much better on long horizons (weekly and monthly reviews).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass

import httpx
import numpy as np

from digestkit import textsim
from digestkit.store import Store, jload

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Thresholds:
    cluster: float  # two new items are the same story
    related: float  # a new item may continue an already known story (shown to the agent as a hint)
    thread: float  # two reported stories belong to one thread (for reviews)


HASHED = Thresholds(cluster=0.55, related=0.30, thread=0.45)
EMBED = Thresholds(cluster=0.80, related=0.62, thread=0.70)


def item_text(r: sqlite3.Row) -> tuple[str, str]:
    return r["title"], r["text"]


def story_text(r: sqlite3.Row) -> tuple[str, str]:
    facts = " ".join(jload(r["facts"], []))
    return r["title"], f"{r['summary']} {facts}"


class Vectorizer:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.base = os.environ.get("EMBED_BASE_URL") or ""
        self.model = os.environ.get("EMBED_MODEL") or ""
        self.key = os.environ.get("EMBED_API_KEY") or os.environ.get("LLM_API_KEY") or ""
        self.remote = bool(self.base and self.model)
        self.th = EMBED if self.remote else HASHED
        self.name = f"embed:{self.model}" if self.remote else "hashed"

    def _embed(self, texts: list[str]) -> np.ndarray:
        out = []
        with httpx.Client(timeout=60) as c:
            for i in range(0, len(texts), 32):
                r = c.post(self.base.rstrip("/") + "/embeddings",
                           headers={"Authorization": f"Bearer {self.key}"} if self.key else {},
                           json={"model": self.model, "input": texts[i:i + 32]})
                r.raise_for_status()
                out += [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]
        m = np.array(out, dtype=np.float32)
        return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)

    def matrix(self, kind: str, rows: list[sqlite3.Row]) -> np.ndarray:
        """kind: 'item' | 'story'. Returns (n, d) unit vectors."""
        if not rows:
            return np.zeros((0, textsim.DIM), dtype=np.float32)
        textf = item_text if kind == "item" else story_text
        if self.remote:
            try:
                ids = [int(r["id"]) for r in rows]
                have = self.store.get_vecs(kind, ids, self.name)
                todo = [r for r in rows if int(r["id"]) not in have]
                if todo:
                    vecs = self._embed([f"{t}. {b[:800]}" for t, b in (textf(r) for r in todo)])
                    for r, v in zip(todo, vecs, strict=True):
                        self.store.put_vec(kind, int(r["id"]), self.name, v)
                        have[int(r["id"])] = v
                return np.stack([have[i] for i in ids])
            except Exception as e:
                log.warning("embeddings unavailable (%s), falling back to hashed vectors", e)
                self.remote, self.th, self.name = False, HASHED, "hashed"
        return np.stack([textsim.hashed_vec(*textf(r)) for r in rows])

    def pair(self, kind_a: str, rows_a: list[sqlite3.Row], kind_b: str, rows_b: list[sqlite3.Row]) -> tuple[np.ndarray, np.ndarray]:
        """Two matrices guaranteed to be in the same space (embeddings may fail between the two calls)."""
        name = self.name
        a = self.matrix(kind_a, rows_a)
        b = self.matrix(kind_b, rows_b)
        if self.name != name:  # fell back to hashed while computing b
            a = self.matrix(kind_a, rows_a)
        return a, b

    def forget_story(self, story_id: int) -> None:
        self.store.x("DELETE FROM vectors WHERE kind='story' AND ref=?", (story_id,))
