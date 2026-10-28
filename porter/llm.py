"""Pluggable LLM interface for the Porter wiki (entity-graph) layer.

The wiki layer organizes approved memories into a lightweight entity graph
("LLM Wiki"): entities/topics extracted from memory text, linked to the
memories that mention them, with co-occurrence edges between entities.

This module defines the extraction protocol and deterministic default stub.
An opt-in Ollama backend lives in llm_ollama.py; see docs/EXTRACTION.md.
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class EntityMention:
    """One entity extracted from a memory's text."""

    name: str  # normalized entity name, e.g. "tailwind"
    kind: str = "topic"  # topic | person | project | tool | place
    # (relation, target_name) pairs found in the same text, e.g.
    # ("uses", "tailwind"), ("prefers", "dark mode").
    relations: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class ExtractedFact:
    """One durable fact pulled from a conversation (auto-capture)."""

    content: str  # self-contained statement
    type: str = "fact"  # preference | fact | episode | skill_ref
    scope_tags: list[str] = field(default_factory=list)
    confidence: float = 0.7  # extracted, not asserted — below 1.0


class BaseLLMExtractor(ABC):
    """Extract entities/topics/relations from memory text."""

    @abstractmethod
    def extract_entities(self, text: str) -> list[EntityMention]:
        """Return entity mentions found in `text`. Must be deterministic
        for a fixed input (or documented otherwise)."""

    def extract_facts(self, text: str) -> list[ExtractedFact]:
        """Pull durable facts (preferences, decisions, project context)
        from a conversation. Default: not implemented — extractors opt in."""
        raise NotImplementedError

    async def aextract_entities(self, text: str) -> list[EntityMention]:
        return await asyncio.to_thread(self.extract_entities, text)

    async def aextract_facts(self, text: str) -> list[ExtractedFact]:
        return await asyncio.to_thread(self.extract_facts, text)


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
    intentionally NOT a substitute for a real LLM in production — see docs/EXTRACTION.md.
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
            e.relations = [("co_occurs", other) for other in names if other != e.name][
                : self.MAX_ENTITIES
            ]
        return mentions

    @staticmethod
    def _add(seen: dict[str, EntityMention], raw: str) -> None:
        name = raw.strip().lower()
        if len(name) >= 2 and name not in seen:
            seen[name] = EntityMention(name=name, kind=_classify(name))

    # ------------------------------------------------------- fact extraction
    # Durable-fact patterns (EN + ZH): preferences, decisions, stable context.
    # Heuristic on purpose — every extracted fact still goes through triage
    # (sensitivity / conflict / confidence gates) before it can go active,
    # and extracted content is always marked untrusted downstream.
    _FACT_PATTERNS = [
        (
            re.compile(
                r"\b(prefer|prefers|preferred|like|likes|love|loves|hate|hates|dislike|dislikes)\b",
                re.I,
            ),
            "preference",
            [],
        ),
        (re.compile(r"喜欢|偏好|最爱|讨厌|习惯"), "preference", []),
        (re.compile(r"\b(decided|decision|chose|chosen|will use|migrated to)\b", re.I), "fact", []),
        (re.compile(r"决定|选择|采用"), "fact", []),
        (re.compile(r"\ballergic to\b|allergy", re.I), "fact", ["health"]),
        (re.compile(r"过敏"), "fact", ["health"]),
        (re.compile(r"\b(works? on|working on|my project|my stack)\b", re.I), "fact", ["work"]),
        (re.compile(r"项目|技术栈"), "fact", ["work"]),
        (re.compile(r"\bsalary\b|wages?|\bincome\b|bank account", re.I), "fact", ["finance"]),
        (re.compile(r"工资|薪水|收入|银行账户"), "fact", ["finance"]),
    ]
    _SENT_SPLIT = re.compile(r"[.!?。！？]+\s*|\n+")
    # Min length 4: CJK sentences are dense ("我喜欢茶" is 4 chars and a
    # complete durable fact); the patterns above still gate quality.
    _MIN_FACT_LEN = 4

    def extract_facts(self, text: str) -> list[ExtractedFact]:
        # T3: the stub is regex-based and never *follows* instructions in the
        # text. A real LLM extractor MUST use an instruction-hierarchy prompt
        # (system > developer > extraction-task; conversation text is DATA)
        # so injected "remember: ..." lines cannot steer it. Either way, every
        # extracted fact passes triage and is marked untrusted downstream.
        facts: list[ExtractedFact] = []
        for sent in self._SENT_SPLIT.split(text):
            sent = sent.strip()
            if not (self._MIN_FACT_LEN <= len(sent) <= 500):
                continue
            for rx, ftype, tags in self._FACT_PATTERNS:
                if rx.search(sent):
                    facts.append(
                        ExtractedFact(
                            content=sent,
                            type=ftype,
                            scope_tags=list(tags),
                            confidence=0.7,
                        )
                    )
                    break
            if len(facts) >= 10:
                break
        return facts


def build_extractor(kind: str, **kwargs) -> BaseLLMExtractor:
    from .llm_ollama import OllamaLLMExtractor

    kinds = {"stub": StubLLMExtractor, "ollama": OllamaLLMExtractor}
    if kind not in kinds:
        raise ValueError(
            f"unknown LLM extractor kind: {kind} (choose from {sorted(kinds)}; "
            "OpenAI extraction is not implemented)"
        )
    return kinds[kind](**kwargs)
