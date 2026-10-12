"""Pluggable LLM interface for the Porter wiki (entity-graph) layer.

The wiki layer organizes approved memories into a lightweight entity graph
("LLM Wiki"): entities/topics extracted from memory text, linked to the
memories that mention them, with co-occurrence edges between entities.

This module defines the extraction protocol. Production wiring (OpenAI,
Ollama, etc.) is documented in the class docstrings; the reference
implementation ships only a deterministic stub so the wiki layer is testable
without a model on the box.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class EntityMention:
    """One entity extracted from a memory's text."""

    name: str                      # normalized entity name, e.g. "tailwind"
    kind: str = "topic"            # topic | person | project | tool | place
    # (relation, target_name) pairs found in the same text, e.g.
    # ("uses", "tailwind"), ("prefers", "dark mode").
    relations: list[tuple[str, str]] = field(default_factory=list)


class BaseLLMExtractor(ABC):
    """Extract entities/topics/relations from memory text."""

    @abstractmethod
    def extract_entities(self, text: str) -> list[EntityMention]:
        """Return entity mentions found in `text`. Must be deterministic
        for a fixed input (or documented otherwise)."""


# ---------------------------------------------------------------------------
# Stub extractor (default): deterministic, dependency-free, no model needed.
# ---------------------------------------------------------------------------

_EN_STOPWORDS = frozenset(
    "the a an and or of to in on for with is are was were be been it its this "
    "that these those i you he she we they my your his her our their me him us "
    "them as at by from about into over after not no yes do does did will would "
    "can could should have has had having there here when where what which who "
    "whom whose how why all any both each few more most other some such than too "
    "very just like prefer prefers liked likes use uses using used remember".split()
)

_ZH_STOPWORDS = frozenset("的了我是在和与或有个这那哪怎么什么谁很都也还就便把被对对于".split())

_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]{2,}")
_CJK_WORD = re.compile(r"[\u4e00-\u9fff]{2,}")


def _classify(name: str) -> str:
    low = name.lower()
    if any(k in low for k in ("project", "repo", "项目")):
        return "project"
    if any(k in low for k in ("vscode", "cursor", "cli", "app", "tool", "工具", "ide")):
        return "tool"
    if re.match(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+$", name):
        return "person"
    return "topic"


class StubLLMExtractor(BaseLLMExtractor):
    """Heuristic extractor: capitalized phrases, CJK n-grams, and
    distinctive latin tokens, minus stopwords. Co-occurrence edges link
    every pair of entities found in the same memory (bounded).

    Good enough to exercise the graph plumbing in tests and demos. It is
    intentionally NOT a substitute for a real LLM in production — see
    OllamaLLMExtractor notes below.
    """

    MAX_ENTITIES = 12

    def extract_entities(self, text: str) -> list[EntityMention]:
        seen: dict[str, EntityMention] = {}
        # Multi-word capitalized phrases first ("San Jose", "Tailwind CSS").
        for m in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", text):
            self._add(seen, m.group(1))
        for m in _CJK_WORD.finditer(text):
            word = m.group(0)
            if word not in _ZH_STOPWORDS:
                self._add(seen, word)
        for m in _LATIN_WORD.finditer(text):
            word = m.group(0)
            if word.lower() not in _EN_STOPWORDS:
                self._add(seen, word)
        mentions = list(seen.values())[: self.MAX_ENTITIES]
        # Co-occurrence relations: every pair in the same memory is linked.
        names = [e.name for e in mentions]
        for e in mentions:
            e.relations = [
                ("co_occurs", other) for other in names if other != e.name
            ][: self.MAX_ENTITIES]
        return mentions

    @staticmethod
    def _add(seen: dict[str, EntityMention], raw: str) -> None:
        name = raw.strip().lower()
        if len(name) >= 2 and name not in seen:
            seen[name] = EntityMention(name=name, kind=_classify(name))


# ---------------------------------------------------------------------------
# Production wiring (documented, not implemented here).
# ---------------------------------------------------------------------------
#
# To use a real LLM, subclass BaseLLMExtractor and prompt the model, e.g.:
#
#   class OllamaLLMExtractor(BaseLLMExtractor):
#       """Local LLM via Ollama (e.g. qwen3:8b). Zero external calls."""
#       def __init__(self, model="qwen3:8b",
#                    base_url="http://localhost:11434"):
#           ...
#       def extract_entities(self, text):
#           # POST {base_url}/api/chat with a JSON-mode prompt:
#           #   "Extract entities... return JSON list of
#           #    {name, kind, relations:[{relation, target}]}"
#           # Parse, normalize names (lowercase/strip), return mentions.
#
#   class OpenAIExtractor(BaseLLMExtractor):
#       """NOTE: sends memory text to a third party (cf. OpenAIEmbedder)."""
#
# Then set PORTER_LLM_EXTRACTOR=openai|ollama and extend build_extractor()
# below. The wiki layer only depends on the BaseLLMExtractor interface.


def build_extractor(kind: str, **kwargs) -> BaseLLMExtractor:
    kinds = {"stub": StubLLMExtractor}
    if kind not in kinds:
        raise ValueError(
            f"unknown LLM extractor kind: {kind} (choose from {sorted(kinds)}; "
            "see porter/llm.py for how to wire ollama/openai)"
        )
    return kinds[kind](**kwargs)
