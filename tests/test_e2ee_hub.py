"""P2: E2EE blind-hub key distribution (porter/e2ee_hub.py).

Covers, with no network and no Postgres (InMemoryEnvelopeStore):
- device keypair generation (X25519; private key never leaves the device)
- seal/open round-trip: A seals for B, only B opens, C cannot
- input validation (bad key sizes, bad base64)
- envelope store: register/get/list devices, post/claim envelopes,
  claim idempotency (second claim returns [] — no double delivery)
- rotate_plan structure and the client/server boundary
"""
from __future__ import annotations

import asyncio
import base64

import pytest
from nacl.exceptions import CryptoError

from porter.e2ee_hub import (
    DATA_KEY_SIZE,
    EnvelopeStore,
    InMemoryEnvelopeStore,
    PostgresEnvelopeStore,
    generate_data_key,
    generate_device_keypair,
    open_sealed_key,
    rotate_plan,
    seal_key_for,
)
from porter.errors import InvalidInput


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------- device keypairs


def test_keypair_generation():
    priv_b64, pub_b64 = generate_device_keypair()
    priv = base64.b64decode(priv_b64)
    pub = base64.b64decode(pub_b64)
    assert len(priv) == 32
    assert len(pub) == 32
    assert priv != pub  # private key material differs from the public key


def test_keypair_generation_is_random():
    priv1, pub1 = generate_device_keypair()
    priv2, pub2 = generate_device_keypair()
    assert priv1 != priv2
    assert pub1 != pub2


def test_generate_data_key_is_32_bytes():
    key = generate_data_key()
    assert isinstance(key, bytes)
    assert len(key) == DATA_KEY_SIZE == 32
    assert generate_data_key() != key


# ------------------------------------------------------------ seal and open


def _devices(n=3):
    return [generate_device_keypair() for _ in range(n)]


def test_seal_open_round_trip():
    (a_priv, _), (b_priv, b_pub), _ = _devices()
    data_key = generate_data_key()
    sealed = seal_key_for(data_key, b_pub)
    assert isinstance(sealed, str)
    assert base64.b64decode(sealed)  # valid base64
    opened = open_sealed_key(sealed, b_priv)
    assert opened == data_key
    assert a_priv  # A sealed it but A's private key is irrelevant to opening


def test_sealed_box_is_opaque_ciphertext():
    _, (_, b_pub) = _devices()[:2]
    data_key = generate_data_key()
    sealed = seal_key_for(data_key, b_pub)
    raw = base64.b64decode(sealed)
    assert data_key not in raw  # the plaintext key never appears in the blob
    assert len(raw) > len(data_key)  # sealed box adds ephemeral key + MAC overhead


def test_third_device_cannot_open():
    (_, _), (b_priv, b_pub), (c_priv, _) = _devices()
    sealed = seal_key_for(generate_data_key(), b_pub)
    # B (the intended recipient) opens fine...
    assert open_sealed_key(sealed, b_priv)
    # ...but C, holding a different private key, cannot.
    with pytest.raises(CryptoError):
        open_sealed_key(sealed, c_priv)


def test_tampered_sealed_box_rejected():
    (_, _), (b_priv, b_pub) = _devices()[:2]
    sealed = seal_key_for(generate_data_key(), b_pub)
    raw = bytearray(base64.b64decode(sealed))
    raw[-1] ^= 0x01  # flip one bit of the MAC/ciphertext
    with pytest.raises(CryptoError):
        open_sealed_key(base64.b64encode(bytes(raw)).decode(), b_priv)


def test_seal_rejects_wrong_key_sizes():
    _, (_, b_pub) = _devices()[:2]
    with pytest.raises(InvalidInput):
        seal_key_for(b"too-short", b_pub)
    with pytest.raises(InvalidInput):
        seal_key_for(b"x" * 64, b_pub)


def test_seal_rejects_bad_recipient_key():
    with pytest.raises(InvalidInput):
        seal_key_for(generate_data_key(), "not-base64!!")
    with pytest.raises(InvalidInput):
        seal_key_for(generate_data_key(), base64.b64encode(b"short").decode())


def test_open_rejects_bad_inputs():
    (b_priv, b_pub) = _devices()[1]
    sealed = seal_key_for(generate_data_key(), b_pub)
    with pytest.raises(InvalidInput):
        open_sealed_key("not-base64!!", b_priv)
    with pytest.raises(InvalidInput):
        open_sealed_key(sealed, base64.b64encode(b"short").decode())


# ------------------------------------------------------------ envelope store


def test_store_is_envelope_store_subclass():
    assert isinstance(InMemoryEnvelopeStore(), EnvelopeStore)
    assert isinstance(PostgresEnvelopeStore(pool=None), EnvelopeStore)


def test_register_get_list_devices():
    async def _run():
        store = InMemoryEnvelopeStore()
        _, pub_a = generate_device_keypair()
        _, pub_b = generate_device_keypair()
        ra = await store.register_device("laptop", pub_a)
        assert ra.device_id == "laptop"
        assert ra.public_key == pub_a
        assert await store.get_device("laptop") == ra
        assert await store.get_device("unknown") is None
        await store.register_device("phone", pub_b)
        ids = {d.device_id for d in await store.list_devices()}
        assert ids == {"laptop", "phone"}

    run(_run())


def test_register_device_rejects_bad_key():
    async def _run():
        store = InMemoryEnvelopeStore()
        with pytest.raises(InvalidInput):
            await store.register_device("laptop", "garbage!!")
        with pytest.raises(InvalidInput):
            await store.register_device("", generate_device_keypair()[1])

    run(_run())


def test_post_and_claim_envelopes():
    async def _run():
        store = InMemoryEnvelopeStore()
        (a_priv, a_pub), (b_priv, b_pub) = _devices()[:2]
        await store.register_device("A", a_pub)
        await store.register_device("B", b_pub)

        # A shares a data key with B through the hub.
        data_key = generate_data_key()
        sealed = seal_key_for(data_key, b_pub)
        env = await store.post_envelope("B", "A", sealed)
        assert env.to_device == "B"
        assert env.from_device == "A"
        assert env.claimed_at is None

        claimed = await store.claim_envelopes("B")
        assert len(claimed) == 1
        assert claimed[0].id == env.id
        assert claimed[0].claimed_at is not None
        # B opens it with its private key: end-to-end key flow works.
        assert open_sealed_key(claimed[0].sealed, b_priv) == data_key
        # A cannot open B's envelope even though A created it.
        with pytest.raises(CryptoError):
            open_sealed_key(claimed[0].sealed, a_priv)

    run(_run())


def test_claim_is_idempotent():
    async def _run():
        store = InMemoryEnvelopeStore()
        _, (_, b_pub) = _devices()[:2]
        await store.post_envelope("B", "A", seal_key_for(generate_data_key(), b_pub))
        first = await store.claim_envelopes("B")
        assert len(first) == 1
        # Retries must never double-deliver.
        assert await store.claim_envelopes("B") == []
        assert await store.claim_envelopes("B") == []
        # A new envelope for B is still delivered.
        await store.post_envelope("B", "A", seal_key_for(generate_data_key(), b_pub))
        assert len(await store.claim_envelopes("B")) == 1

    run(_run())


def test_claim_is_per_device():
    async def _run():
        store = InMemoryEnvelopeStore()
        _, (_, b_pub) = _devices()[:2]
        sealed = seal_key_for(generate_data_key(), b_pub)
        await store.post_envelope("B", "A", sealed)
        assert await store.claim_envelopes("A") == []  # not addressed to A
        assert len(await store.claim_envelopes("B")) == 1

    run(_run())


def test_claim_ordering_oldest_first():
    async def _run():
        store = InMemoryEnvelopeStore()
        _, (_, b_pub) = _devices()[:2]
        e1 = await store.post_envelope("B", "A", seal_key_for(generate_data_key(), b_pub))
        e2 = await store.post_envelope("B", "A", seal_key_for(generate_data_key(), b_pub))
        claimed = await store.claim_envelopes("B")
        assert [e.id for e in claimed] == [e1.id, e2.id]

    run(_run())


# ----------------------------------------------------------------- rotation


def test_rotate_plan_structure():
    plan = rotate_plan("key-old123", ["laptop", "phone"])
    assert plan["old_key_id"] == "key-old123"
    assert plan["new_key_id"] != "key-old123"
    assert plan["new_key_id"].startswith("key-")
    assert isinstance(plan["new_key"], bytes)
    assert len(plan["new_key"]) == DATA_KEY_SIZE
    assert plan["repost_to"] == ["laptop", "phone"]
    assert isinstance(plan["client_steps"], list) and plan["client_steps"]
    # The plan is a description: it must not contain any plaintext old key.
    assert "old_key" not in str(plan["new_key"])


def test_rotate_plan_defaults_to_no_devices():
    plan = rotate_plan("key-old123")
    assert plan["repost_to"] == []


def test_rotate_plan_rejects_bad_input():
    with pytest.raises(InvalidInput):
        rotate_plan("")
    with pytest.raises(InvalidInput):
        rotate_plan("key-ok", [""])


def test_rotated_key_flows_through_sealed_envelopes():
    # Full client-side rotation flow: new key sealed per device, each device
    # opens it, hub never sees plaintext.
    async def _run():
        store = InMemoryEnvelopeStore()
        privs, pubs = {}, {}
        for name in ("laptop", "phone"):
            priv, pub = generate_device_keypair()
            privs[name], pubs[name] = priv, pub
            await store.register_device(name, pub)
        plan = rotate_plan("key-old", ["laptop", "phone"])
        for name in plan["repost_to"]:
            sealed = seal_key_for(plan["new_key"], pubs[name])
            await store.post_envelope(name, "laptop", sealed)
        for name in ("laptop", "phone"):
            claimed = await store.claim_envelopes(name)
            assert len(claimed) == 1
            assert open_sealed_key(claimed[0].sealed, privs[name]) == plan["new_key"]

    run(_run())
