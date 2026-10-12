"""Porter — personal AI memory + state layer (PRD v2.0)."""
from pathlib import Path

try:
    __version__ = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
except OSError:  # pragma: no cover - defensive fallback
    __version__ = "0.1.0"

from .embeddings import Embedder, HashEmbedder, build_embedder

__all__ = ["Embedder", "HashEmbedder", "build_embedder", "__version__"]
