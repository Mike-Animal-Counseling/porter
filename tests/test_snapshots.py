"""Snapshot Ed25519 signing tests (PRD §9.2, T6).

Covers: sign/verify round-trip, tamper detection (fail-closed),
legacy unsigned files (accepted with explicit warning), key hygiene.
Repo convention: asyncio.run() inside sync tests (no pytest-asyncio).
"""

import asyncio
import json
import os

import pytest

from porter.embeddings import HashEmbedder
from porter.repositories import InMemoryMemoryRepository
from porter.snapshots import (
    InMemorySnapshotStore,
    _instance_private_key,
    apply_snapshot_file,
    create_snapshot,
    inspect_snapshot_file,
    sign_snapshot_body,
    verify_snapshot_body,
)


def _mem(content="prefers tea over coffee", tags=("dietary",)):
    return {
        "content": content,
        "type": "preference",
        "scope_tags": list(tags),
        "confidence": 0.9,
    }


async def _make_active_repo(*contents):
    repo = InMemoryMemoryRepository(HashEmbedder())
    for c in contents:
        rec, _ = await repo.create_pending(
            content=c,
            type="preference",
            scope_tags=["dietary"],
            confidence=0.9,
            source={},
        )
        await repo.approve(rec.id)
    return repo


async def _null_audit(**kw):
    return None


def test_sign_verify_roundtrip(tmp_path):
    body = {"format": "porter-snapshot/v1", "memories": [_mem()]}
    signed = sign_snapshot_body(body, str(tmp_path / ".keys"))
    assert verify_snapshot_body(signed) is True
    assert "signature" in signed and "sig" in signed["signature"]


def test_tampered_content_fails_verification(tmp_path):
    body = {"format": "porter-snapshot/v1", "memories": [_mem()]}
    signed = sign_snapshot_body(body, str(tmp_path / ".keys"))
    tampered = json.loads(json.dumps(signed))
    tampered["memories"][0]["content"] = "attacker-controlled content"
    with pytest.raises(ValueError, match="signature verification failed"):
        verify_snapshot_body(tampered)


def test_signature_from_other_key_verifies_tofu(tmp_path):
    # Trust-on-first-use model (v1): a snapshot signed by ANY valid Ed25519
    # key verifies. This is intentional — the user moves snapshots between
    # their own machines, each with its own instance key. The signature
    # proves "not modified since signing", not "from an authorized instance".
    # Forged-but-valid snapshots still land in the pending-review inbox
    # (human gate), never directly into active memory.
    body = {"format": "porter-snapshot/v1", "memories": [_mem()]}
    signed = sign_snapshot_body(body, str(tmp_path / ".keys"))
    other = sign_snapshot_body(body, str(tmp_path / ".other-keys"))
    signed["signature"] = other["signature"]
    assert verify_snapshot_body(signed) is True


def test_legacy_unsigned_returns_none():
    assert verify_snapshot_body({"format": "x", "memories": []}) is None
    assert verify_snapshot_body({"format": "x"}) is None


def test_key_persisted_and_0600(tmp_path):
    keys = str(tmp_path / ".keys")
    k1 = _instance_private_key(keys)
    k2 = _instance_private_key(keys)
    assert k1.private_bytes_raw() == k2.private_bytes_raw()
    mode = oct(os.stat(os.path.join(keys, "ed25519.key")).st_mode & 0o777)
    if os.name != "nt":
        assert mode == "0o600"


def test_create_inspect_apply_signed_flow(tmp_path):
    repo = asyncio.run(_make_active_repo("likes oat milk"))
    store = InMemorySnapshotStore()
    sdir = str(tmp_path / "snaps")
    rec_snap, path = asyncio.run(create_snapshot(repo, store, tags=["dietary"], storage_dir=sdir))
    assert rec_snap.signature_ok is True
    assert rec_snap.manifest["signature"] is not None

    summary = inspect_snapshot_file(path, None)
    assert summary["signature_ok"] is True
    assert summary["memory_count"] == 1

    # tamper the file on disk -> inspect and apply must refuse
    raw = open(path, "rb").read().decode("utf-8")
    evil = raw.replace("oat milk", "rat poison")
    assert evil != raw
    open(path, "wb").write(evil.encode("utf-8"))
    with pytest.raises(ValueError, match="signature verification failed"):
        inspect_snapshot_file(path, None)
    with pytest.raises(ValueError, match="signature verification failed"):
        asyncio.run(apply_snapshot_file(repo, _null_audit, path=path, password=None))


def test_apply_legacy_unsigned_warns(tmp_path):
    repo = asyncio.run(_make_active_repo())
    # hand-written legacy file: valid schema, no signature block
    body = {"format": "porter-snapshot/v1", "memories": [_mem("old note")]}
    path = str(tmp_path / "legacy.porter.json")
    open(path, "w").write(json.dumps(body))

    report = asyncio.run(apply_snapshot_file(repo, _null_audit, path=path, password=None))
    assert report["imported"] == 1
    assert "warning" in report and "not signed" in report["warning"]


def test_encrypted_snapshot_still_verifies(tmp_path):
    repo = asyncio.run(_make_active_repo("secret pref"))
    store = InMemorySnapshotStore()
    sdir = str(tmp_path / "snaps")
    asyncio.run(create_snapshot(repo, store, password="correct-horse", storage_dir=sdir))
    # find the .porter file
    path = [f for f in os.listdir(sdir) if f.endswith(".porter")][0]
    summary = inspect_snapshot_file(os.path.join(sdir, path), "correct-horse")
    assert summary["signature_ok"] is True
    with pytest.raises(ValueError, match="password"):
        inspect_snapshot_file(os.path.join(sdir, path), "wrong-password")
