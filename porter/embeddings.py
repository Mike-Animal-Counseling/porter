"""Embedding providers behind a stable interface.

PRD §13.1: v1 uses local sentence-transformers + BGE-M3 (1024 dim), zero
external calls. The interface lets us swap in OpenAI (or any provider) later
without touching the repository or API layers.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from abc import ABC, abstractmethod
from collections.abc import Sequence

import numpy as np

from .errors import PorterError

EMBEDDING_DIM = 1024


class EmbeddingUnavailable(PorterError):
    """The configured embedding dependency could not serve the request."""

    status_code = 503
    code = "dependency_unavailable"


class EmbeddingTimeout(EmbeddingUnavailable):
    status_code = 504
    code = "dependency_timeout"


def _embedding_request(url: str, **kwargs) -> dict:
    """Translate upstream HTTP failures without exposing text or credentials."""
    import httpx

    try:
        response = httpx.post(url, **kwargs)
        response.raise_for_status()
    except httpx.TimeoutException:
        raise EmbeddingTimeout(
            "Embedding request timed out. The service may still be warming up; "
            "check the configured embedding service and retry."
        ) from None
    except httpx.HTTPError:
        raise EmbeddingUnavailable(
            "Embedding service is unavailable. Check the configured service and retry."
        ) from None
    try:
        return response.json()
    except ValueError:
        raise EmbeddingUnavailable(
            "Embedding service returned an invalid response. Check the configured service and retry."
        ) from None


class Embedder(ABC):
    """Produces L2-normalized embedding vectors."""

    dim: int = EMBEDDING_DIM

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns one vector per input text."""

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    async def aembed_one(self, text: str) -> list[float]:
        """Keep model inference and HTTP requests off the API event loop."""
        return await asyncio.to_thread(self.embed_one, text)

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


class Qwen3Embedder(Embedder):
    """Challenger embedder: Qwen3-Embedding-0.6B via sentence-transformers.

    Why it is the challenger (researched 2026-10-12):
    - MTEB Multilingual 64.33 overall / 64.64 retrieval, C-MTEB 66.33 —
      beats BGE-M3 and multilingual-e5-large-instruct at the same 0.6B size.
    - 1024 dim == EMBEDDING_DIM, so the existing pgvector vector(1024)
      schema needs NO migration to A/B it against BGE-M3.
    - 0.6B params (~1.2 GB bf16): lighter than BGE-M3 (~2.3 GB), runs on
      CPU-only hosts. The 4B/8B siblings score higher but need a GPU.
    - 32k context, 100+ languages incl. strong Chinese.

    Model downloads once from HuggingFace on first use. Lazy import keeps
    the API importable without torch installed. Select with
    PORTER_EMBEDDER=qwen3 (default embedder is unchanged: hash for dev,
    sentence-transformer/BGE-M3 for the docker prod profile).
    """

    MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"

    def __init__(self, model_id: str = MODEL_ID) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_id, trust_remote_code=True)
        self.dim = self._model.get_sentence_embedding_dimension() or EMBEDDING_DIM

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vecs = self._model.encode(list(texts), normalize_embeddings=True)
        return [list(map(float, v)) for v in vecs]


class HarrierEmbedder(Embedder):
    """Challenger embedder: Microsoft harrier-oss-v1-0.6B.

    Why it is a challenger (researched 2026-10-13):
    - #1 on MTEB Multilingual v2 (family); the 0.6B ranks 10th at 0.6901,
      ten places above Qwen3-Embedding-0.6B at the same sub-billion size.
    - 1024 dim == EMBEDDING_DIM, so the existing pgvector vector(1024)
      schema needs NO migration to A/B it against BGE-M3/Qwen3.
    - MIT license, 32k context, sentence-transformers compatible.

    Model downloads once from HuggingFace on first use. Lazy import keeps
    the API importable without torch installed. Select with
    PORTER_EMBEDDER=harrier.
    """

    MODEL_ID = "microsoft/harrier-oss-v1-0.6b"

    def __init__(self, model_id: str = MODEL_ID) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_id, trust_remote_code=True)
        self.dim = self._model.get_sentence_embedding_dimension() or EMBEDDING_DIM

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vecs = self._model.encode(list(texts), normalize_embeddings=True)
        return [list(map(float, v)) for v in vecs]


class EmbeddingGemmaEmbedder(Embedder):
    """Challenger embedder: Google embeddinggemma-300m (308M params).

    Compact open-weights multilingual embedder. NOTE the license: Gemma
    Terms of Use (not MIT/Apache) — check it fits your deployment before
    choosing this over harrier/qwen3. 768 dim != EMBEDDING_DIM, so
    promoting it to default would need a pgvector migration; fine for
    eval comparison as-is.

    Model downloads once from HuggingFace on first use. Lazy import keeps
    the API importable without torch installed. Select with
    PORTER_EMBEDDER=embeddinggemma.
    """

    MODEL_ID = "google/embeddinggemma-300m"

    def __init__(self, model_id: str = MODEL_ID) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_id, trust_remote_code=True)
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
        data = _embedding_request(
            f"{self.base_url}/embed",
            json={"texts": list(texts)},
            timeout=self.timeout,
        )
        self.model_id = data.get("model", "unknown")
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
        data = _embedding_request(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model_id, "input": list(texts)},
            timeout=60.0,
        )
        items = data["data"]
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
        # BGE-M3 (v1 default for prod): both names work.
        "sentence-transformer": SentenceTransformerEmbedder,
        "bge-m3": SentenceTransformerEmbedder,
        # Challenger (2026-10): Qwen3-Embedding-0.6B. Same 1024 dim as
        # BGE-M3, so no pgvector migration is needed to compare them.
        "qwen3": Qwen3Embedder,
        # Challenger (2026-10): Microsoft harrier-oss-v1-0.6B (MIT).
        # Same 1024 dim — drop-in A/B against BGE-M3/Qwen3.
        "harrier": HarrierEmbedder,
        # Challenger (2026-10): Google embeddinggemma-300m. 768 dim and
        # Gemma license — eval only until a migration + license review.
        "embeddinggemma": EmbeddingGemmaEmbedder,
        "http": HttpEmbedder,
        "openai": OpenAIEmbedder,
    }
    if kind not in kinds:
        raise ValueError(f"unknown embedder kind: {kind} (choose from {sorted(kinds)})")
    return kinds[kind](**kwargs)
