"""Portable State snapshots, v1 minimal (PRD §9).

A snapshot is a versioned, optionally encrypted bundle of active memories
(filtered by scope_tags) that can be moved to another Porter instance.
v1 scope (per task brief): create (choose tags, set password), list history,
download; apply = upload -> dry-run manifest -> confirm -> memories land in
the pending review inbox (never directly into active).

Encryption: AES-256-GCM, key derived via PBKDF2HMAC-SHA256 (600k rounds).
File layout: b"PORTER1" + salt(16) + nonce(12) + ciphertext.
Signing: Ed25519 (PRD §9.2). The instance keypair is generated on first
snapshot creation and stored 0o600 under <snapshot_dir>/.keys/. Every
snapshot's payload is signed; inspect/apply verify the signature and fail
closed on mismatch. Legacy unsigned files are still accepted but flagged
(signature_ok=False) in the report.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .db import json_object
from .schemas import (
    MAX_CIPHERTEXT_LEN,
    MAX_CONTENT_LEN,
    MAX_KEY_ID_LEN,
    MEMORY_TYPES,
    _validate_tags,
)

SNAPSHOT_FORMAT = "porter-snapshot/v1"
MAGIC = b"PORTER1"
PBKDF2_ROUNDS = 600_000
SNAPSHOT_MEMORY_TYPES = MEMORY_TYPES
_SIGNING_KEY_NAME = "ed25519.key"


# ------------------------------------------------------------------ crypto
def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ROUNDS)
    return kdf.derive(password.encode("utf-8"))


def encrypt_snapshot(payload: bytes, password: str) -> bytes:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    ct = AESGCM(_derive_key(password, salt)).encrypt(nonce, payload, None)
    return MAGIC + salt + nonce + ct


def decrypt_snapshot(blob: bytes, password: str) -> bytes:
    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 28:
        raise ValueError("not a Porter snapshot file")
    salt = blob[len(MAGIC) : len(MAGIC) + 16]
    nonce = blob[len(MAGIC) + 16 : len(MAGIC) + 28]
    ct = blob[len(MAGIC) + 28 :]
    try:
        return AESGCM(_derive_key(password, salt)).decrypt(nonce, ct, None)
    except Exception as exc:
        raise ValueError("decryption failed: wrong password or corrupted file") from exc


# ------------------------------------------------------------------ signing
def _instance_private_key(keys_dir: str) -> Ed25519PrivateKey:
    """Load (or generate) the instance Ed25519 signing key.

    The private key file is created 0o600. Anyone who can read this file
    can forge snapshots for this instance — it lives next to the memories
    themselves, so this adds no new trust assumption (T6: detects tampering
    in transit, e.g. USB/cloud-drive moves between your own machines).
    """
    os.makedirs(keys_dir, exist_ok=True)
    key_path = os.path.join(keys_dir, _SIGNING_KEY_NAME)
    if os.path.exists(key_path):
        raw = open(key_path, "rb").read()
        if len(raw) != 32:
            raise ValueError(f"corrupt signing key at {key_path}")
        return Ed25519PrivateKey.from_private_bytes(raw)
    key = Ed25519PrivateKey.generate()
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key.private_bytes_raw())
    return key


def _canonical_bytes(body: dict[str, Any]) -> bytes:
    """Deterministic JSON encoding for signing/verification."""
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def sign_snapshot_body(body: dict[str, Any], keys_dir: str) -> dict[str, Any]:
    """Return a copy of *body* with an attached Ed25519 signature block.

    The signature covers the canonical encoding of the body WITHOUT the
    signature block itself (no chicken-and-egg). The block travels inside
    the file, so another machine can verify without the local DB.
    """
    key = _instance_private_key(keys_dir)
    sig = key.sign(_canonical_bytes(body))
    pub = key.public_key().public_bytes_raw()
    signed = dict(body)
    signed["signature"] = {
        "sig": base64.b64encode(sig).decode(),
        "pubkey": base64.b64encode(pub).decode(),
    }
    return signed


def verify_snapshot_body(body: dict[str, Any]) -> bool | None:
    """Verify the attached signature block of a snapshot body.

    Returns True (valid). Never returns False — mismatch raises ValueError.
    Returns None for legacy unsigned snapshot files (no signature block).
    """
    if not isinstance(body, dict):
        raise ValueError("snapshot payload must be an object")
    if "signature" not in body:
        return None
    block = body["signature"]
    if not isinstance(block, dict):
        raise ValueError("invalid snapshot signature")
    try:
        sig = base64.b64decode(block["sig"], validate=True)
        pub = base64.b64decode(block["pubkey"], validate=True)
        unsigned = {k: v for k, v in body.items() if k != "signature"}
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, _canonical_bytes(unsigned))
    except (KeyError, TypeError, ValueError, InvalidSignature) as exc:
        raise ValueError("snapshot signature verification failed") from exc
    return True


# ------------------------------------------------------------------ store
@dataclass
class SnapshotRecord:
    id: UUID
    manifest: dict[str, Any]
    storage_path: str
    signature_ok: bool
    created_at: datetime


class SnapshotStore(ABC):
    @abstractmethod
    async def create(
        self,
        *,
        manifest: dict[str, Any],
        storage_path: str,
        signature_ok: bool = False,
    ) -> SnapshotRecord: ...

    @abstractmethod
    async def list(self, limit: int = 50) -> list[SnapshotRecord]: ...

    @abstractmethod
    async def get(self, snapshot_id: UUID) -> SnapshotRecord | None: ...


class InMemorySnapshotStore(SnapshotStore):
    def __init__(self) -> None:
        self._rows: dict[UUID, SnapshotRecord] = {}

    async def create(self, *, manifest, storage_path, signature_ok=False):
        rec = SnapshotRecord(
            id=uuid4(),
            manifest=dict(manifest),
            storage_path=storage_path,
            signature_ok=signature_ok,
            created_at=datetime.now(UTC),
        )
        self._rows[rec.id] = rec
        return rec

    async def list(self, limit=50):
        rows = sorted(self._rows.values(), key=lambda r: r.created_at, reverse=True)
        return rows[:limit]

    async def get(self, snapshot_id):
        return self._rows.get(snapshot_id)


class PostgresSnapshotStore(SnapshotStore):
    def __init__(self, pool) -> None:
        self.pool = pool

    async def create(self, *, manifest, storage_path, signature_ok=False):
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                "INSERT INTO snapshots (manifest, storage_path, signature_ok)"
                " VALUES ($1, $2, $3)"
                " RETURNING id, manifest, storage_path, signature_ok, created_at",
                json.dumps(manifest),
                storage_path,
                signature_ok,
            )
        return SnapshotRecord(
            id=row["id"],
            manifest=json_object(row["manifest"]),
            storage_path=row["storage_path"],
            signature_ok=row["signature_ok"],
            created_at=row["created_at"],
        )

    async def list(self, limit=50):
        async with self.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT id, manifest, storage_path, signature_ok, created_at"
                " FROM snapshots ORDER BY created_at DESC LIMIT $1",
                limit,
            )
        return [
            SnapshotRecord(
                id=r["id"],
                manifest=json_object(r["manifest"]),
                storage_path=r["storage_path"],
                signature_ok=r["signature_ok"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def get(self, snapshot_id):
        async with self.pool.acquire() as con:
            row = await con.fetchrow(
                "SELECT id, manifest, storage_path, signature_ok, created_at"
                " FROM snapshots WHERE id = $1",
                snapshot_id,
            )
        if row is None:
            return None
        return SnapshotRecord(
            id=row["id"],
            manifest=json_object(row["manifest"]),
            storage_path=row["storage_path"],
            signature_ok=row["signature_ok"],
            created_at=row["created_at"],
        )


# ------------------------------------------------------------------ build / apply
def _memory_to_export_dict(rec) -> dict[str, Any]:
    # Embeddings are deliberately NOT exported: the importing instance
    # re-embeds with its own embedder (avoids dim/model mismatch).
    # E2EE (P2): encrypted memories serialize as ciphertext with the flag —
    # there is no plaintext server-side to leak; the importer restores the
    # flag verbatim (see _validated_entries).
    return {
        "content": rec.content,
        "type": rec.type,
        "scope_tags": list(rec.scope_tags),
        "confidence": rec.confidence,
        "source": dict(rec.source),
        "created_at": rec.created_at.isoformat(),
        "encrypted": bool(rec.encrypted),
        "key_id": rec.key_id,
    }


async def create_snapshot(
    repo,
    store: SnapshotStore,
    *,
    tags: Sequence[str] = (),
    password: str | None = None,
    storage_dir: str,
    instance_id: str = "local",
    keys_dir: str | None = None,
) -> tuple[SnapshotRecord, str]:
    """Build a snapshot file; returns (record, file path).

    The snapshot body is Ed25519-signed with the instance key (PRD §9.2);
    the signature block travels inside the file so another machine can
    verify it without access to this instance's database.
    """
    memories = await repo.list(status="active", tags=list(tags), limit=100_000)
    items = [_memory_to_export_dict(m) for m in memories]
    body = {
        "format": SNAPSHOT_FORMAT,
        "memories": items,
    }
    signed_body = sign_snapshot_body(body, keys_dir or os.path.join(storage_dir, ".keys"))
    payload = json.dumps(signed_body, ensure_ascii=False).encode("utf-8")
    manifest = {
        "snapshot_format": SNAPSHOT_FORMAT,
        "created_at": datetime.now(UTC).isoformat(),
        "source_instance_id": instance_id,
        "filter_tags": list(tags),
        "counts": {"memories": len(items), "skills": 0, "tasks": 0},
        "content_sha256": hashlib.sha256(payload).hexdigest(),
        "encrypted": bool(password),
        "signature": signed_body["signature"],
        "signature_ok": True,
    }
    os.makedirs(storage_dir, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex
    if password:
        blob = encrypt_snapshot(payload, password)
        fname = f"snapshot-{stamp}.porter"
    else:
        blob = payload
        fname = f"snapshot-{stamp}.porter.json"
    path = os.path.join(storage_dir, fname)
    # 0o600: snapshots contain the user's private memories.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(blob)
    rec = await store.create(manifest=manifest, storage_path=path, signature_ok=True)
    return rec, path


def _validated_entries(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate every snapshot memory entry; raise ValueError on any problem.

    Shared by inspect (dry-run) and apply so the preview never promises an
    import the confirm step would refuse. A crafted snapshot must fail
    cleanly (ValueError -> 422), never KeyError -> 500, and must not smuggle
    oversized payloads past the 20k REST content limit.
    """
    if body.get("format") != SNAPSHOT_FORMAT:
        raise ValueError(f"unsupported snapshot format: {body.get('format')!r}")
    memories = body.get("memories", [])
    if not isinstance(memories, list):
        raise ValueError("snapshot memories must be an array")
    out: list[dict[str, Any]] = []
    for i, m in enumerate(memories):
        if not isinstance(m, dict):
            raise ValueError(f"snapshot entry #{i} is not an object")
        content = m.get("content")
        if not isinstance(content, str) or not content:
            raise ValueError(f"snapshot entry #{i} is missing content")
        # E2EE (P2): encrypted entries carry ciphertext + the flag; the
        # plaintext cap does not apply to them (wire cap 40k, base64 check).
        encrypted = m.get("encrypted", False)
        if not isinstance(encrypted, bool):
            raise ValueError(f"snapshot entry #{i} has invalid encrypted flag")
        key_id = m.get("key_id")
        if encrypted:
            if not isinstance(key_id, str) or not (1 <= len(key_id) <= MAX_KEY_ID_LEN):
                raise ValueError(f"snapshot entry #{i} is encrypted but has an invalid key_id")
            if len(content) > MAX_CIPHERTEXT_LEN:
                raise ValueError(f"snapshot entry #{i} exceeds {MAX_CIPHERTEXT_LEN} chars")
            try:
                base64.b64decode(content, validate=True)
            except ValueError:
                raise ValueError(f"snapshot entry #{i} has invalid base64 ciphertext")
        elif len(content) > MAX_CONTENT_LEN:
            raise ValueError(f"snapshot entry #{i} exceeds {MAX_CONTENT_LEN} chars")
        mtype = m.get("type", "fact")
        if mtype not in SNAPSHOT_MEMORY_TYPES:
            raise ValueError(f"snapshot entry #{i} has invalid type: {mtype!r}")
        raw_tags = m.get("scope_tags", [])
        if not isinstance(raw_tags, list):
            raise ValueError(f"snapshot entry #{i} has invalid scope_tags")
        try:
            tags = _validate_tags(raw_tags)
        except ValueError as exc:
            raise ValueError(f"snapshot entry #{i}: {exc}") from exc
        try:
            confidence = float(m.get("confidence", 0.5))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"snapshot entry #{i} has invalid confidence") from exc
        if not (0.0 <= confidence <= 1.0):
            raise ValueError(f"snapshot entry #{i} has invalid confidence")
        source = m.get("source", {})
        if not isinstance(source, dict):
            raise ValueError(f"snapshot entry #{i} has invalid source")
        out.append(
            {
                "content": content,
                "type": mtype,
                "scope_tags": tags,
                "confidence": confidence,
                "source": dict(source),
                "encrypted": encrypted,
                "key_id": key_id if encrypted else None,
            }
        )
    return out


def inspect_snapshot_file(path: str, password: str | None) -> dict[str, Any]:
    """Dry-run: parse + validate a snapshot file without importing anything.

    Returns the manifest plus a content summary. Raises ValueError on any
    problem (wrong password, corrupt file, unknown format, bad signature).
    """
    with open(path, "rb") as f:
        blob = f.read()
    if blob.startswith(MAGIC):
        if not password:
            raise ValueError("this snapshot is encrypted: password required")
        payload = decrypt_snapshot(blob, password)
    else:
        payload = blob
    try:
        body = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise ValueError("snapshot payload is not valid JSON") from exc
    signature_ok = verify_snapshot_body(body)  # raises on tampered signature
    entries = _validated_entries(body)
    memories = body.get("memories", [])
    tag_counts: dict[str, int] = {}
    for m in memories:
        for t in m.get("scope_tags", []):
            tag_counts[t] = tag_counts.get(t, 0) + 1
    return {
        "format": body["format"],
        "memory_count": len(entries),
        "tag_counts": tag_counts,
        "content_sha256": hashlib.sha256(payload).hexdigest(),
        "signature_ok": signature_ok,
        "sample": [m.get("content", "")[:120] for m in memories[:5]],
    }


async def apply_snapshot_file(
    repo, audit, *, path: str, password: str | None, source_label: str = "snapshot"
) -> dict[str, Any]:
    """Import a snapshot's memories into the PENDING inbox (never active
    directly — PRD §9.2). Verifies the Ed25519 signature first and fails
    closed on tampering. Returns a report dict."""
    with open(path, "rb") as f:
        blob = f.read()
    if blob.startswith(MAGIC):
        if not password:
            raise ValueError("this snapshot is encrypted: password required")
        payload = decrypt_snapshot(blob, password)
    else:
        payload = blob
    body = json.loads(payload.decode("utf-8"))
    signature_ok = verify_snapshot_body(body)  # raises on tampered signature
    entries = _validated_entries(body)
    imported: list[str] = []
    for e in entries:
        source = e["source"]
        source["imported_via"] = source_label
        rec, _dups = await repo.create_pending(
            content=e["content"],
            type=e["type"],
            scope_tags=e["scope_tags"],
            confidence=e["confidence"],
            source=source,
            # E2EE (P2): encrypted entries restore the flag verbatim —
            # ciphertext is never re-embedded or deduplicated.
            encrypted=e["encrypted"],
            key_id=e["key_id"],
        )
        imported.append(str(rec.id))
    await audit(
        action="snapshot_apply",
        memory_ids=[],
        scopes_used=["memory.admin"],
        meta={
            "file": os.path.basename(path),
            "imported": len(imported),
            "signature_ok": bool(signature_ok),
        },
    )
    report: dict[str, Any] = {"imported": len(imported), "memory_ids": imported}
    if signature_ok is None:
        # Legacy unsigned file: accepted for backward compatibility, but the
        # user must know it was not cryptographically verified.
        report["warning"] = "snapshot is not signed; contents not verified"
    return report
