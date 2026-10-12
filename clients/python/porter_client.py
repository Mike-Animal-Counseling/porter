#!/usr/bin/env python3
"""Porter sample client (REST).

Companion to docs/DEVELOPER.md. Demonstrates the OAuth authorization-code
flow, scoped memory search/add/update, E2EE helpers, and the P1 sync poll loop.

Dependencies (install once):
    pip install requests pynacl

Everything else is stdlib. Python 3.10+.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
from pathlib import Path

import requests
from nacl import bindings as _sodium

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class PorterError(Exception):
    """Raised for any non-2xx Porter response."""

    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(f"[{status}] {code}: {message}")


# --------------------------------------------------------------------------- #
# E2EE helpers (P2 contract; see docs/DEVELOPER.md §6)
# --------------------------------------------------------------------------- #

KEYS_DIR = Path.home() / ".porter" / "keys"
NONCE_SIZE = 24  # XChaCha20-Poly1305 nonce size
KEY_SIZE = 32


def generate_key() -> bytes:
    """Generate a fresh 32-byte XChaCha20-Poly1305 key."""
    return _sodium.randombytes(KEY_SIZE)


def save_key(key_id: str, key: bytes) -> Path:
    """Save a key to ~/.porter/keys/<key_id>.key with mode 0600.

    Key management is the client's job: Porter never sees these files.
    """
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    path = KEYS_DIR / f"{key_id}.key"
    # Open with restrictive mode from the start (no umask race window).
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                   "wb") as fh:
        fh.write(key)
    os.chmod(path, 0o600)  # belt and braces: enforce 0600
    return path


def load_key(key_id: str) -> bytes:
    """Load a key previously stored with save_key()."""
    path = KEYS_DIR / f"{key_id}.key"
    with open(path, "rb") as fh:
        key = fh.read()
    if len(key) != KEY_SIZE:
        raise PorterError(0, "bad_key", f"{path} is not a 32-byte key")
    return key


def encrypt_content(plaintext: str, key_id: str) -> tuple[str, dict]:
    """Encrypt memory content for E2EE upload.

    Returns (content_ciphertext_b64, encryption_dict) where the ciphertext is
    base64(nonce(24B) || XChaCha20-Poly1305(plaintext)).
    """
    key = load_key(key_id)
    nonce = _sodium.randombytes(NONCE_SIZE)
    ct = _sodium.crypto_aead_xchacha20poly1305_ietf_encrypt(
        plaintext.encode("utf-8"), None, nonce, key
    )
    b64 = base64.b64encode(nonce + ct).decode("ascii")
    return b64, {"alg": "xchacha20-poly1305", "key_id": key_id}


def decrypt_content(content_ciphertext: str, encryption: dict) -> str:
    """Reverse encrypt_content(). encryption = the dict returned alongside."""
    if encryption.get("alg") != "xchacha20-poly1305":
        raise PorterError(0, "unsupported_alg", f"alg={encryption.get('alg')!r}")
    key = load_key(encryption["key_id"])
    raw = base64.b64decode(content_ciphertext)
    nonce, ct = raw[:NONCE_SIZE], raw[NONCE_SIZE:]
    pt = _sodium.crypto_aead_xchacha20poly1305_ietf_decrypt(ct, None, nonce, key)
    return pt.decode("utf-8")


# --------------------------------------------------------------------------- #
# REST client
# --------------------------------------------------------------------------- #


class PorterClient:
    """Thin REST wrapper. `token` is an OAuth access token (Bearer)."""

    def __init__(self, base_url: str, token: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    # -- low-level ---------------------------------------------------------- #

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}/api/v1{path}"
        resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {})
                code, message = err.get("code", "unknown"), err.get("message", "")
            except ValueError:
                code, message = "unknown", resp.text[:200]
            raise PorterError(resp.status_code, code, message)
        if resp.status_code == 204:
            return None
        return resp.json()

    def _post(self, path: str, payload: dict):
        return self._request("POST", path, json=payload)

    def _get(self, path: str, params: dict | None = None):
        return self._request("GET", path, params=params)

    # -- memory API ---------------------------------------------------------- #

    def search(self, query: str, top_k: int = 5, tags: list[str] | None = None) -> list[dict]:
        """Semantic search over active memories. Tag filtering is enforced
        server-side from the token's scopes (fail-closed)."""
        body = {"query": query, "top_k": top_k, "tags": tags or []}
        return self._post("/memories/search", body)["results"]

    def add(
        self,
        content: str,
        type: str = "fact",
        scope_tags: list[str] | None = None,
        confidence: float = 1.0,
        source: dict | None = None,
    ) -> dict:
        """Submit a memory -> lands in the user's pending-review queue (202).
        Requires scope memory.write."""
        body = {
            "content": content,
            "type": type,
            "scope_tags": scope_tags or [],
            "confidence": confidence,
            "source": source or {},
        }
        return self._post("/memories", body)

    def update(self, memory_id: str, **fields) -> dict:
        """History-preserving edit (PATCH). Returns the NEW memory id
        (old version archived via superseded_by).

        NOTE: the REST endpoint requires scope memory.admin; the MCP
        `memory_update` tool requires only memory.write. See DEVELOPER.md §3.
        """
        if not fields:
            raise ValueError("nothing to update")
        return self._request("PATCH", f"/memories/{memory_id}", json=fields)

    def add_encrypted(
        self,
        plaintext: str,
        key_id: str,
        type: str = "fact",
        scope_tags: list[str] | None = None,
        confidence: float = 1.0,
        source: dict | None = None,
    ) -> dict:
        """E2EE write: encrypt client-side, post ciphertext only (P2 contract).

        The server stores content_ciphertext as-is — never embeds, indexes, or
        logs the plaintext. No semantic search/dedupe over encrypted memories.
        """
        ciphertext_b64, encryption = encrypt_content(plaintext, key_id)
        body = {
            "content_ciphertext": ciphertext_b64,  # never "content"
            "encryption": encryption,
            "type": type,
            "scope_tags": scope_tags or [],
            "confidence": confidence,
            "source": source or {},
        }
        return self._post("/memories", body)

    def decrypt_memory(self, memory: dict) -> str:
        """Decrypt a memory dict as returned by the server.

        Server shape (docs/DEVELOPER.md §6): {"content": <base64
        ciphertext>, "encrypted": True, "key_id": <id>}. The
        pre-implementation {"content_ciphertext": ..., "encryption": {...}}
        shape is also accepted.
        """
        if memory.get("encrypted"):
            ciphertext = memory["content"]
            encryption = {
                "alg": "xchacha20-poly1305", "key_id": memory["key_id"]}
        else:
            ciphertext = memory["content_ciphertext"]
            encryption = memory["encryption"]
        return decrypt_content(ciphertext, encryption)

    # -- sync (P1) ------------------------------------------------------------ #

    def get_delta(self, since: int = 0, limit: int = 100) -> dict:
        """GET /sync/delta?since=&limit= -> {"changes": [...], "cursor": int}.

        P1 auto-sync (live since the F9 release); v1-only servers answer
        501 sync_not_available (manual snapshots only).
        """
        return self._get("/sync/delta", {"since": since, "limit": limit})

    def ack_cursor(self, cursor: int) -> None:
        """POST /sync/cursor {"cursor": n} — persist our position server-side."""
        self._post("/sync/cursor", {"cursor": cursor})

    def poll_forever(self, interval: float = 60.0, on_change=None, cursor: int = 0,
                     state_file: str | None = None) -> None:
        """60-second sync poll loop. Calls on_change(change) for each change,
        then persists the cursor (local file + server ack)."""
        if state_file:
            try:
                cursor = int(Path(state_file).read_text().strip())
            except (OSError, ValueError):
                pass
        print(f"[porter] sync loop started (cursor={cursor}, interval={interval}s)")
        while True:
            try:
                delta = self.get_delta(since=cursor)
            except PorterError as exc:
                if exc.code == "sync_not_available":
                    print("[porter] sync not available yet (v1) — waiting")
                else:
                    print(f"[porter] delta error: {exc} — retrying")
                time.sleep(interval)
                continue
            changes = delta.get("changes", [])
            for change in changes:
                if on_change:
                    on_change(change)
                else:
                    print(f"[porter] change seq={change['seq']} "
                          f"op={change['op']} id={change['memory_id']}")
            if changes:
                cursor = int(delta["cursor"])
                self.ack_cursor(cursor)
                if state_file:
                    Path(state_file).write_text(str(cursor))
            time.sleep(interval)


# --------------------------------------------------------------------------- #
# OAuth helpers (used by the demo below)
# --------------------------------------------------------------------------- #


def register_client(base_url: str, name: str, redirect_uri: str,
                    setup_token: str) -> dict:
    """POST /api/v1/clients. Returns dict with client_id and client_secret
    (the secret is shown ONCE — store it)."""
    resp = requests.post(
        f"{base_url.rstrip('/')}/api/v1/clients",
        json={"name": name, "redirect_uris": [redirect_uri]},
        headers={"x-porter-setup-token": setup_token},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def authorize_url(base_url: str, client_id: str, redirect_uri: str,
                  scopes: list[str], state: str) -> str:
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
    })
    return f"{base_url.rstrip('/')}/oauth/authorize?{params}"


def exchange_code(base_url: str, code: str, redirect_uri: str,
                  client_id: str, client_secret: str) -> dict:
    """POST /oauth/token (grant_type=authorization_code)."""
    resp = requests.post(
        f"{base_url.rstrip('/')}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def refresh_access_token(base_url: str, refresh_token: str,
                         client_id: str, client_secret: str) -> dict:
    """POST /oauth/token (grant_type=refresh_token). Returns a ROTATED
    refresh_token — persist the new one; the old one is single-use."""
    resp = requests.post(
        f"{base_url.rstrip('/')}/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# --------------------------------------------------------------------------- #
# Demo: register -> authorize URL -> (paste code) -> search -> add -> poll once
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    BASE = os.environ.get("PORTER_BASE_URL", "http://localhost:8000")
    REDIRECT = os.environ.get("PORTER_REDIRECT_URI", "http://localhost:8765/callback")

    # 1. Register (first client needs the one-time setup token).
    setup = os.environ.get("PORTER_SETUP_TOKEN", "")
    if not setup:
        raise SystemExit("Set PORTER_SETUP_TOKEN (see Porter startup logs).")
    reg = register_client(BASE, "porter-sample-py", REDIRECT, setup)
    cid, csecret = reg["client_id"], reg["client_secret"]
    print(f"client_id={cid}\nclient_secret={csecret}  # shown once — keep it!\n")

    # 2. Print the consent URL; the user approves scopes in the browser.
    state = base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("=")
    scopes = ["memory.read.dietary", "memory.write"]
    print("Open this URL, approve, then paste the ?code= value:")
    print(authorize_url(BASE, cid, REDIRECT, scopes, state) + "\n")

    code = os.environ.get("PORTER_AUTH_CODE") or input("auth code: ").strip()

    # 3. Exchange code -> tokens.
    toks = exchange_code(BASE, code, REDIRECT, cid, csecret)
    print(f"access_token expires in {toks['expires_in']}s; scope={toks['scope']}")
    client = PorterClient(BASE, toks["access_token"])

    # 4. Scoped search (server enforces memory.read.dietary tag filter).
    print("\n-- search --")
    for r in client.search("what do I avoid eating", top_k=3, tags=["dietary"]):
        print(f"[{r['score']:.2f}] {r['content'][:80]} tags={r['scope_tags']}")

    # 5. Write -> pending-review queue.
    print("\n-- add --")
    created = client.add(
        "User prefers pnpm over npm",
        type="preference",
        scope_tags=["coding_style"],
        confidence=0.9,
        source={"client_id": "porter-sample-py"},
    )
    print(json.dumps(created, ensure_ascii=False, indent=2))

    # 6. One sync poll (P1 auto-sync; v1-only servers answer 501).
    print("\n-- poll once --")
    try:
        delta = client.get_delta(since=0)
        print(f"changes={len(delta['changes'])} cursor={delta['cursor']}")
    except PorterError as exc:
        print(f"(expected on v1-only servers) {exc}")

    print("\nDone. For the continuous loop: client.poll_forever(interval=60, on_change=...)")
