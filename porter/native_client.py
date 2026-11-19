"""Manage Porter connections and user-level Codex or Claude Code hooks."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import secrets
import shlex
import sqlite3
import sys
import tempfile
import time
from getpass import GetPassWarning, getpass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from warnings import catch_warnings, simplefilter

import httpx2

from . import native_credentials as vault
from .auto_memory import _TAG, _base_url

CLIENTS = ("codex", "claude")
EVENTS = {"UserPromptSubmit": 5, "Stop": 10}
MARKER = "--porter-managed"
CALLBACK = "/porter/callback"
_PS = "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand "
_CMD_SAFE = re.compile(r"[A-Za-z0-9:/.\-_]+")


def _absolute(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
    return _absolute(base / "porter" / "native-connections.json")


def _hooks_path(client: str) -> Path:
    if client == "codex":
        return _absolute(Path(os.getenv("CODEX_HOME", Path.home() / ".codex")) / "hooks.json")
    return _absolute(
        Path(os.getenv("CLAUDE_CONFIG_DIR", Path.home() / ".claude")) / "settings.json"
    )


def _read(path: Path) -> tuple[dict, bytes | None]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, None
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise vault.ConnectionError("Expected a JSON object in the selected configuration")
    return value, raw


def _save(path: Path, value: dict, original: bytes | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".porter-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = None
        if current != original:
            raise vault.ConnectionError("The configuration changed during this command. Try again.")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _projects(path: Path) -> tuple[dict, list, bytes | None]:
    settings, raw = _read(path)
    projects = settings.setdefault("projects", [])
    if not isinstance(projects, list) or any(not isinstance(item, dict) for item in projects):
        raise vault.ConnectionError("Expected a projects list in the selected configuration")
    return settings, projects, raw


def _entry(projects: list, client: str, root: Path) -> dict | None:
    matches = [
        item for item in projects if item.get("client") == client and Path(item["root"]) == root
    ]
    if len(matches) > 1:
        raise vault.ConnectionError("Multiple managed connections match this client and project")
    return matches[0] if matches else None


def _hook(client: str, config: Path, timeout: int) -> dict:
    executable = Path(sys.executable).resolve()
    args = ["-I", "-m", "porter.auto_memory", client, "--config", str(config), MARKER]
    if client == "claude":
        return {"type": "command", "command": str(executable), "args": args, "timeout": timeout}
    if sys.platform == "win32":
        paths = [executable.as_posix(), config.as_posix()]
        if all(_CMD_SAFE.fullmatch(path) for path in paths):
            command = f"{paths[0]} -I -m porter.auto_memory {client} --config {paths[1]} {MARKER}"
        else:
            # The shell may be cmd.exe or PowerShell. A command starting with
            # a quoted path has different execution semantics under each.
            script = f"& {_ps_quote(paths[0])}{_ps_hook_tail(client, paths[1])}"
            command = _PS + base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    else:
        command = shlex.join([str(executable), *args])
    return {"type": "command", "command": command, "timeout": timeout}


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ps_hook_tail(client: str, path: str) -> str:
    return f" -I -m porter.auto_memory {client} --config {_ps_quote(path)} {MARKER}\nexit $LASTEXITCODE"


def _owned(hook: object, client: str, config: Path) -> bool:
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return False
    args = hook.get("args")
    target = ["-I", "-m", "porter.auto_memory", client, "--config", str(config), MARKER]
    if client == "claude":
        return args == target
    command = hook.get("command")
    if not isinstance(command, str):
        return False
    if sys.platform == "win32":
        path = config.as_posix()
        if command.startswith(_PS):
            try:
                script = base64.b64decode(command.removeprefix(_PS), validate=True).decode(
                    "utf-16-le"
                )
            except ValueError:
                return False
            return script.startswith("& '") and script.endswith(_ps_hook_tail(client, path))
        prefix = f" -I -m porter.auto_memory {client} --config "
        return command.endswith(prefix + path + " " + MARKER) or command.endswith(
            prefix + '"' + path + '" ' + MARKER
        )
    if not command.endswith(" " + MARKER):
        return False
    return shlex.split(command)[1:] == target


def _edit_hooks(path: Path, client: str, config: Path, *, install: bool) -> bool:
    settings, raw = _read(path)
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        raise vault.ConnectionError("Expected a hooks object in the client configuration")
    changed = False
    for event, timeout in EVENTS.items():
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise vault.ConnectionError("Expected a hook group list in the client configuration")
        remaining = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise vault.ConnectionError("Expected existing hook groups to contain a hooks list")
            handlers = [item for item in group["hooks"] if not _owned(item, client, config)]
            if len(handlers) == len(group["hooks"]):
                remaining.append(group)
            elif handlers:
                remaining.append(group | {"hooks": handlers})
        if install:
            remaining.append({"hooks": [_hook(client, config, timeout)]})
        if remaining != groups:
            changed = True
            if remaining:
                hooks[event] = remaining
            else:
                hooks.pop(event, None)
    if changed:
        settings["hooks"] = hooks
        _save(path, settings, raw)
    return changed


def _has_hooks(path: Path, client: str, config: Path) -> bool:
    settings, _ = _read(path)
    return all(
        any(
            _owned(hook, client, config)
            for group in settings.get("hooks", {}).get(event, [])
            for hook in group.get("hooks", [])
        )
        for event in EVENTS
    )


def _authorization_code(
    origin: str, client_id: str, port: int, scopes: set[str]
) -> tuple[str, str]:
    state = secrets.token_urlsafe(32)
    redirect = f"http://127.0.0.1:{port}{CALLBACK}"
    code: str | None = None
    denied = False

    class CallbackHandler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(2)
            super().setup()

        def log_message(self, *_):
            pass  # The callback query contains a one-time authorization code.

        def do_GET(self):
            nonlocal code, denied
            target = urlsplit(self.path)
            params = parse_qs(target.query)
            if (
                self.headers.get("Host") != f"127.0.0.1:{port}"
                or target.path != CALLBACK
                or params.get("state") != [state]
            ):
                self.send_error(400, "Invalid Porter callback")
                return
            values = params.get("code", [])
            code = values[0] if len(values) == 1 else None
            denied = code is None
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(b"Return to the Porter terminal to finish connecting.")

    with HTTPServer(("127.0.0.1", port), CallbackHandler) as listener:
        listener.timeout = 0.25
        url = (
            origin
            + "/oauth/authorize?"
            + urlencode(
                {
                    "client_id": client_id,
                    "redirect_uri": redirect,
                    "scope": " ".join(sorted(scopes)),
                    "state": state,
                    "response_type": "code",
                }
            )
        )
        print("Open this URL in a browser with an existing Porter owner session:", flush=True)
        print(url, flush=True)
        deadline = time.monotonic() + 180
        while code is None and not denied and time.monotonic() < deadline:
            listener.handle_request()
    if code is None:
        raise vault.ConnectionError("OAuth consent was declined or no valid callback arrived")
    return code, redirect


async def _connect(args, config: Path, root: Path, http) -> None:
    if not (args.recall or args.capture):
        raise vault.ConnectionError("Select --recall, --capture, or both")
    if not _TAG.fullmatch(args.tag) or not root.is_dir() or not 1024 <= args.callback_port <= 65535:
        raise vault.ConnectionError(
            "Specify an existing project directory, valid tag and callback port"
        )
    origin = _base_url(args.url)
    key = vault.credential_key(config, args.client, root, origin, args.client_id)
    _, projects, _ = _projects(config)
    if _entry(projects, args.client, root):
        raise vault.ConnectionError(
            "Disconnect the existing project connection before replacing it"
        )
    if any(
        item.get("client_id") == args.client_id and item.get("base_url") == origin
        for item in projects
    ):
        raise vault.ConnectionError(
            "Register a dedicated OAuth client for each project and harness"
        )
    scopes = {f"memory.read.{args.tag}"}
    if args.capture:
        scopes.add(f"memory.capture.{args.tag}")
    try:
        with catch_warnings():
            simplefilter("error", GetPassWarning)
            secret = getpass("Porter OAuth client secret: ")
    except (GetPassWarning, EOFError):
        raise vault.ConnectionError(
            "Run connect in a terminal that supports hidden input"
        ) from None
    if not secret:
        raise vault.ConnectionError("Enter the OAuth client secret from Porter")
    code, redirect = _authorization_code(origin, args.client_id, args.callback_port, scopes)
    pair = await vault.exchange(
        http,
        origin,
        {
            "grant_type": "authorization_code",
            "client_id": args.client_id,
            "client_secret": secret,
            "code": code,
            "redirect_uri": redirect,
        },
        scopes,
    )
    config.parent.mkdir(parents=True, exist_ok=True)
    with vault.local_lock(config, timeout=2):
        settings, projects, raw = _projects(config)
        if _entry(projects, args.client, root) or any(
            item.get("client_id") == args.client_id and item.get("base_url") == origin
            for item in projects
        ):
            raise vault.ConnectionError(
                "The project or OAuth client was connected in another process"
            )
        vault.save_credentials(key, pair)
        projects.append(
            {
                "root": str(root),
                "client": args.client,
                "client_id": args.client_id,
                "base_url": origin,
                "tag": args.tag,
                "credential_key": key,
                "recall": args.recall,
                "capture": args.capture,
            }
        )
        try:
            _save(config, settings, raw)
        except (OSError, ValueError):
            vault.delete_credentials(key)
            raise
    print("Project connected. Install the hooks and run doctor before starting a fresh session.")


def _bound_key(config: Path, item: dict, origin: str) -> str:
    key = vault.credential_key(
        config, item["client"], Path(item["root"]), origin, item["client_id"]
    )
    if key != item["credential_key"]:
        raise vault.ConnectionError("The connection metadata changed. Reconnect this project.")
    return key


async def _doctor(args, config: Path, path: Path, item: dict, http) -> None:
    installed = _has_hooks(path, args.client, config)
    print(f"Hooks: {'installed' if installed else 'missing'}")
    print(
        f"Capture: {bool(item.get('capture'))}. Recall: {bool(item.get('recall'))}. "
        f"Paused: {bool(item.get('paused') or os.getenv('PORTER_AUTO_MEMORY_PAUSE') == '1')}."
    )
    origin = _base_url(item["base_url"])
    health = await http.get(origin + "/healthz")
    health.raise_for_status()
    print("Porter is reachable.")
    token = await vault.access_token(
        http, config, _bound_key(config, item, origin), origin, renew=args.renew
    )
    response = await http.get(
        origin + "/api/v1/memories",
        params={"tag": item["tag"], "limit": 1},
        headers={"Authorization": "Bearer " + token},
    )
    response.raise_for_status()
    print(
        "Scoped read succeeded."
        if not args.renew
        else "Credential renewed and scoped read succeeded."
    )
    if not installed:
        raise vault.ConnectionError(
            "Install both Porter hooks in the selected client configuration"
        )


async def _run(args, config: Path, path: Path) -> None:
    if args.action in {"install", "uninstall"}:
        _, projects, _ = _projects(config)
        if args.action == "uninstall" and any(
            item.get("client") == args.client for item in projects
        ):
            raise vault.ConnectionError(
                "Disconnect this harness's managed projects before uninstalling"
            )
        _edit_hooks(path, args.client, config, install=args.action == "install")
        print("Porter hooks installed." if args.action == "install" else "Porter hooks removed.")
        if args.action == "install" and args.client == "codex":
            print("Review and trust both Porter hooks with /hooks in Codex.")
        return
    root = _absolute(args.root)
    async with httpx2.AsyncClient(trust_env=False, follow_redirects=False, timeout=4) as http:
        if args.action == "connect":
            await _connect(args, config, root, http)
            return
        _, projects, _ = _projects(config)
        item = _entry(projects, args.client, root)
        if item is None:
            raise vault.ConnectionError(
                "This project has no managed connection for the selected harness"
            )
        if args.action == "doctor":
            await _doctor(args, config, path, item, http)
            return
        with vault.local_lock(config, timeout=2):
            settings, projects, raw = _projects(config)
            item = _entry(projects, args.client, root)
            if item is None:
                raise vault.ConnectionError("The project was already disconnected")
            if args.action == "disconnect":
                origin = _base_url(item["base_url"])
                key = _bound_key(config, item, origin)
                if not args.local_only:
                    pair = vault.get_credentials(key)
                    response = await http.post(
                        origin + "/oauth/revoke",
                        data=pair.client_form()
                        | {"token": pair.access_token, "revoke_all": "true"},
                    )
                    response.raise_for_status()
                    if response.json().get("revoked") is not True:
                        raise vault.ConnectionError("Server revocation could not be confirmed")
                vault.delete_credentials(key)
                projects.remove(item)
            else:
                item["paused"] = args.action == "pause"
            _save(config, settings, raw)
        if args.action == "disconnect":
            print("Project disconnected.")
            if args.local_only:
                print("Revoke this OAuth client in the Porter owner panel when it is reachable.")
        else:
            print("Project paused." if args.action == "pause" else "Project resumed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=_absolute, default=_config_path())
    parser.add_argument("--hooks-file", type=_absolute)
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("install", "connect", "doctor", "pause", "resume", "disconnect", "uninstall"):
        action = actions.add_parser(name)
        action.add_argument("client", choices=CLIENTS)
        if name not in {"install", "uninstall"}:
            action.add_argument("root")
        if name == "connect":
            action.add_argument("--url", required=True)
            action.add_argument("--tag", required=True)
            action.add_argument("--client-id", required=True)
            action.add_argument("--callback-port", type=int, default=8766)
            action.add_argument("--recall", action="store_true")
            action.add_argument("--capture", action="store_true")
        if name == "doctor":
            action.add_argument("--renew", action="store_true")
        if name == "disconnect":
            action.add_argument("--local-only", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(_run(args, args.config, args.hooks_file or _hooks_path(args.client)))
    except vault.ConnectionError as exc:
        print(f"Porter: {exc}", file=sys.stderr)
    except httpx2.HTTPStatusError as exc:
        print(
            f"Porter returned HTTP {exc.response.status_code}. Check the connection and grant.",
            file=sys.stderr,
        )
    except (TimeoutError, httpx2.RequestError):
        print(
            "Porter could not be reached. Check the server URL and availability.", file=sys.stderr
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error):
        print(
            "Porter could not read or update the selected configuration or local resources.",
            file=sys.stderr,
        )
    else:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
