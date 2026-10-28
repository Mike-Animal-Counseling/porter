"""Runtime configuration. Everything via env vars (12-factor), see .env.example."""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from ipaddress import IPv6Address
from urllib.parse import urlsplit


def env_bool(name: str, default: bool) -> bool:
    """Parse flags consistently; a typo must never disable authentication."""
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be 1/0, true/false, yes/no or on/off")


def repository_kind() -> str:
    """Reject storage typos instead of silently using an ephemeral repository."""
    value = os.getenv("PORTER_REPO", "memory").strip().lower()
    if value not in {"memory", "postgres"}:
        raise ValueError("PORTER_REPO must be memory or postgres")
    return value


def _allowed_hosts() -> list[str]:
    value = os.getenv("PORTER_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]")
    hosts = [host.strip().lower() for host in value.split(",")]
    for host in hosts:
        if host.startswith("[") and host.endswith("]"):
            try:
                IPv6Address(host[1:-1])
            except ValueError:
                raise ValueError("PORTER_ALLOWED_HOSTS contains an invalid IPv6 address") from None
        elif not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
            raise ValueError(
                "PORTER_ALLOWED_HOSTS must contain exact hostnames/IPs without ports or wildcards"
            )
    return list(dict.fromkeys(hosts))


def _public_origin() -> str:
    """Explicit external origin for TLS termination; never trust proxy headers."""
    value = os.getenv("PORTER_PUBLIC_ORIGIN", "").strip().rstrip("/")
    if not value:
        return ""
    try:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path
            or url.query
            or url.fragment
        ):
            raise ValueError
        _ = url.port
    except ValueError:
        raise ValueError(
            "PORTER_PUBLIC_ORIGIN must be an http(s) origin without credentials or a path"
        ) from None
    return value


def _default_setup_token() -> str:
    # P0: one-time bootstrap token. The open first-registration + consent
    # screen without owner auth would otherwise let any network neighbor
    # self-register and self-approve memory.admin during setup.
    return os.getenv("PORTER_SETUP_TOKEN", "") or secrets.token_urlsafe(32)


@dataclass
class Settings:
    allowed_hosts: list[str] = field(default_factory=_allowed_hosts)
    public_origin: str = field(default_factory=_public_origin)
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", "postgresql://porter:porter@localhost:5432/porter"
        )
    )
    # hash | sentence-transformer | bge-m3 | qwen3 | harrier | embeddinggemma | http | openai
    # PORTER_EMBEDDER is the documented flag (EMBEDDER_KIND kept as alias).
    # Default stays "hash": real models load in Docker, never implicitly.
    embedder_kind: str = field(
        default_factory=lambda: os.getenv("PORTER_EMBEDDER", os.getenv("EMBEDDER_KIND", "hash"))
    )
    embedder_url: str = field(
        default_factory=lambda: os.getenv("EMBEDDER_URL", "http://embedder:8001")
    )
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    # W2: Bearer token enforcement is ON by default (PRD §10).
    # Set AUTH_ENABLED=0 only for local dev without OAuth.
    auth_enabled: bool = field(default_factory=lambda: env_bool("AUTH_ENABLED", True))
    # HS256 signing key for access-token JWTs. If unset, a random key is
    # generated per process (tokens do not survive restarts) — see oauth.py.
    jwt_secret: str = field(default_factory=lambda: os.getenv("PORTER_JWT_SECRET", ""))
    # T5: per-client rate limit (requests/minute). In-memory token bucket.
    rate_limit_per_minute: int = field(
        default_factory=lambda: int(os.getenv("PORTER_RATE_LIMIT_PER_MINUTE", "60"))
    )
    access_token_ttl_seconds: int = 3600  # PRD §10.2: 1h
    refresh_token_ttl_days: int = 30  # PRD §10.2: 30d
    auth_code_ttl_minutes: int = 10  # PRD §10.2: 10 min, one-time
    refresh_reuse_grace_seconds: int = 10  # race window before reuse = theft
    duplicate_threshold: float = 0.92
    # P0: gate for the very first client registration (X-Porter-Setup-Token).
    # Auto-generated per process unless PORTER_SETUP_TOKEN is set; the value
    # is logged loudly at startup when auto-generated.
    setup_token: str = field(default_factory=_default_setup_token)
    setup_token_is_env: bool = field(default_factory=lambda: bool(os.getenv("PORTER_SETUP_TOKEN")))
    # v1 snapshots are local files (PRD §9.1, panel).
    snapshot_dir: str = field(default_factory=lambda: os.getenv("PORTER_SNAPSHOT_DIR", "snapshots"))
    # LLM Wiki layer (entity graph, prototype): PORTER_WIKI_ENABLED=1 turns
    # on entity extraction at approve-time + hybrid graph-boosted retrieval.
    # Default OFF: with the flag off, search behavior is identical to v1.
    wiki_enabled: bool = field(default_factory=lambda: env_bool("PORTER_WIKI_ENABLED", False))
    # Fact/wiki extractor: stub (default) | ollama (explicit model configuration).
    llm_extractor_kind: str = field(
        default_factory=lambda: os.getenv("PORTER_LLM_EXTRACTOR", "stub")
    )
    # Automation layer: tiered approval for new memories (porter/triage.py).
    # "tiered" (default): low-risk memories auto-approve (audited); sensitive
    # content/tags, possible conflicts, and low confidence queue for humans.
    # "strict": v1 behavior — every write goes to the pending inbox.
    approval_mode: str = field(default_factory=lambda: os.getenv("PORTER_APPROVAL_MODE", "tiered"))
    # ML triage model (porter/triage_model.py): second opinion over
    # rule-passed writes. "auto" (default): finetuned when the model
    # directory exists, then zero-shot when transformers is importable,
    # else heuristic. "zeroshot" forces the model (needs transformers +
    # torch). "heuristic" is pure rules, no extra latency.
    triage_model: str = field(default_factory=lambda: os.getenv("PORTER_TRIAGE_MODEL", "auto"))
    # Per-category zero-shot threshold (0..1). A write queues when any
    # category scores >= this. Default 0.65.
    triage_model_threshold: float = field(
        default_factory=lambda: float(os.getenv("PORTER_TRIAGE_THRESHOLD", "0.65"))
    )
    # Inference timeout per write (seconds). On timeout the rule decision
    # stands and the fallback is audit-logged.
    triage_model_timeout: float = field(
        default_factory=lambda: float(os.getenv("PORTER_TRIAGE_TIMEOUT", "2.0"))
    )
    # Local directory with a fine-tuned triage classifier
    # (scripts/finetune_triage.py output). Top of the `auto` priority when
    # the directory exists.
    triage_model_path: str = field(
        default_factory=lambda: os.getenv("PORTER_TRIAGE_MODEL_PATH", "models/triage-v1")
    )


settings = Settings()
