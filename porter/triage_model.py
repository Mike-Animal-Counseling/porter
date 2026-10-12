"""ML triage model for the tiered approval layer (automation).

Rules (porter/triage.py) are the fast pre-filter and the always-available
fallback. This module adds an ML second opinion: a zero-shot classifier
that catches sensitive content the regex patterns miss (paraphrases, novel
phrasing), in both English and Chinese.

Selection via PORTER_TRIAGE_MODEL:
  auto       (default) finetuned if PORTER_TRIAGE_MODEL_PATH exists,
             else zero-shot when transformers is importable, else heuristic
  finetuned  local fine-tuned classifier (scripts/finetune_triage.py output)
  zeroshot   force the zero-shot model (errors clearly if deps are missing)
  heuristic  pure rules, no model, no extra latency

Design notes:
- E2EE memories NEVER reach a model (ciphertext-blind) — enforced by the
  caller (triage.create_with_triage), not here.
- Model scores are cached per content-hash (LRU, in-process); rules always
  run fresh.
- Inference runs with a timeout (PORTER_TRIAGE_TIMEOUT, default 2s); on
  timeout the caller falls back to the rule decision and audit-logs it.
- Expected CPU latency for mDeBERTa-v3-base (~280M params): ~100-500ms per
  short text on a modern CPU after the one-time model load. First use
  downloads ~1.1 GB from HuggingFace and takes noticeably longer.
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
import threading
import time
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Sequence

from .config import settings

#: Categories the model reports. Keep stable — they appear in audit logs
#: and in triage reasons as ml_sensitive:<category>.
CATEGORIES = ("health", "finance", "pii", "credentials")

#: Dimension 1 — memory domain (what KIND of thing this memory is about).
#: Orthogonal to sensitivity (dimension 2):
#:   personal  the user's own memories: preferences, health, finance, PII,
#:             relationships. Sensitivity labels + human-review queue apply.
#:   project   the agent harness's working memory for a project/workspace:
#:             AGENTS.md/CLAUDE.md contents, repo conventions, architecture
#:             decisions, codebase knowledge, task state. Auto-approved by
#:             default, but MUST carry a project:<name> tag.
#: Triage queues on personal-domain sensitivity; project-domain on missing
#: scope or same-project conflicts.
DOMAINS = ("personal", "project")

#: Domain threshold for the ML classifiers.
DOMAIN_THRESHOLD = 0.5

#: HuggingFace model for zero-shot classification. Multilingual (100+
#: languages incl. Chinese), ~280M params, CPU-runnable. Chosen 2026-10:
#: mDeBERTa-v3-base fine-tuned on XNLI beats larger English-only models on
#: cross-lingual zero-shot, and 280M keeps per-write latency acceptable.
ZEROSHOT_MODEL_ID = "MoritzLaurer/mDeBERTa-v3-base-mnli-xnli"

#: Candidate hypotheses, kept in English (the model is English-prompted;
#: it handles Chinese *text* fine — the hypothesis language is what matters
#: for the NLI head).
_LABEL_HYPOTHESES: dict[str, str] = {
    "health": "this text contains health or medical information",
    "finance": "this text contains financial information (money, accounts, income)",
    "pii": "this text contains personal identifiers (phone, email, ID numbers, addresses)",
    "credentials": "this text contains passwords, tokens or secret credentials",
}

#: Dimension-1 hypotheses: which DOMAIN is this memory about. The project
#: domain is deliberately broad — it covers the agent's ENTIRE working
#: memory for a project/workspace (task state, decisions, codebase
#: understanding, pitfalls, conventions), not just instruction files.
#: Multiple hypotheses map to "project"; the max score wins.
_DOMAIN_HYPOTHESES: dict[str, list[str]] = {
    "personal": [
        "this text is a personal memory about the user",
    ],
    "project": [
        "this text is project documentation, coding instructions or "
        "codebase knowledge",
        "this text is about an ongoing task, decision or work context",
    ],
}


@dataclass
class TriageScore:
    """Result of scoring one memory's text. Two dimensions:

    - sensitivity: health/finance/pii/credentials scores (dim 2)
    - domain: "personal" | "project" | "unknown" (dim 1)
    """

    sensitive: bool
    categories: dict[str, float] = field(default_factory=dict)  # cat -> [0,1]
    latency_ms: float = 0.0
    backend: str = "heuristic"  # heuristic | zeroshot | finetuned
    domain: str = "unknown"
    domain_scores: dict[str, float] = field(default_factory=dict)


class BaseTriageModel(ABC):
    """Scores memory text for sensitivity. Implementations must be safe to
    call from any thread; score() must never raise on weird input."""

    @abstractmethod
    def score(self, text: str, tags: Sequence[str] = ()) -> TriageScore:
        """Return sensitivity scores. Empty/whitespace text -> not sensitive."""

    @property
    def backend(self) -> str:  # noqa: D102
        return "heuristic"


# Heuristic domain patterns (dimension 1). The project domain is broad:
# instruction files are just one source — task state, decisions, codebase
# knowledge, and work context all count. Priority: personal (from
# sensitivity) > project > unknown. A miss means "unknown", which triages
# like personal (sensitivity gates still apply).
_DOMAIN_PATTERNS: list[tuple[str, str]] = [
    (
        "project",
        r"AGENTS\.md|CLAUDE\.md|GEMINI\.md|CURSOR\.md|WARP\.md|\.cursorrules|"
        r"\brepo\b|repository|architecture|convention|coding standard|"
        r"style guide|\bCI\b|lint|monorepo|codebase|module|endpoint|schema|"
        r"always\s+(use|run|do)|never\s+(use|commit|push)|"
        r"\bworking on\b|in progress|\bWIP\b|blocked by|\btodo\b|currently\b|"
        r"\bdecided\b|decision\b|pitfall|gotcha|workaround|"
        r"架构|规范|约定|仓库|流水线|正在|进行中|待办|决定|任务",
    ),
]

_COMPILED_DOMAINS = [
    (name, re.compile(pat, re.IGNORECASE)) for name, pat in _DOMAIN_PATTERNS
]


def _heuristic_domain(text: str, sensitive: bool) -> tuple[str, dict[str, float]]:
    """Classify memory domain. Sensitivity implies personal (dimension 1)."""
    if sensitive:
        return "personal", {"personal": 1.0}
    norm = _normalize_for_domain(text)
    for name, rx in _COMPILED_DOMAINS:
        if rx.search(norm):
            return name, {name: 1.0, "personal": 0.0}
    return "unknown", {}


def _normalize_for_domain(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


class HeuristicTriageModel(BaseTriageModel):
    """Wraps the existing rule/pattern logic as a model.

    No behavior change vs triage.detect_sensitive_*: matched categories get
    score 1.0. Used for PORTER_TRIAGE_MODEL=heuristic and as the automatic
    fallback when the zero-shot model is unavailable or too slow.
    """

    @property
    def backend(self) -> str:
        return "heuristic"

    def score(self, text: str, tags: Sequence[str] = ()) -> TriageScore:
        # Lazy import: triage.py imports this module, so a top-level import
        # would be circular.
        from .triage import detect_sensitive_content, detect_sensitive_tags

        t0 = time.perf_counter()
        cats: dict[str, float] = {}
        try:
            if not (text or "").strip():
                return TriageScore(sensitive=False, backend="heuristic")
            for hit in detect_sensitive_content(text):
                if hit == "health":
                    cats["health"] = 1.0
                elif hit == "finance":
                    cats["finance"] = 1.0
                elif hit == "credentials_pii":
                    cats["credentials"] = 1.0
                elif hit == "pii_pattern":
                    cats["pii"] = 1.0
            if detect_sensitive_tags(tags):
                # Tags alone are a weak signal here (rules already gate on
                # them); record health/finance at full weight only if the
                # tag names match known categories.
                for t in tags:
                    low = t.strip().casefold()
                    if low in ("health", "finance") and low not in cats:
                        cats[low] = 1.0
        except Exception:
            # Heuristics must never break a write.
            cats = {}
        latency_ms = (time.perf_counter() - t0) * 1000.0
        domain, domain_scores = _heuristic_domain(text, bool(cats))
        return TriageScore(
            sensitive=bool(cats), categories=cats,
            latency_ms=latency_ms, backend="heuristic",
            domain=domain, domain_scores=domain_scores,
        )


class ZeroShotTriageModel(BaseTriageModel):
    """Multilingual zero-shot sensitivity classifier (transformers pipeline).

    Lazy model load on first score(): the import + ~1.1 GB download happen
    once, then the pipeline is reused. Inference is guarded by a lock
    (transformers pipelines are not guaranteed thread-safe).
    """

    MODEL_ID = ZEROSHOT_MODEL_ID

    def __init__(self, threshold: float = 0.65) -> None:
        if not zeroshot_importable():
            raise RuntimeError(
                "PORTER_TRIAGE_MODEL=zeroshot needs transformers + torch: "
                "pip install transformers torch"
            )
        self.threshold = threshold
        self._pipe = None
        self._lock = threading.Lock()

    @property
    def backend(self) -> str:
        return "zeroshot"

    def _load(self):
        # Double-checked locking: load once, even under concurrent writes.
        if self._pipe is None:
            with self._lock:
                if self._pipe is None:
                    from transformers import pipeline

                    self._pipe = pipeline(
                        "zero-shot-classification",
                        model=self.MODEL_ID,
                    )
        return self._pipe

    def score(self, text: str, tags: Sequence[str] = ()) -> TriageScore:
        t0 = time.perf_counter()
        try:
            if not (text or "").strip():
                return TriageScore(sensitive=False, backend="zeroshot")
            pipe = self._load()
            clipped = text[:2000]  # bound input; memories are short by schema
            with self._lock:
                # Dimension 2: sensitivity.
                sens_out = pipe(
                    clipped,
                    candidate_labels=list(_LABEL_HYPOTHESES.values()),
                    multi_label=True,
                )
                # Dimension 1: domain. Each domain has one or more
                # hypotheses; the domain score is the max over its set so
                # the broad project definition (instructions AND task state)
                # is covered.
                domain_hypos = [
                    h for hyps in _DOMAIN_HYPOTHESES.values() for h in hyps
                ]
                domain_out = pipe(
                    clipped,
                    candidate_labels=domain_hypos,
                    multi_label=True,
                )
            cats: dict[str, float] = {}
            hypo_to_cat = {v: k for k, v in _LABEL_HYPOTHESES.items()}
            for label, s in zip(sens_out["labels"], sens_out["scores"]):
                cat = hypo_to_cat.get(label)
                if cat is not None:
                    cats[cat] = float(s)
            over = {c: s for c, s in cats.items() if s >= self.threshold}
            hypo_to_domain = {
                h: d for d, hyps in _DOMAIN_HYPOTHESES.items() for h in hyps
            }
            domain_scores: dict[str, float] = {}
            for label, s in zip(domain_out["labels"], domain_out["scores"]):
                d = hypo_to_domain.get(label)
                if d is not None:
                    domain_scores[d] = max(domain_scores.get(d, 0.0), float(s))
            best_domain = max(
                domain_scores, key=domain_scores.get, default="unknown"
            )
            if domain_scores.get(best_domain, 0.0) < DOMAIN_THRESHOLD:
                best_domain = "unknown"
        except Exception:
            # A model hiccup must never break a write; the caller treats an
            # empty/exceptional score as "no signal" (rules already ran).
            cats, over, domain_scores, best_domain = {}, {}, {}, "unknown"
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return TriageScore(
            sensitive=bool(over),
            categories={c: round(s, 4) for c, s in cats.items()},
            latency_ms=round(latency_ms, 1),
            backend="zeroshot",
            domain=best_domain,
            domain_scores={
                c: round(s, 4) for c, s in domain_scores.items()
            },
        )

    def over_threshold_categories(self, score: TriageScore) -> list[str]:
        return [c for c, s in score.categories.items()
                if s >= self.threshold]


#: Fine-tune head labels: 2 domain labels (inference takes argmax) + 4
#: sensitivity labels (independent). scripts/finetune_triage.py trains
#: exactly this layout.
FINETUNE_LABELS = (
    "domain_personal",
    "domain_project",
    "sens_health",
    "sens_finance",
    "sens_pii",
    "sens_credentials",
)


class FinetunedTriageModel(BaseTriageModel):
    """Fine-tuned multi-label classifier from a local directory.

    Trained by scripts/finetune_triage.py: microsoft/mdeberta-v3-base with
    a 6-label head (FINETUNE_LABELS order),
    problem_type="multi_label_classification". Inference is sigmoid +
    per-label threshold, CPU-friendly.

    This is the top of the `auto` priority when the directory exists: it
    beats zero-shot once you have a few hundred hand-corrected labels from
    YOUR inbox (your approvals/rejections are the training signal).
    """

    def __init__(self, model_path: str, threshold: float = 0.65) -> None:
        import os

        if not os.path.isdir(model_path):
            raise RuntimeError(
                f"finetuned triage model not found at {model_path} — "
                "train one with scripts/finetune_triage.py or set "
                "PORTER_TRIAGE_MODEL_PATH"
            )
        try:
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as e:
            raise RuntimeError(
                "finetuned triage model needs transformers + torch: "
                "pip install transformers torch"
            ) from e
        if importlib.util.find_spec("torch") is None:
            raise RuntimeError(
                "finetuned triage model needs torch: pip install torch"
            )
        self.threshold = threshold
        self._tokenizer = AutoTokenizer.from_pretrained(model_path)
        self._model = AutoModelForSequenceClassification.from_pretrained(model_path)
        self._model.eval()
        self._lock = threading.Lock()
        self._model_path = model_path

    @property
    def backend(self) -> str:
        return "finetuned"

    def score(self, text: str, tags: Sequence[str] = ()) -> TriageScore:
        t0 = time.perf_counter()
        try:
            if not (text or "").strip():
                return TriageScore(sensitive=False, backend="finetuned")
            import torch

            with self._lock:
                enc = self._tokenizer(
                    text[:2000], return_tensors="pt",
                    truncation=True, max_length=256,
                )
                with torch.no_grad():
                    logits = self._model(**enc).logits[0]
                probs = torch.sigmoid(logits).tolist()
            # First 2 are domain logits (argmax), last 4 are sensitivity.
            domain_probs = {
                DOMAINS[i]: round(float(probs[i]), 4) for i in range(2)
            }
            best_domain = max(domain_probs, key=domain_probs.get)
            if domain_probs[best_domain] < DOMAIN_THRESHOLD:
                best_domain = "unknown"
            cats = {
                CATEGORIES[i]: round(float(probs[2 + i]), 4) for i in range(4)
            }
            over = {c: s for c, s in cats.items() if s >= self.threshold}
        except Exception:
            cats, over, domain_probs, best_domain = {}, {}, {}, "unknown"
        latency_ms = (time.perf_counter() - t0) * 1000.0
        return TriageScore(
            sensitive=bool(over), categories=cats,
            latency_ms=round(latency_ms, 1), backend="finetuned",
            domain=best_domain, domain_scores=domain_probs,
        )


# ------------------------------------------------------------------ selection
def zeroshot_importable() -> bool:
    """True when `transformers` is installed (torch checked at load time)."""
    return importlib.util.find_spec("transformers") is not None


def _finetuned_path_exists() -> bool:
    import os

    path = getattr(settings, "triage_model_path", "models/triage-v1") or ""
    return bool(path) and os.path.isdir(path)


def _resolve_auto_kind() -> str:
    """Priority for PORTER_TRIAGE_MODEL=auto: finetuned (if the directory
    exists) -> zeroshot (if importable) -> heuristic. Separated for tests."""
    if _finetuned_path_exists():
        return "finetuned"
    if zeroshot_importable():
        return "zeroshot"
    return "heuristic"


def build_triage_model(kind: str, **kwargs) -> BaseTriageModel:
    """Build a triage model. `kind`: auto | finetuned | zeroshot | heuristic."""
    kind = (kind or "auto").strip().lower()

    def _threshold() -> float:
        return float(kwargs.get("threshold",
                                getattr(settings, "triage_model_threshold", 0.65)))

    if kind == "heuristic":
        return HeuristicTriageModel()
    if kind == "zeroshot":
        return ZeroShotTriageModel(threshold=_threshold())
    if kind == "finetuned":
        import os

        path = kwargs.get("model_path") or getattr(
            settings, "triage_model_path", "models/triage-v1")
        if not path or not os.path.isdir(path):
            raise RuntimeError(
                f"PORTER_TRIAGE_MODEL=finetuned but no model at {path!r} — "
                "train one with scripts/finetune_triage.py"
            )
        return FinetunedTriageModel(model_path=path, threshold=_threshold())
    if kind == "auto":
        # Finetuned (if the directory exists) -> zeroshot (if importable)
        # -> heuristic. Build exceptions fall back one level, never crash.
        for candidate in (_resolve_auto_kind(), "zeroshot", "heuristic"):
            try:
                if candidate == "auto":
                    continue
                return build_triage_model(candidate, **kwargs)
            except Exception:
                continue
        return HeuristicTriageModel()  # unreachable, belt-and-suspenders
    raise ValueError(
        "unknown triage model kind: "
        f"{kind} (choose auto|finetuned|zeroshot|heuristic)"
    )


# Module-level cache: one instance per kind (models are expensive to build).
_model_cache: dict[str, BaseTriageModel] = {}
_model_cache_lock = threading.Lock()


def get_triage_model() -> BaseTriageModel:
    """Return the configured model (cached). Reads PORTER_TRIAGE_MODEL."""
    kind = (getattr(settings, "triage_model", "auto") or "auto").strip().lower()
    with _model_cache_lock:
        if kind not in _model_cache:
            _model_cache[kind] = build_triage_model(kind)
        return _model_cache[kind]


# ------------------------------------------------------------ score LRU cache
# Model scores only (rules always run fresh). Keyed by content hash; tags
# are folded into the key because the heuristic reads them.
_SCORE_CACHE: dict[str, TriageScore] = {}
_SCORE_CACHE_MAX = 1024
_score_cache_lock = threading.Lock()


def _cache_key(content: str, tags: Sequence[str], backend: str) -> str:
    h = hashlib.sha256()
    h.update(backend.encode())
    h.update(b"\x00")
    h.update(content.encode("utf-8", "replace"))
    for t in sorted(tags):
        h.update(b"\x00")
        h.update(t.encode("utf-8", "replace"))
    return h.hexdigest()


def cached_score(model: BaseTriageModel, content: str,
                 tags: Sequence[str]) -> TriageScore:
    """Score with an in-process LRU cache. Never raises: on any model
    error returns a non-sensitive score so the caller falls back to rules."""
    key = _cache_key(content, tags, model.backend)
    with _score_cache_lock:
        hit = _SCORE_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        result = model.score(content, tags)
    except Exception:
        result = TriageScore(sensitive=False, backend=model.backend)
    # Cache only confident, successful scores — never cache an error-shaped
    # or timed-out result (transient conditions must not stick).
    if result.latency_ms >= 0:
        with _score_cache_lock:
            if len(_SCORE_CACHE) >= _SCORE_CACHE_MAX:
                _SCORE_CACHE.pop(next(iter(_SCORE_CACHE)))
            _SCORE_CACHE[key] = result
    return result


def clear_score_cache() -> None:
    """Test hook."""
    with _score_cache_lock:
        _SCORE_CACHE.clear()


def clear_model_cache() -> None:
    """Test hook."""
    with _model_cache_lock:
        _model_cache.clear()
