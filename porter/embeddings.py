"""Embedding providers behind a stable interface.

PRD §13.1: v1 uses local sentence-transformers + BGE-M3 (1024 dim), zero
external calls. The interface lets us swap in OpenAI (or any provider) later
without touching the repository or API layers.
"""
from __future__ import annotations

import hashlib
import math
from abc import ABC, abstractmethod
from typing import Sequence

import numpy as np

EMBEDDING_DIM = 1024


class Embedder(ABC):
    """Produces L2-normalized embedding vectors."""

    dim: int = EMBEDDING_DIM

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns one vector per input text."""

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    @staticmethod
    def cosine(a: Sequence[float], b: Sequence[float]) -> float:
        va = np.asarray(a, dtype=np.float64)
        vb = np.asarray(b, dtype=np.float64)
        denom = np.linalg.norm(va) * np.linalg.norm(vb)
        if denom == 0:
            return 0.0
        return float(np.dot(va, vb) / denom)


class SentenceTransformerEmbedder(Embedder):
    """Production embedder: local BGE-M3 via sentence-transformers.

    Model is downloaded once from HuggingFace on first use (~2.3 GB).
    Lazy import keeps the API importable without torch installed.
    """

    MODEL_ID = "BAAI/bge-m3"

    def __init__(self, model_id: str = MODEL_ID) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_id)
        self.dim = self._model.get_sentence_embedding_dimension() or EMBEDDING_DIM

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vecs = self._model.encode(list(texts), normalize_embeddings=True)
        return [list(map(float, v)) for v in vecs]


class HashEmbedder(Embedder):
    """Deterministic dev/test stub.

    Hashes token n-grams into a fixed-size vector. NOT semantically meaningful
    beyond coarse token overlap -- good enough for wiring, tests, and demos in
    environments where downloading BGE-M3 is impractical. Never use in prod.
    """

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    def _token_vec(self, token: str) -> np.ndarray:
        v = np.zeros(self.dim)
        for n in (1, 2):
            for i in range(len(token) - n + 1):
                gram = token[i : i + n]
                h = int(hashlib.sha256(gram.encode()).hexdigest(), 16)
                v[h % self.dim] += 1.0 / n
        return v

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = np.zeros(self.dim)
            for tok in text.lower().split():
                vec += self._token_vec(tok)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec /= norm
            out.append([float(x) for x in vec])
        return out


class HttpEmbedder(Embedder):
    """Client for the standalone embedder service (docker-compose `embedder`).

    Lets the API container stay light while the model lives in its own
    container with its own resource limits.
    """

    def __init__(self, base_url: str, dim: int = EMBEDDING_DIM, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.dim = dim
        self.timeout = timeout

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        import httpx

        resp = httpx.post(
            f"{self.base_url}/embed",
            json={"texts": list(texts)},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["vectors"]


class OpenAIEmbedder(Embedder):
    """Drop-in replacement hitting OpenAI's embedding API (opt-in, §15).

    NOTE: enabling this sends memory text to a third party. The dashboard
    must surface that choice prominently (PRD §15).
    """

    MODEL_ID = "text-embedding-3-small"

    def __init__(self, api_key: str, model_id: str = MODEL_ID, dim: int = EMBEDDING_DIM) -> None:
        self.api_key = api_key
        self.model_id = model_id
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        import httpx

        resp = httpx.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model_id, "input": list(texts)},
            timeout=60.0,
        )
        resp.raise_for_status()
        items = resp.json()["data"]
        # API returns 1536-dim for text-embedding-3-small; truncate/pad to our dim
        out = []
        for item in sorted(items, key=lambda d: d["index"]):
            v = list(map(float, item["embedding"]))[: self.dim]
            v += [0.0] * (self.dim - len(v))
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out


def build_embedder(kind: str, **kwargs) -> Embedder:
    kinds = {
        "hash": HashEmbedder,
        "sentence-transformer": SentenceTransformerEmbedder,
        "http": HttpEmbedder,
        "openai": OpenAIEmbedder,
    }
    if kind not in kinds:
        raise ValueError(f"unknown embedder kind: {kind} (choose from {sorted(kinds)})")
    return kinds[kind](**kwargs)
