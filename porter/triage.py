"""Tiered approval triage for new memories (automation layer).

Design principle: automate the mechanical, keep humans on judgment.

- QUEUE (human review): sensitive content/tags, possible conflicts with
  existing memories, low model confidence, E2EE ciphertext (server is
  blind), or `PORTER_APPROVAL_MODE=strict`.
- AUTO-APPROVE: everything else — goes active immediately, audit-logged
  with reasons, reversible (edit/delete like any memory).
- AUTO-MERGE: similarity >= 0.97 to an active memory means "essentially
  identical" — fold into it, no inbox. The classic 0.92 threshold keeps
  its existing duplicate-candidates path (flag, don't merge).

Security notes (T3):
- Sensitivity is checked on CONTENT (normalized scan), not just tags —
  a malicious writer can set any tags it likes, so tag-only checks are
  bypassable. Tags are still checked (normalized: NFKC + strip + lower)
  as a second signal.
- Attacker-controlled `confidence=1.0` cannot force auto-approve: the
  conflict and sensitivity gates are similarity/content based, never
  confidence based. Confidence only gates one way (low -> queue).
- Extracted facts (memory_capture) go through the exact same triage;
  extraction cannot escalate privilege.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal, Sequence
from uuid import UUID

from .config import settings

# ------------------------------------------------------------------ thresholds
#: Similarity in [CONFLICT_MIN, DUPLICATE_THRESHOLD) -> possible conflict.
CONFLICT_MIN_SIMILARITY = 0.70
#: Similarity >= this -> auto-merge into the existing memory.
AUTO_MERGE_SIMILARITY = 0.97
#: Confidence below this -> human review.
LOW_CONFIDENCE = 0.6
#: Idle decay: confidence *= DECAY_FACTOR every DECAY_PERIOD_DAYS idle.
DECAY_FACTOR = 0.95
DECAY_PERIOD_DAYS = 30
#: Effective confidence below this on read -> auto-archive.
ARCHIVE_CONFIDENCE = 0.2

#: Tags that always force human review (normalized before compare).
SENSITIVE_TAGS = frozenset({"health", "finance"})

TriageAction = Literal["auto_approve", "queue", "auto_merge"]


@dataclass
class TriageDecision:
    action: TriageAction
    reasons: list[str] = field(default_factory=list)
    #: auto_merge target.
    merge_target_id: UUID | None = None
    #: ids of active memories in the conflict band (for side-by-side UI).
    conflict_ids: list[UUID] = field(default_factory=list)
    #: ids of duplicate candidates (>= 0.92, existing path).
    duplicate_ids: list[UUID] = field(default_factory=list)
    #: (id, similarity) for duplicate candidates, for the API response.
    duplicate_scores: list[tuple[UUID, float]] = field(default_factory=list)


# ------------------------------------------------------- sensitivity scanning
_ZERO_WIDTH = re.compile(r"[\u200b-\u200d\ufeff]")


def _normalize(text: str) -> str:
    """NFKC + strip zero-width chars + casefold. Defeats fullwidth/unicode
    smuggling (e.g. 'ｈｅａｌｔｈ', 'he​alth' with zero-width joiners)."""
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH.sub("", text)
    return text.casefold()


def _norm_tag(tag: str) -> str:
    return _normalize(tag).strip()


# Best-effort heuristic patterns (EN + ZH). False positives are safe: they
# only route to human review, never block. False negatives are mitigated by
# the tag check + low-confidence gate + the fact that auto-approved memories
# stay fully audited and reversible.
_SENSITIVE_PATTERNS: list[tuple[str, str]] = [
    (
        "health",
        r"allerg\w*|asthma|diabet|diagnos|medicat|prescription|surgery|"
        r"hospital|doctor|disease|symptom|treatment|cancer|therap|clinic|"
        r"psychiat|depress|anxiet|insulin|vaccine|lisinopril|"
        r"\b\d+\s*(mg|mcg|ml|units?)\b|"  # dosage mentions
        r"过敏|哮喘|糖尿病|诊断|药物|处方|手术|医院|医生|疾病|症状|治疗|癌症|诊所|抑郁|焦虑|胰岛素|疫苗|看病|吃药",
    ),
    (
        "finance",
        r"\bsalary\b|wages?|income\b|bank account|credit card|debit card|"
        r"account number|routing number|ssn|social security|tax return|"
        r"工资|薪水|收入|银行账户|信用卡|借记卡|账号|社保|报税|理财|股票|基金|贷款",
    ),
    (
        "credentials_pii",
        r"password\s*(is|=|:)|passwd\s*(is|=|:)|\bpin\s*(is|=|:)|"
        r"api[_-]?key\s*(is|=|:)|\bsecret\s*(is|=|:)|\btoken\s*(is|=|:)|\n"
        r"密码\s*(是|为|:|：)|密钥\s*(是|为|:|：)",
    ),
    (
        "pii_pattern",
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"  # email
        r"|\b\d{3}-\d{2}-\d{4}\b"  # US SSN
        r"|\b\d{16}\b"  # card-like 16 digits
        r"|1[3-9]\d{9}",  # CN mobile
    ),
]

_COMPILED_SENSITIVE = [
    (name, re.compile(pat)) for name, pat in _SENSITIVE_PATTERNS
]


def detect_sensitive_content(text: str) -> list[str]:
    """Return sensitivity categories matched in `text` (normalized scan).

    Never raises on weird input; empty text matches nothing (the schema
    already rejects empty content upstream — this is belt-and-suspenders).
    """
    if not text:
        return []
    norm = _normalize(text)
    return [name for name, rx in _COMPILED_SENSITIVE if rx.search(norm)]


def detect_sensitive_tags(tags: Sequence[str]) -> list[str]:
    """Normalized tag check — defeats 'health ' / 'ＨＥＡＬＴＨ' smuggling."""
    return sorted({_norm_tag(t) for t in tags} & SENSITIVE_TAGS)


# ------------------------------------------------------------------- decision
#: Memory domain taxonomy (dimension 1). See porter/triage_model.py.
#: A memory belongs to the user's PERSONAL domain or a PROJECT domain —
#: there is no middle ground; "unknown" triages like the pre-taxonomy
#: default (auto-approve eligible, sensitivity/conflict gates still apply).
NEW_TAXONOMY = frozenset({"personal", "project"})
#: v1 type values mapped onto the domain taxonomy for triage purposes.
OLD_TYPE_TO_NEW = {"preference": "personal"}


def _project_names(tags: Sequence[str]) -> set[str]:
    """Extract project names from `project:<name>` tags (normalized)."""
    return project_names(tags)


def project_names(tags: Sequence[str]) -> set[str]:
    """Public: normalized project names from `project:<name>` scope tags."""
    out = set()
    for t in tags:
        norm = _norm_tag(t)
        if norm.startswith("project:"):
            name = norm[len("project:"):].strip()
            if name:
                out.add(name)
    return out


def project_tag(name: str) -> str:
    """Build a normalized `project:<name>` scope tag from a project name.

    Used by the REST/MCP `project` convenience parameter so harnesses don't
    hand-roll the tag format. Raises ValueError on an empty name; overlong
    names are rejected downstream by tag-length validation (fail loudly,
    never silently truncate — two truncated names could collide).
    """
    norm = _normalize(name).strip()
    norm = re.sub(r"\s+", "-", norm)
    if not norm:
        raise ValueError("project name must not be empty")
    return f"project:{norm}"


def has_project_tag(tags: Sequence[str]) -> bool:
    """True when any tag is a `project:<name>` tag (normalized)."""
    return any(
        _norm_tag(t).startswith("project:") and _norm_tag(t)[len("project:"):].strip()
        for t in tags
    )


def _norm_similar(similar):
    """Normalize similar entries to (id, score, tags) triples."""
    out = []
    for entry in similar:
        if len(entry) == 3:
            out.append((entry[0], entry[1], list(entry[2] or ())))
        else:
            out.append((entry[0], entry[1], []))
    return out


def decide(
    *,
    content: str,
    scope_tags: Sequence[str],
    confidence: float,
    encrypted: bool,
    # (record_id, similarity[, tags]) of ACTIVE, non-encrypted memories,
    # descending by similarity. Tags enable project-scoped conflict checks.
    similar: Sequence[tuple],
    approval_mode: str = "tiered",
    duplicate_threshold: float = 0.92,
    # ML second opinion (porter/triage_model.py): categories the model
    # flagged over threshold, e.g. ["health"]. Empty = model not run or
    # no signal. Treated exactly like a rule sensitivity hit: queues the
    # memory and blocks auto-merge.
    ml_categories: Sequence[str] = (),
    # Dimension 1: one of "personal" | "project" | "unknown". Callers
    # resolve it from the explicit type and/or the ML classifier;
    # "unknown" triages like the pre-taxonomy default.
    domain: str = "unknown",
) -> TriageDecision:
    """Pure policy function — no I/O, fully unit-testable.

    Two-domain triage:
    - personal domain OR any sensitivity hit (dim 2) -> queue for humans.
    - project domain -> auto-approve by default (audit-logged), BUT the
      memory MUST carry a project:<name> tag; without one it queues with
      missing_project_scope. Conflict detection is per-project: a new
      instruction contradicting an existing memory with the SAME project
      tag queues side-by-side; cross-project similarities are not
      conflicts.
    - unknown domain -> auto-approve eligible (sensitivity, conflict, and
      confidence gates still apply).
    """
    if approval_mode == "strict":
        return TriageDecision(action="queue", reasons=["strict_mode"])
    if encrypted:
        # Ciphertext-blind: never scan, never auto-approve, never merge.
        return TriageDecision(action="queue", reasons=["encrypted"])

    reasons: list[str] = []
    # Dimension 1: personal domain always queues (like a sensitivity hit).
    if domain == "personal":
        reasons.append("personal_domain")
    tag_hits = detect_sensitive_tags(scope_tags)
    if tag_hits:
        reasons.append("sensitive_tag")
    content_hits = detect_sensitive_content(content)
    if content_hits:
        reasons.append("sensitive_content")
    for _ml_cat in ml_categories:
        reasons.append(f"ml_sensitive:{_ml_cat}")
    if not (0.0 <= confidence <= 1.0):
        # Defensive: schema clamps this, but a caller bug must fail toward
        # human review, never toward auto-approve.
        reasons.append("low_confidence")
    elif confidence < LOW_CONFIDENCE:
        reasons.append("low_confidence")

    # Dimension 1: project memories must be scoped to a project. Unscoped
    # project content could leak across projects — queue for a human to tag
    # (or confirm global scope).
    my_projects = _project_names(scope_tags)
    if domain == "project" and not my_projects:
        reasons.append("missing_project_scope")

    sim3 = _norm_similar(similar)
    dup_ids = [mid for mid, s, _ in sim3 if s >= duplicate_threshold]
    dup_scores = [(mid, s) for mid, s, _ in sim3 if s >= duplicate_threshold]
    if domain == "project" and my_projects:
        # Conflicts only matter within the same project: instructions for
        # project A never conflict with project B's.
        conflict_ids = [
            mid for mid, s, tags in sim3
            if CONFLICT_MIN_SIMILARITY <= s < duplicate_threshold
            and (my_projects & _project_names(tags))
        ]
    else:
        conflict_ids = [
            mid for mid, s, _ in sim3
            if CONFLICT_MIN_SIMILARITY <= s < duplicate_threshold
        ]

    # Near-identical -> merge, but NEVER when any sensitivity/low-confidence
    # flag fired: a sensitive near-duplicate still deserves human eyes
    # (e.g. a corrected dosage). For project-domain writes the merge target
    # must share a project tag — a project:A memory must never fold into a
    # project:B memory, or the project scope is silently lost (the
    # duplicate_candidate path below still flags cross-project near-dups
    # for human review instead).
    merge_target_id = None
    if not reasons:
        for mid, s, tags in sim3:
            if s < AUTO_MERGE_SIMILARITY:
                break  # sim3 is similarity-descending
            if (domain == "project" and my_projects
                    and not (my_projects & _project_names(tags))):
                continue  # same text, different project: not a merge
            merge_target_id = mid
            break
    if merge_target_id is not None:
        return TriageDecision(
            action="auto_merge",
            reasons=["near_duplicate"],
            merge_target_id=merge_target_id,
            duplicate_ids=dup_ids,
            duplicate_scores=dup_scores,
        )
    if dup_ids:
        reasons.append("duplicate_candidate")
    if conflict_ids:
        reasons.append("possible_conflict")

    if reasons:
        return TriageDecision(
            action="queue",
            reasons=reasons,
            conflict_ids=conflict_ids,
            duplicate_ids=dup_ids,
            duplicate_scores=dup_scores,
        )
    return TriageDecision(action="auto_approve", reasons=["low_risk"])


# ------------------------------------------------------------------ orchestration
# ------------------------------------------------- ML second opinion (triage_model)
async def _ml_second_opinion(
    content: str, scope_tags: Sequence[str]
) -> tuple[list[str], str, dict]:
    """Run the ML triage model as a second opinion over a rule-passed write.

    Returns (flagged_categories, predicted_domain, meta). Never raises: on
    timeout, missing deps, or any model error the rule decision stands and
    the fallback is reported in meta for the audit log. Results are
    LRU-cached per content hash (model scores only; rules always run fresh).
    """
    # Lazy import: triage_model imports config only at module level, but
    # keeping the import here avoids any import-order surprises and lets
    # tests monkeypatch porter.triage_model.get_triage_model cleanly.
    from . import triage_model as _tm

    timeout = float(getattr(settings, "triage_model_timeout", 2.0))
    meta: dict = {"backend": "none", "fallback": None, "latency_ms": 0.0}
    try:
        model = _tm.get_triage_model()
    except Exception:
        meta["fallback"] = "model_unavailable"
        return [], "unknown", meta
    meta["backend"] = model.backend
    try:
        import asyncio as _asyncio

        score = await _asyncio.wait_for(
            _asyncio.to_thread(_tm.cached_score, model, content, scope_tags),
            timeout=timeout,
        )
    except _asyncio.TimeoutError:
        meta["fallback"] = "timeout"
        return [], "unknown", meta
    except Exception:
        meta["fallback"] = "error"
        return [], "unknown", meta
    meta["latency_ms"] = score.latency_ms
    meta["predicted_domain"] = score.domain
    threshold = float(getattr(settings, "triage_model_threshold", 0.65))
    flagged = sorted(
        c for c, s in score.categories.items() if s >= threshold
    )
    # Heuristic backend reports 1.0 for hits / nothing otherwise, so the
    # threshold check is a no-op for it; zeroshot needs it.
    if score.backend == "heuristic":
        flagged = sorted(score.categories.keys()) if score.sensitive else []
    return flagged, score.domain, meta


async def create_with_triage(
    repo,
    *,
    content: str,
    type: str,
    scope_tags: Sequence[str],
    confidence: float,
    source: dict,
    actor_client_id=None,
    encrypted: bool = False,
    key_id: str | None = None,
    approval_mode: str | None = None,
    via: str = "api",
    scopes_used: Sequence[str] = (),
) -> tuple:
    """Write path shared by REST / MCP / panel: embed -> triage -> branch.

    Returns (record, decision). `record` is None only for auto_merge (the
    incoming memory was folded into the target; use decision.merge_target_id).
    Audit entries: "auto_approve" / "write" (queued) / "auto_merge".
    """
    mode = approval_mode or getattr(settings, "approval_mode", "tiered")
    dup_threshold = getattr(settings, "duplicate_threshold", 0.92)

    if encrypted:
        rec, dups = await repo.create_pending(
            content=content, type=type, scope_tags=scope_tags,
            confidence=confidence, source=source,
            actor_client_id=actor_client_id, encrypted=True, key_id=key_id,
        )
        decision = TriageDecision(action="queue", reasons=["encrypted"])
        await repo.audit(
            action="write", memory_ids=[rec.id], scopes_used=list(scopes_used),
            client_id=actor_client_id,
            meta={"via": via, "triage": decision.reasons},
        )
        return rec, decision

    vec = repo.embedder.embed_one(content)
    similar = await repo.find_similar_active(vec, threshold=CONFLICT_MIN_SIMILARITY, limit=5)
    # 3-tuples carry tags for project-scoped conflict checks.
    similar3 = [(s.record.id, s.score, list(s.record.scope_tags or ()))
                for s in similar]

    # Dimension 1 resolution: explicit new-taxonomy type wins; v1
    # "preference" maps to the personal domain; otherwise the ML model gets
    # a vote on the second pass.
    explicit_domain = type if type in NEW_TAXONOMY else OLD_TYPE_TO_NEW.get(type)
    first_pass_domain = explicit_domain or "unknown"
    decision = decide(
        content=content, scope_tags=scope_tags, confidence=confidence,
        encrypted=False,
        similar=similar3,
        approval_mode=mode, duplicate_threshold=dup_threshold,
        domain=first_pass_domain,
    )

    # ML second opinion: rules are the fast pre-filter — if they already
    # queued the memory, the model adds nothing. Only rule-passed writes
    # (auto_approve / auto_merge candidates) get the model check, which
    # contributes BOTH dimensions (sensitivity + predicted domain).
    ml_meta: dict = {"backend": "none", "fallback": None, "latency_ms": 0.0}
    if decision.action in ("auto_approve", "auto_merge"):
        ml_cats, ml_domain, ml_meta = await _ml_second_opinion(content, scope_tags)
        # "personal" is sticky: either the explicit type or the model
        # saying personal queues the memory. Otherwise explicit wins.
        if explicit_domain == "personal" or ml_domain == "personal":
            effective_domain = "personal"
        else:
            effective_domain = explicit_domain or ml_domain or "unknown"
        if ml_cats or effective_domain != first_pass_domain:
            # Re-decide with the ML signal: sensitivity queues the memory
            # and blocks auto-merge; a refined domain re-evaluates the
            # project-tag and personal rules.
            decision = decide(
                content=content, scope_tags=scope_tags, confidence=confidence,
                encrypted=False,
                similar=similar3,
                approval_mode=mode, duplicate_threshold=dup_threshold,
                ml_categories=ml_cats,
                domain=effective_domain,
            )

    def _audit_meta(extra: dict | None = None) -> dict:
        m = {"via": via, "triage": decision.reasons, "triage_model": ml_meta}
        if extra:
            m.update(extra)
        return m

    if decision.action == "auto_merge":
        assert decision.merge_target_id is not None
        target = await repo.auto_merge(
            decision.merge_target_id, content=content,
            confidence=confidence,
            actor_client_id=actor_client_id,
        )
        if target is None:
            # Lost a race (target deleted/archived between probe and merge):
            # fall back to the safe path — queue for a human.
            decision = TriageDecision(
                action="queue", reasons=["merge_target_gone"],
                duplicate_ids=decision.duplicate_ids,
                duplicate_scores=decision.duplicate_scores,
            )
        else:
            import hashlib

            await repo.audit(
                action="auto_merge", memory_ids=[target.id],
                scopes_used=list(scopes_used), client_id=actor_client_id,
                meta=_audit_meta({
                    "similarity": round(similar[0].score, 4),
                    "content_sha256": hashlib.sha256(
                        content.encode("utf-8", "replace")).hexdigest(),
                    "content_preview": content[:200],
                }),
            )
            return None, decision

    if decision.action == "auto_approve":
        rec, _dups = await repo.create_active(
            content=content, type=type, scope_tags=scope_tags,
            confidence=confidence, source=source,
            actor_client_id=actor_client_id,
            triage_reasons=decision.reasons,
        )
        await repo.audit(
            action="auto_approve", memory_ids=[rec.id],
            scopes_used=list(scopes_used),
            client_id=actor_client_id,
            meta=_audit_meta(),
        )
        return rec, decision

    # queue (default, incl. strict mode)
    rec, dups = await repo.create_pending(
        content=content, type=type, scope_tags=scope_tags,
        confidence=confidence, source=source,
        actor_client_id=actor_client_id,
        triage_reasons=decision.reasons,
    )
    # Merge the repo's own dup list with triage's (same source, keep both
    # consistent — repo list is authoritative for the response).
    decision.duplicate_ids = [d.record.id for d in dups]
    decision.duplicate_scores = [(d.record.id, d.score) for d in dups]
    await repo.audit(
        action="write", memory_ids=[rec.id], scopes_used=list(scopes_used),
        client_id=actor_client_id,
        meta=_audit_meta(),
    )
    return rec, decision
