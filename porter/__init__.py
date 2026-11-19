"""Porter — personal AI memory + state layer (PRD v2.0)."""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .embeddings import Embedder, HashEmbedder, build_embedder

try:
    __version__ = (
        (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
    )
except OSError:  # pragma: no cover - defensive fallback
    from importlib.metadata import version

    __version__ = version("porter-memory")

__all__ = ["Embedder", "HashEmbedder", "build_embedder", "__version__"]


def __getattr__(name: str):
    # CLI hooks do not rank or embed locally. Defer NumPy initialization until
    # callers use the public embedding exports.
    if name in {"Embedder", "HashEmbedder", "build_embedder"}:
        from . import embeddings

        return getattr(embeddings, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
