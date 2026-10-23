"""Standalone embedder service (docker-compose `embedder`).

Exposes POST /embed {texts: [...]} -> {vectors: [...], dim: N, model: ...}.
Runs BGE-M3 in its own container so the API stays light.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from pydantic import BaseModel

from .embeddings import build_embedder


class EmbedRequest(BaseModel):
    texts: list[str]


class EmbedResponse(BaseModel):
    vectors: list[list[float]]
    dim: int
    model: str


def create_embed_server(kind: str = "sentence-transformer", **kwargs) -> FastAPI:
    app = FastAPI(title="porter-embedder", version="0.1.0")
    embedder = build_embedder(kind, **kwargs)

    @app.post("/embed", response_model=EmbedResponse)
    def embed(body: EmbedRequest):
        vectors = embedder.embed(body.texts)
        model = getattr(embedder, "MODEL_ID", kind)
        return EmbedResponse(vectors=vectors, dim=embedder.dim, model=model)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "dim": embedder.dim}

    return app


app = create_embed_server(os.getenv("PORTER_EMBEDDER", os.getenv("EMBEDDER_KIND", "bge-m3")))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("porter.embed_server:app", host="0.0.0.0", port=8001)
