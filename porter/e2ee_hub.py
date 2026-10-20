"""P2 E2EE blind-hub key distribution (Porter PRD §9.5).

Problem
-------
Porter's E2EE foundation already exists: clients encrypt memory content with
XChaCha20-Poly1305 (``clients/python/porter_client.py`` ``encrypt_content`` /
``decrypt_content``; the 32-byte key lives in ``~/.porter/keys/<key_id>.key``
at mode 0600) and the server stores only ciphertext — it never decays,
triages, or embeds encrypted rows (see ``migrations/005_e2ee_share.sql`` and
``porter/schemas.py::EncryptionInfo``, whose ``alg`` is the exact literal
``"xchacha20-poly1305"``).

The missing piece was: how does the 32-byte data key travel between the
user's own devices (laptop, desktop, phone) without the hub ever learning it?

Solution: NaCl sealed boxes (``crypto_box_seal``)
------------------------------------------------
Every device owns an X25519 keypair. The private key never leaves the device.
To share a data key K with device B, device A seals K inside a
``nacl.public.SealedBox`` addressed to B's *public* key and posts the sealed
blob to the hub. The hub stores and forwards opaque sealed boxes only:

    Device A ── seal(K, B_pub) ──> hub (sees only ciphertext)
                                              ──> Device B ── open(box, B_priv) ──> K

Security claim (blind hub)
--------------------------
The hub is *mathematically* unable to open a sealed envelope: opening requires
the recipient's X25519 private key, which is generated on-device and never
transmitted. Recovering K from a sealed box without that private key would
require breaking the sealed-box construction (ephemeral ECDH + XSalsa20-Poly1305).
This is a strictly stronger guarantee than "encrypted at rest on the server"
(e.g. SharedContext's model of AES-encrypted blobs synced to Arweave): there,
whoever controls the sync layer could in principle be handed the key; here,
the hub has no key to be handed, ever.

Boundary of responsibility
--------------------------
* Client-side (this module + ``porter_client.py``): keypair generation,
  data-key generation, sealing/opening, re-encrypting memories on rotation.
* Server-side (REST wiring, not implemented here): store sealed envelopes,
  register device public keys, hand unclaimed envelopes to their recipient.
  The server MUST NOT accept plaintext data keys — the suggested endpoints
  below have no field for one, by construction.

Suggested REST wiring (for ``api/``; not implemented here)
-----------------------------------------------------------
* ``POST /api/v1/e2ee/devices``
  Body: ``{"device_id": "...", "public_key": "<b64 X25519 pubkey>"}``
  Registers (or rotates) a device's public key. Authenticated like every
  other endpoint (OAuth/JWT).
* ``POST /api/v1/e2ee/envelopes``
  Body: ``{"to_device": "...", "from_device": "...", "sealed": "<b64 sealed box>"}``
  Stores one sealed key envelope. The server validates base64 shape only —
  it cannot and must not interpret the payload.
* ``GET /api/v1/e2ee/envelopes?device_id=<id>``
  Returns this device's unclaimed envelopes and atomically marks them claimed
  (``claimed_at = now()``), so each envelope is delivered exactly once per
  device even under retries.

Rotation model
--------------
Key rotation is client-driven (``rotate_plan`` below returns a plan, not an
action): the owning device generates a new data key, seals it for every other
registered device, posts the envelopes, then re-encrypts its memories with
``encrypt_content(plaintext, new_key_id)`` — plaintext never leaves the
client. The hub only relays the new sealed envelopes.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime

from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.utils import random as random_bytes

from .errors import InvalidInput

# ---------------------------------------------------------------- constants

#: XChaCha20-Poly1305 key size. Data keys sealed by this module are exactly
#: this long so they drop straight into ``encrypt_content`` / ``decrypt_content``.
DATA_KEY_SIZE = 32

#: X25519 public key size (bytes).
X25519_PUBLIC_KEY_SIZE = 32

#: Max device_id length (mirrors the DB column).
MAX_DEVICE_ID_LEN = 128


# ------------------------------------------------------------------- helpers


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str, what: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidInput(f"invalid base64 for {what}: {exc}") from exc


def _check_device_id(device_id: str) -> str:
    if not isinstance(device_id, str) or not device_id.strip():
        raise InvalidInput("device_id must be a non-empty string")
    if len(device_id) > MAX_DEVICE_ID_LEN:
        raise InvalidInput(f"device_id longer than {MAX_DEVICE_ID_LEN} chars")
    return device_id


def _check_data_key(data_key: bytes) -> bytes:
    if not isinstance(data_key, (bytes, bytearray)) or len(data_key) != DATA_KEY_SIZE:
        raise InvalidInput(f"data key must be exactly {DATA_KEY_SIZE} bytes")
    return bytes(data_key)


def _check_public_key(public_key_b64: str) -> PublicKey:
    raw = _b64d(public_key_b64, "device public key")
    if len(raw) != X25519_PUBLIC_KEY_SIZE:
        raise InvalidInput(
            f"X25519 public key must be {X25519_PUBLIC_KEY_SIZE} bytes, got {len(raw)}"
        )
    return PublicKey(raw)


def _check_private_key(private_key_b64: str) -> PrivateKey:
    raw = _b64d(private_key_b64, "device private key")
    if len(raw) != X25519_PUBLIC_KEY_SIZE:
        raise InvalidInput(
            f"X25519 private key must be {X25519_PUBLIC_KEY_SIZE} bytes, got {len(raw)}"
        )
    return PrivateKey(raw)


# ------------------------------------------------- device keypairs & sealing


def generate_device_keypair() -> tuple[str, str]:
    """Generate an X25519 device keypair.

    Returns ``(private_key_b64, public_key_b64)``. The private key is for
    this device only: persist it locally (0600, like ``~/.porter/keys/``)
    and never transmit it. Only the public key is registered with the hub.
    """
    private = PrivateKey.generate()
    return _b64e(bytes(private)), _b64e(bytes(private.public_key))


def generate_data_key() -> bytes:
    """Generate a fresh 32-byte data key.

    Format-compatible with the XChaCha20-Poly1305 keys used by
    ``porter_client.encrypt_content`` — the sealed payload of
    ``seal_key_for`` can be saved straight to ``~/.porter/keys/<key_id>.key``.
    """
    return random_bytes(DATA_KEY_SIZE)


def seal_key_for(data_key: bytes, recipient_public_b64: str) -> str:
    """Seal a data key for one recipient device (NaCl ``crypto_box_seal``).

    The returned base64 blob can only be opened with the recipient's private
    key. The hub sees nothing but this opaque ciphertext — this is the core
    of the blind-hub guarantee.
    """
    _check_data_key(data_key)
    box = SealedBox(_check_public_key(recipient_public_b64))
    return _b64e(box.encrypt(data_key))


def open_sealed_key(sealed_b64: str, recipient_private_b64: str) -> bytes:
    """Open a sealed key envelope with the recipient's private key.

    Raises ``nacl.exceptions.CryptoError`` when the box was not sealed for
    this private key (or is corrupted) — the hub, holding no private key,
    can never succeed here.
    """
    sealed = _b64d(sealed_b64, "sealed key envelope")
    box = SealedBox(_check_private_key(recipient_private_b64))
    return box.decrypt(sealed)


# ----------------------------------------------------------------- envelopes


@dataclass
class KeyEnvelope:
    """One sealed data-key envelope in transit through the hub."""

    id: str
    to_device: str
    from_device: str
    sealed: str  # base64 sealed box; opaque to the hub
    created_at: datetime
    claimed_at: datetime | None = None


@dataclass
class DeviceRecord:
    """A registered device: id + public key. The hub never holds privates."""

    device_id: str
    public_key: str  # base64 X25519 public key
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class EnvelopeStore(ABC):
    """Storage for device public keys and sealed key envelopes.

    Deliberately separate from ``repositories.py``: this store never touches
    memory content, encrypted or otherwise — it only moves sealed keys.
    """

    @abstractmethod
    async def register_device(self, device_id: str, public_key_b64: str) -> DeviceRecord:
        """Register (or rotate) a device's public key. Idempotent per id."""
        ...

    @abstractmethod
    async def get_device(self, device_id: str) -> DeviceRecord | None: ...

    @abstractmethod
    async def list_devices(self) -> list[DeviceRecord]: ...

    @abstractmethod
    async def post_envelope(self, to_device: str, from_device: str, sealed_b64: str) -> KeyEnvelope:
        """Store one sealed envelope addressed to ``to_device``."""
        ...

    @abstractmethod
    async def claim_envelopes(self, device_id: str) -> list[KeyEnvelope]:
        """Return this device's unclaimed envelopes and mark them claimed.

        Must be idempotent: a second call returns [] until new envelopes
        arrive, so client retries can never double-deliver a key.
        """
        ...


class InMemoryEnvelopeStore(EnvelopeStore):
    """In-memory store (tests / single-process dev)."""

    def __init__(self) -> None:
        self._devices: dict[str, DeviceRecord] = {}
        self._envelopes: dict[str, KeyEnvelope] = {}

    async def register_device(self, device_id: str, public_key_b64: str) -> DeviceRecord:
        device_id = _check_device_id(device_id)
        _check_public_key(public_key_b64)  # validate shape, reject garbage early
        rec = DeviceRecord(
            device_id=device_id,
            public_key=public_key_b64,
            created_at=datetime.now(UTC),
        )
        self._devices[device_id] = rec
        return rec

    async def get_device(self, device_id: str) -> DeviceRecord | None:
        return self._devices.get(_check_device_id(device_id))

    async def list_devices(self) -> list[DeviceRecord]:
        return list(self._devices.values())

    async def post_envelope(self, to_device: str, from_device: str, sealed_b64: str) -> KeyEnvelope:
        to_device = _check_device_id(to_device)
        from_device = _check_device_id(from_device)
        _b64d(sealed_b64, "sealed envelope")  # shape check only; hub stays blind
        env = KeyEnvelope(
            id=str(uuid.uuid4()),
            to_device=to_device,
            from_device=from_device,
            sealed=sealed_b64,
            created_at=datetime.now(UTC),
        )
        self._envelopes[env.id] = env
        return env

    async def claim_envelopes(self, device_id: str) -> list[KeyEnvelope]:
        device_id = _check_device_id(device_id)
        now = datetime.now(UTC)
        claimed: list[KeyEnvelope] = []
        for env in self._envelopes.values():
            if env.to_device == device_id and env.claimed_at is None:
                env.claimed_at = now
                claimed.append(env)
        claimed.sort(key=lambda e: e.created_at)
        return claimed


class PostgresEnvelopeStore(EnvelopeStore):
    """Production store over asyncpg (tables from ``migrations/008_e2ee_hub.sql``).

    ``pool`` is an ``asyncpg.Pool``. Claiming is a single atomic UPDATE ...
    RETURNING, so concurrent claimers cannot double-deliver.
    """

    def __init__(self, pool) -> None:  # pool: asyncpg.Pool (import kept soft)
        self._pool = pool

    async def register_device(self, device_id: str, public_key_b64: str) -> DeviceRecord:
        device_id = _check_device_id(device_id)
        _check_public_key(public_key_b64)
        row = await self._pool.fetchrow(
            """
            INSERT INTO devices (device_id, public_key)
            VALUES ($1, $2)
            ON CONFLICT (device_id) DO UPDATE SET public_key = EXCLUDED.public_key
            RETURNING device_id, public_key, created_at
            """,
            device_id,
            public_key_b64,
        )
        return DeviceRecord(
            device_id=row["device_id"],
            public_key=row["public_key"],
            created_at=row["created_at"],
        )

    async def get_device(self, device_id: str) -> DeviceRecord | None:
        row = await self._pool.fetchrow(
            "SELECT device_id, public_key, created_at FROM devices WHERE device_id = $1",
            _check_device_id(device_id),
        )
        if row is None:
            return None
        return DeviceRecord(
            device_id=row["device_id"],
            public_key=row["public_key"],
            created_at=row["created_at"],
        )

    async def list_devices(self) -> list[DeviceRecord]:
        rows = await self._pool.fetch(
            "SELECT device_id, public_key, created_at FROM devices ORDER BY created_at"
        )
        return [
            DeviceRecord(
                device_id=r["device_id"],
                public_key=r["public_key"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def post_envelope(self, to_device: str, from_device: str, sealed_b64: str) -> KeyEnvelope:
        to_device = _check_device_id(to_device)
        from_device = _check_device_id(from_device)
        _b64d(sealed_b64, "sealed envelope")
        row = await self._pool.fetchrow(
            """
            INSERT INTO key_envelopes (to_device, from_device, sealed)
            VALUES ($1, $2, $3)
            RETURNING id, to_device, from_device, sealed, created_at, claimed_at
            """,
            to_device,
            from_device,
            sealed_b64,
        )
        return KeyEnvelope(
            id=str(row["id"]),
            to_device=row["to_device"],
            from_device=row["from_device"],
            sealed=row["sealed"],
            created_at=row["created_at"],
            claimed_at=row["claimed_at"],
        )

    async def claim_envelopes(self, device_id: str) -> list[KeyEnvelope]:
        # Atomic: only rows still unclaimed at UPDATE time are returned,
        # so two racing claimers can never receive the same envelope.
        rows = await self._pool.fetch(
            """
            WITH claimed AS (
                UPDATE key_envelopes
                SET claimed_at = now()
                WHERE to_device = $1 AND claimed_at IS NULL
                RETURNING id, to_device, from_device, sealed, created_at, claimed_at
            )
            SELECT * FROM claimed ORDER BY created_at
            """,
            _check_device_id(device_id),
        )
        return [
            KeyEnvelope(
                id=str(r["id"]),
                to_device=r["to_device"],
                from_device=r["from_device"],
                sealed=r["sealed"],
                created_at=r["created_at"],
                claimed_at=r["claimed_at"],
            )
            for r in rows
        ]


# ------------------------------------------------------------------ rotation


def rotate_plan(
    old_key_id: str,
    device_ids: list[str] | None = None,
) -> dict:
    """Build a key-rotation plan. Returns a description, performs nothing.

    Rotation is client-driven; the hub only relays sealed envelopes. The
    returned plan tells the owning device exactly what to do:

    1. Generate the new data key (``new_key``, 32 bytes — never sent to the
       hub in plaintext) and persist it locally, e.g. via
       ``porter_client.save_key(new_key_id, new_key)``.
    2. For every device in ``repost_to``: ``seal_key_for(new_key, pubkey)``
       and ``POST /api/v1/e2ee/envelopes`` with the sealed blob.
    3. Re-encrypt memories: for each memory encrypted under ``old_key_id``,
       ``decrypt_content`` with the old key and ``encrypt_content`` with the
       new one (plaintext exists only in client memory during this step).
    4. Retire ``old_key_id`` locally once every device has claimed its
       envelope (poll the envelopes endpoint, or track claims out-of-band).

    The server's only job in this flow is step 2's relay — it never sees
    ``new_key``, ``old_key_id``'s key material, or any plaintext.
    """
    if not old_key_id or not isinstance(old_key_id, str):
        raise InvalidInput("old_key_id must be a non-empty string")
    targets = [_check_device_id(d) for d in (device_ids or [])]
    new_key_id = f"key-{uuid.uuid4().hex[:12]}"
    return {
        "old_key_id": old_key_id,
        "new_key_id": new_key_id,
        "new_key": generate_data_key(),  # 32 bytes; client-held, never uploaded
        "repost_to": targets,  # devices needing a fresh sealed envelope
        "client_steps": [
            "save_key(new_key_id, new_key) locally (0600)",
            "seal new_key for each device in repost_to and POST sealed envelopes",
            "re-encrypt memories: decrypt_content(old) -> encrypt_content(new)",
            "retire old_key_id after all devices claim their envelopes",
        ],
    }
