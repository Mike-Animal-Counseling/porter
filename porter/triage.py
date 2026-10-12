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
def decide(
    *,
    content: str,
    scope_tags: Sequence[str],
    confidence: float,
    encrypted: bool,
    # (record_id, similarity) of active memories, descending by similarity.
    similar: Sequence[tuple[UUID, float]],
    approval_mode: str = "tiered",
    duplicate_threshold: float = 0.92,
) -> TriageDecision:
    """Pure policy function — no I/O, fully unit-testable.

    `similar` must only contain ACTIVE, non-encrypted memories (ciphertext
    is not comparable; callers enforce this).
    """
    if approval_mode == "strict":
        return TriageDecision(action="queue", reasons=["strict_mode"])
    if encrypted:
        # Ciphertext-blind: never scan, never auto-approve, never merge.
        return TriageDecision(action="queue", reasons=["encrypted"])

    reasons: list[str] = []
    tag_hits = detect_sensitive_tags(scope_tags)
    if tag_hits:
        reasons.append("sensitive_tag")
    content_hits = detect_sensitive_content(content)
    if content_hits:
        reasons.append("sensitive_content")
    if not (0.0 <= confidence <= 1.0):
        # Defensive: schema clamps this, but a caller bug must fail toward
        # human review, never toward auto-approve.
        reasons.append("low_confidence")
    elif confidence < LOW_CONFIDENCE:
        reasons.append("low_confidence")

    dup_ids = [mid for mid, s in similar if s >= duplicate_threshold]
    dup_scores = [(mid, s) for mid, s in similar if s >= duplicate_threshold]
    conflict_ids = [
        mid for mid, s in similar if CONFLICT_MIN_SIMILARITY <= s < duplicate_threshold
    ]

    # Near-identical -> merge, but NEVER when any sensitivity/low-confidence
    # flag fired: a sensitive near-duplicate still deserves human eyes
    # (e.g. a corrected dosage).
    if similar and similar[0][1] >= AUTO_MERGE_SIMILARITY and not reasons:
        return TriageDecision(
            action="auto_merge",
            reasons=["near_duplicate"],
            merge_target_id=similar[0][0],
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
    decision = decide(
        content=content, scope_tags=scope_tags, confidence=confidence,
        encrypted=False,
        similar=[(s.record.id, s.score) for s in similar],
        approval_mode=mode, duplicate_threshold=dup_threshold,
    )

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
                meta={
                    "via": via,
                    "similarity": round(similar[0].score, 4),
                    "content_sha256": hashlib.sha256(
                        content.encode("utf-8", "replace")).hexdigest(),
                    "content_preview": content[:200],
                },
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
            meta={"via": via, "triage": decision.reasons},
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
        meta={"via": via, "triage": decision.reasons},
    )
    return rec, decision
