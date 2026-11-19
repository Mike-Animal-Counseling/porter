"""System-held credentials for Porter’s per-project native connections."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import httpx2


class ConnectionError(ValueError):
    """An actionable connection failure, safe to print without secret data."""


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str
    refresh_token: str
    access_token: str
    expires_at: float
    scope: str

    def client_form(self) -> dict[str, str]:
        return {"client_id": self.client_id, "client_secret": self.client_secret}


def _wincred(action: str, key: str, value: str = "") -> str | None:
    try:
        import pywintypes
        import win32cred
    except ImportError:
        raise ConnectionError("Windows Credential Manager support is unavailable") from None
    kind = win32cred.CRED_TYPE_GENERIC
    target = "Porter/AutomaticMemory/" + key
    try:
        if action == "get":
            blob = win32cred.CredRead(target, kind)["CredentialBlob"]
            return blob.decode("utf-16-le") if isinstance(blob, bytes) else blob
        if action == "delete":
            win32cred.CredDelete(target, kind)
        else:
            win32cred.CredWrite(
                {
                    "Type": kind,
                    "TargetName": target,
                    "UserName": "Porter",
                    "CredentialBlob": value,
                    "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
                },
                0,
            )
    except pywintypes.error as exc:
        if exc.winerror != 1168 or action == "set":
            raise ConnectionError(
                "Windows Credential Manager could not complete the request"
            ) from None
    return None


def _secret_service(action: str, key: str, value: str = "") -> str | None:
    operation = {"get": ["lookup"], "delete": ["clear"], "set": ["store", "--label=Porter"]}
    try:
        result = subprocess.run(
            ["secret-tool", *operation[action], "application", "porter", "connection", key],
            input=value,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=0.4,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ConnectionError("Linux Secret Service is unavailable or locked") from None
    if result.returncode:
        if action != "set" and not result.stderr.strip():
            return None
        raise ConnectionError("Linux Secret Service could not complete the request")
    return result.stdout.strip() or None


def _vault(action: str, key: str, value: str = "") -> str | None:
    if sys.platform == "win32":
        return _wincred(action, key, value)
    if sys.platform == "linux":
        return _secret_service(action, key, value)
    raise ConnectionError("Managed credentials require Windows or Linux Secret Service")


def get_credentials(key: str) -> Credentials:
    value = _vault("get", key)
    if value is None:
        raise ConnectionError("No system credential found. Connect the project again.")
    try:
        return Credentials(**json.loads(value))
    except (ValueError, TypeError):
        raise ConnectionError(
            "The system credential is invalid. Connect the project again."
        ) from None


def save_credentials(key: str, credentials: Credentials) -> None:
    _vault("set", key, json.dumps(asdict(credentials), separators=(",", ":")))


def delete_credentials(key: str) -> None:
    _vault("delete", key)


def credential_key(config: Path, client: str, root: Path, origin: str, client_id: str) -> str:
    identity = json.dumps([str(config), client, str(root), origin, client_id]).encode()
    return hashlib.sha256(identity).hexdigest()


@contextmanager
def local_lock(config: Path, *, timeout: float = 0.1):
    # The database stores no data. A process exit releases the lock, including
    # when a hook is cancelled while an OAuth refresh is in progress.
    with closing(sqlite3.connect(config.with_suffix(".lock.sqlite3"), timeout=timeout)) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            yield


async def exchange(http, base_url: str, form: dict[str, str], expected_scopes: set[str]):
    response = await http.post(base_url + "/oauth/token", data=form)
    if response.status_code in {400, 401, 403}:
        raise ConnectionError("Porter rejected the OAuth grant. Check consent or reconnect.")
    response.raise_for_status()
    data = response.json()
    if set(data["scope"].split()) != expected_scopes:
        raise ConnectionError(
            "Porter granted a different scope. Approve exactly the requested scopes."
        )
    return Credentials(
        client_id=form["client_id"],
        client_secret=form["client_secret"],
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_at=time.time() + int(data["expires_in"]),
        scope=data["scope"],
    )


async def access_token(http, config: Path, key: str, base_url: str, *, renew=False) -> str:
    pair = get_credentials(key)
    if not renew and pair.expires_at > time.time() + 5:
        return pair.access_token
    with local_lock(config):
        pair = get_credentials(key)
        if not renew and pair.expires_at > time.time() + 5:
            return pair.access_token
        if not pair.refresh_token:
            raise ConnectionError(
                "OAuth renewal was interrupted. Disconnect and reconnect the project."
            )
        # Mark the refresh consumed before sending it. If the server rotates it
        # and loses the response, replaying it could revoke the entire client.
        save_credentials(key, replace(pair, refresh_token=""))
        try:
            updated = await exchange(
                http,
                base_url,
                pair.client_form()
                | {"grant_type": "refresh_token", "refresh_token": pair.refresh_token},
                set(pair.scope.split()),
            )
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout):
            # The request never reached Porter, so the refresh token is safe to keep.
            save_credentials(key, pair)
            raise
        save_credentials(key, updated)
        return updated.access_token
