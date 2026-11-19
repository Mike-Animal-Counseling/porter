"""PostgreSQL-backed native capture and current project handoff retrieval."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import auth
from .auth import AuthContext
from .config import settings
from .errors import InsufficientScope, InvalidInput, ServiceNotReady, Unauthorized
from .llm import build_extractor
from .repositories import HANDOFF_WINDOW_SQL, PostgresMemoryRepository
from .triage import detect_sensitive_content

router = APIRouter(prefix="/api/v1/project-handoffs")
log = logging.getLogger(__name__)
_PROJECT = re.compile(r"[a-z][a-z0-9_]{0,31}")
_HANDOFF = re.compile(r"(?m)^```porter-handoff[ \t]*\r?\n(.*?)\r?\n```[ \t]*$", re.S | re.M)
_COMMIT = re.compile(r"[0-9a-fA-F]{7,64}")
_CLAIM = """
    SELECT * FROM project_capture_jobs
    WHERE payload IS NOT NULL AND next_attempt <= clock_timestamp()
    ORDER BY next_attempt, received_at LIMIT 1 FOR UPDATE SKIP LOCKED
"""
_CURRENT_PROJECT = f"""
    scope_tags = ARRAY[$1]::text[] AND status = 'active' AND NOT encrypted
    AND superseded_by IS NULL AND source->>'pipeline' = 'native_handoff'
    AND {HANDOFF_WINDOW_SQL}
"""


class Capture(BaseModel):
    event_key: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    harness: Literal["codex", "claude", "dsh"]
    text: Annotated[str, Field(min_length=1, max_length=1800)]


class Recall(BaseModel):
    query: Annotated[str, Field(min_length=1, max_length=1000)]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["file", "commit", "verification"]
    ref: Annotated[str, Field(min_length=1, max_length=240)]

    @model_validator(mode="after")
    def valid_ref(self):
        if not self.ref.strip() or any(char in self.ref for char in "\r\n"):
            raise ValueError("Use a single-line evidence reference")
        if self.kind == "file":
            path, separator, line = self.ref.partition(":")
            if (
                "\\" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or (separator and not re.fullmatch(r"[1-9][0-9]*", line))
            ):
                raise ValueError("Use a repository-relative file path with an optional line")
        if self.kind == "commit" and not _COMMIT.fullmatch(self.ref):
            raise ValueError("Use a commit ID")
        if self.kind == "verification" and re.search(r"(?:[A-Za-z]:[\\/]|(?:^|\s)[\\/])", self.ref):
            raise ValueError("Use a verification result without local absolute paths")
        return self


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["verified", "hypothesis", "next_step"]
    conclusion: Annotated[str, Field(min_length=1, max_length=600)]
    evidence: list[Evidence] = Field(default_factory=list, max_length=4)
    next_step: Annotated[str, Field(max_length=400)] = ""
    effective_from: AwareDatetime | None = None
    effective_until: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_proposal(self):
        if not self.conclusion.strip():
            raise ValueError("A conclusion is required")
        if self.kind == "verified" and not self.evidence:
            raise ValueError("A verified conclusion needs evidence")
        if (
            self.effective_from
            and self.effective_until
            and self.effective_until <= self.effective_from
        ):
            raise ValueError("The effective window must have a positive duration")
        return self

    def render(self) -> str:
        lines = [f"{self.kind}: {self.conclusion.strip()}"]
        lines.extend(f"evidence ({item.kind}): {item.ref}" for item in self.evidence)
        if self.next_step.strip():
            lines.append(f"next step: {self.next_step.strip()}")
        return "\n".join(lines)


def authorize_project(repo, identity: AuthContext | None, tag: str, *, capture: bool = False):
    if not _PROJECT.fullmatch(tag):
        raise InvalidInput("invalid project tag")
    if identity is None or identity.client_pk is None:
        raise Unauthorized("an authenticated project OAuth client is required")
    scopes = {f"memory.read.{tag}"}
    if capture:
        scopes.add(f"memory.capture.{tag}")
    if not scopes.issubset(identity.scopes):
        raise InsufficientScope("this OAuth client is not enrolled for this project operation")
    if not isinstance(repo, PostgresMemoryRepository):
        raise ServiceNotReady("native handoffs require PostgreSQL")


async def queue_capture(repo, identity: AuthContext, tag: str, body: Capture) -> dict:
    authorize_project(repo, identity, tag, capture=True)
    if not body.text.strip() or detect_sensitive_content(body.text):
        raise InvalidInput("capture text is empty or matches a sensitive pattern")
    await repo.pool.execute(
        """
        INSERT INTO project_capture_jobs (client_id, project_tag, event_key, harness, payload)
        VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING
        """,
        identity.client_pk,
        tag,
        body.event_key,
        body.harness,
        body.text,
    )
    return {"accepted": True}


@router.post("/{tag}/captures", status_code=202)
async def capture_endpoint(tag: str, body: Capture, request: Request):
    return await queue_capture(request.app.state.repo, auth.current_auth(request), tag, body)


async def _proposals(text: str) -> list[Proposal]:
    matches = _HANDOFF.findall(text)
    if matches:
        try:
            proposal = Proposal.model_validate_json(matches[-1])
        except ValidationError:
            return []
        return [] if detect_sensitive_content(proposal.render()) else [proposal]
    extractor = build_extractor(settings.llm_extractor_kind)
    facts = await extractor.aextract_facts(text)
    return [
        Proposal(kind="hypothesis", conclusion=fact.content[:600])
        for fact in facts[:5]
        if fact.content.strip() and not detect_sensitive_content(fact.content)
    ]


async def process_capture(repo: PostgresMemoryRepository) -> bool:
    """Process one isolated job. The transaction rolls back on process exit."""
    async with repo.atomic([]):
        async with repo.pool.acquire() as connection:
            job = await connection.fetchrow(_CLAIM)
            if job is None:
                return False
            key = job["client_id"], job["project_tag"], job["event_key"]
            try:
                async with connection.transaction(), asyncio.timeout(45):
                    for proposal in await _proposals(job["payload"]):
                        effective = proposal.effective_from or job["received_at"]
                        if proposal.effective_until and proposal.effective_until <= effective:
                            continue
                        memory, _ = await repo.create_pending(
                            content=proposal.render(),
                            type="fact",
                            scope_tags=[job["project_tag"]],
                            confidence=1.0 if proposal.kind == "verified" else 0.5,
                            actor_client_id=job["client_id"],
                            triage_reasons=["native_capture_review"],
                            source={
                                "pipeline": "native_handoff",
                                "harness": job["harness"],
                                "event_key": job["event_key"],
                                "kind": proposal.kind,
                                "received_at": job["received_at"].isoformat(),
                                "effective_from": effective.timestamp(),
                                "effective_until": proposal.effective_until.timestamp()
                                if proposal.effective_until
                                else None,
                            },
                        )
                        await repo.audit(
                            action="write",
                            client_id=job["client_id"],
                            memory_ids=[memory.id],
                            meta={"via": "native_capture", "status": "pending"},
                        )
            except Exception as exc:
                log.warning("Native capture will retry (%s)", type(exc).__name__)
                await connection.execute(
                    """
                    UPDATE project_capture_jobs SET failures = failures + 1,
                        next_attempt = clock_timestamp() + make_interval(secs => 2.0 ^ failures),
                        payload = CASE WHEN failures = 4 THEN NULL ELSE payload END
                    WHERE client_id = $1 AND project_tag = $2 AND event_key = $3
                    """,
                    *key,
                )
            else:
                await connection.execute(
                    """
                    UPDATE project_capture_jobs SET payload = NULL, completed_at = clock_timestamp()
                    WHERE client_id = $1 AND project_tag = $2 AND event_key = $3
                    """,
                    *key,
                )
    return True


async def run_worker(repo: PostgresMemoryRepository) -> None:
    while True:
        try:
            if not await process_capture(repo):
                await asyncio.sleep(0.25)
        except Exception as exc:
            log.warning("Native capture worker will retry (%s)", type(exc).__name__)
            await asyncio.sleep(2)


async def current_handoffs(repo: PostgresMemoryRepository, tag: str, query: str) -> list[dict]:
    embedding = await repo.embedder.aembed_one(query)
    rows = await repo.pool.fetch(
        f"""
        WITH current_records AS MATERIALIZED (
            SELECT id, version, content, source, embedding, created_at FROM memories
            WHERE {_CURRENT_PROJECT}
        ), ranked AS (
            SELECT id, version, content, source, created_at,
              GREATEST(COALESCE(1 - (embedding <=> $2::vector), 0),
                LEAST(1.0, ts_rank_cd(to_tsvector('simple', content),
                                     plainto_tsquery('simple', $3)) * 5)) AS score
            FROM current_records
        )
        SELECT id, version, content, source->>'kind' AS kind, source->>'harness' AS harness,
               source->>'effective_from' AS effective_from,
               source->>'effective_until' AS effective_until, created_at, score
        FROM ranked WHERE score >= 0.25
        ORDER BY score DESC, created_at DESC, id LIMIT 5
        """,
        tag,
        PostgresMemoryRepository._vec_literal(embedding),
        query,
    )
    return [dict(row) | {"scope_tags": [tag], "untrusted_data": True} for row in rows]


async def revalidate_candidates(
    repo: PostgresMemoryRepository, tag: str, candidates: dict[UUID, int]
) -> list[UUID]:
    """Authorize derived IDs and exact versions against the current Postgres view."""
    if not candidates:
        return []
    rows = await repo.pool.fetch(
        f"SELECT id, version FROM memories WHERE {_CURRENT_PROJECT} AND id = ANY($2::uuid[])",
        tag,
        list(candidates),
    )
    current = {row["id"]: row["version"] for row in rows}
    return [
        memory_id for memory_id, version in candidates.items() if current.get(memory_id) == version
    ]


async def search_current(repo, identity: AuthContext, tag: str, body: Recall) -> dict:
    authorize_project(repo, identity, tag)
    results = await current_handoffs(repo, tag, body.query)
    await repo.audit(
        action="read",
        client_id=identity.client_pk,
        memory_ids=[row["id"] for row in results],
        meta={"via": "native_handoff"},
    )
    return {"results": results}


@router.post("/{tag}/search")
async def recall_endpoint(tag: str, body: Recall, request: Request):
    return await search_current(request.app.state.repo, auth.current_auth(request), tag, body)
