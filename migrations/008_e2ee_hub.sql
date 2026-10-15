-- Porter migration 008: P2 E2EE blind-hub key distribution
--
-- Device public keys + sealed key envelopes for moving 32-byte data keys
-- between a user's own devices without the hub ever learning them.
--
-- Blind-hub invariant (see porter/e2ee_hub.py): `key_envelopes.sealed` holds
-- NaCl crypto_box_seal blobs addressed to a recipient device's X25519 public
-- key. The hub stores/forwards these opaque blobs only; opening one requires
-- the recipient's private key, which never leaves the device. There is no
-- column for plaintext keys by construction — the server must never accept
-- one.
--
-- `devices`: one row per user device. Registering the same device_id twice
-- rotates its public key (upsert); the old private key simply stops being
-- able to open newly sealed envelopes.
-- `key_envelopes`: sealed blobs in transit. `claimed_at` is set when the
-- recipient device fetches them (GET /api/v1/e2ee/envelopes); the claiming
-- UPDATE is atomic (claimed_at IS NULL) so retries never double-deliver.

-- ------------------------------------------------------------------- devices
CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    public_key  TEXT NOT NULL,                        -- base64 X25519 public key
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------ key_envelopes
CREATE TABLE IF NOT EXISTS key_envelopes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    to_device   TEXT NOT NULL,                        -- devices.device_id (no FK: the hub
                                                     -- is a dumb relay; registration order
                                                     -- must not gate envelope delivery)
    from_device TEXT NOT NULL,
    sealed      TEXT NOT NULL,                        -- base64 sealed box; opaque to hub
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS key_envelopes_to_unclaimed_idx
    ON key_envelopes (to_device, created_at)
    WHERE claimed_at IS NULL;
