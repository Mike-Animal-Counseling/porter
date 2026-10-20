"""Porter — personal AI memory + state layer (PRD v2.0)."""

from pathlib import Path

from .embeddings import Embedder, HashEmbedder, build_embedder

try:
    __version__ = (
        (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
    )
except OSError:  # pragma: no cover - defensive fallback
    from importlib.metadata import version

    __version__ = version("porter-memory")

__all__ = ["Embedder", "HashEmbedder", "build_embedder", "__version__"]
