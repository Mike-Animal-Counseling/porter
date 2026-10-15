"""Tests for porter.teams (team workspaces). Uses InMemoryTeamStore."""
import asyncio
import re
from uuid import UUID, uuid4

import jwt
import pytest

from porter import teams
from porter.embeddings import HashEmbedder
from porter.errors import Conflict, InvalidInput
from porter.errors import NotFound
from porter.oauth import jwt_signing_key
from porter.oauth_store import InMemoryOAuthStore
from porter.repositories import InMemoryMemoryRepository
from porter.teams import (
    InMemoryTeamStore,
    check_access,
    create_workspace,
    invite,
    is_workspace_tag,
    mint_member_token,
    validate_workspace_memory,
    workspace_tag,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def store() -> InMemoryTeamStore:
    return InMemoryTeamStore()


@pytest.fixture()
def repo() -> InMemoryMemoryRepository:
    return InMemoryMemoryRepository(HashEmbedder())


@pytest.fixture()
def oauth_store() -> InMemoryOAuthStore:
    return InMemoryOAuthStore()


def _make_ws(store, repo, name="acme", owner=None):
    owner = owner or uuid4()
    ws = _run(
        create_workspace(store, repo, name=name, owner_client_id=owner)
    )
    return ws, owner


# ---------------------------------------------------------------- tag helpers
def test_workspace_tag_format_and_determinism():
    wid = uuid4()
    tag = workspace_tag(wid)
    assert re.fullmatch(r"workspace:[0-9a-f]{8}", tag), tag
    assert workspace_tag(wid) == tag  # pure: same input, same output
    assert workspace_tag(uuid4()) != tag  # overwhelmingly likely


def test_is_workspace_tag():
    assert is_workspace_tag("workspace:a3f9c2e1")
    assert not is_workspace_tag("workspace:notes")     # not 8 hex
    assert not is_workspace_tag("project:acme")
    assert not is_workspace_tag("")
    assert not is_workspace_tag("workspace:A3F9C2E1")  # strict lowercase


# ---------------------------------------------------------------- create
def test_create_workspace(store, repo):
    owner = uuid4()
    ws = _run(
        create_workspace(store, repo, name="acme", owner_client_id=owner,
                         project="acme/api")
    )
    assert re.fullmatch(r"workspace:[0-9a-f]{8}", ws["project_tag"])
    assert ws["name"] == "acme"
    assert ws["owner_client_id"] == str(owner)

    # owner membership seeded
    member = _run(store.get_member(UUID(ws["id"]), owner))
    assert member is not None and member.role == "owner"

    # audited
    actions = [a.action for a in _run(repo.audit_list())]
    assert "workspace_create" in actions
    rec = next(a for a in _run(repo.audit_list())
               if a.action == "workspace_create")
    assert rec.meta["project_tag"] == ws["project_tag"]
    assert rec.meta["project"] == "acme/api"
    assert rec.client_id == owner


def test_create_workspace_empty_name(store, repo):
    with pytest.raises(InvalidInput):
        _run(create_workspace(store, repo, name="  ", owner_client_id=uuid4()))


def test_get_workspace_by_tag(store, repo):
    ws, _ = _make_ws(store, repo)
    found = _run(store.get_workspace_by_tag(ws["project_tag"]))
    assert found is not None and str(found.id) == ws["id"]
    assert _run(store.get_workspace_by_tag("workspace:deadbeef")) is None


def test_list_workspaces_for_client(store, repo):
    alice, bob = uuid4(), uuid4()
    ws1, _ = _make_ws(store, repo, name="one", owner=alice)
    ws2, _ = _make_ws(store, repo, name="two", owner=bob)
    _run(invite(store, repo, workspace_id=UUID(ws2["id"]),
                client_id=alice, role="viewer", actor_client_id=bob))
    names = {w.name for w in _run(store.list_workspaces_for_client(alice))}
    assert names == {"one", "two"}
    assert {w.name for w in _run(store.list_workspaces_for_client(bob))} == {"two"}


# ---------------------------------------------------------------- invite
def test_invite_owner_only(store, repo):
    ws, owner = _make_ws(store, repo)
    wid = UUID(ws["id"])
    editor, stranger = uuid4(), uuid4()

    # owner invites an editor: fine
    m = _run(invite(store, repo, workspace_id=wid, client_id=editor,
                    role="editor", actor_client_id=owner))
    assert m["role"] == "editor"

    # editor (non-owner) cannot invite
    with pytest.raises(PermissionError):
        _run(invite(store, repo, workspace_id=wid, client_id=stranger,
                    role="viewer", actor_client_id=editor))
    # stranger (non-member) cannot invite
    with pytest.raises(PermissionError):
        _run(invite(store, repo, workspace_id=wid, client_id=stranger,
                    role="viewer", actor_client_id=stranger))

    # bad role
    with pytest.raises(InvalidInput):
        _run(invite(store, repo, workspace_id=wid, client_id=stranger,
                    role="admin", actor_client_id=owner))
    # duplicate membership
    with pytest.raises(Conflict):
        _run(invite(store, repo, workspace_id=wid, client_id=editor,
                    role="viewer", actor_client_id=owner))

    # invite audited
    actions = [a.action for a in _run(repo.audit_list())]
    assert "workspace_invite" in actions


def test_invite_unknown_workspace(store, repo):
    with pytest.raises(NotFound):
        _run(invite(store, repo, workspace_id=uuid4(), client_id=uuid4(),
                    role="viewer", actor_client_id=uuid4()))


# ---------------------------------------------------------------- check_access matrix
def test_check_access_matrix(store, repo):
    ws, owner = _make_ws(store, repo)
    wid = UUID(ws["id"])
    editor, viewer, stranger = uuid4(), uuid4(), uuid4()
    _run(invite(store, repo, workspace_id=wid, client_id=editor,
                role="editor", actor_client_id=owner))
    _run(invite(store, repo, workspace_id=wid, client_id=viewer,
                role="viewer", actor_client_id=owner))

    async def _matrix():
        return {
            (str(who), need): await check_access(
                store, workspace_id=wid, client_id=who, need=need)
            for who in (owner, editor, viewer, stranger)
            for need in ("read", "write", "admin")
        }

    got = _run(_matrix())
    o, e, v, s = (str(owner), str(editor), str(viewer), str(stranger))
    assert got[(o, "read")] and got[(o, "write")] and got[(o, "admin")]
    assert got[(e, "read")] and got[(e, "write")] and not got[(e, "admin")]
    assert got[(v, "read")] and not got[(v, "write")] and not got[(v, "admin")]
    assert not any(got[(s, n)] for n in ("read", "write", "admin"))


def test_check_access_bad_need(store):
    with pytest.raises(InvalidInput):
        _run(check_access(store, workspace_id=uuid4(), client_id=uuid4(),
                          need="delete"))


# ---------------------------------------------------------------- personal red line
def test_personal_domain_red_line():
    tag = "workspace:a3f9c2e1"
    # personal + workspace tag -> rejected (REST maps to 422)
    with pytest.raises(InvalidInput):
        validate_workspace_memory("personal", [tag])
    with pytest.raises(InvalidInput):
        validate_workspace_memory("personal", ["work", tag, "coding_style"])
    # everything else passes
    validate_workspace_memory("project", [tag])
    validate_workspace_memory("fact", [tag])
    validate_workspace_memory("personal", ["work", "health"])
    validate_workspace_memory("personal", [])
    validate_workspace_memory("personal", ["workspace:notes"])  # not a real ws tag


# ---------------------------------------------------------------- member tokens
def _decode(token):
    return jwt.decode(token, jwt_signing_key(), algorithms=["HS256"])


def test_mint_member_token_viewer(store, repo, oauth_store):
    ws, owner = _make_ws(store, repo)
    ws_rec = _run(store.get_workspace(UUID(ws["id"])))
    viewer = uuid4()
    _run(invite(store, repo, workspace_id=ws_rec.id, client_id=viewer,
                role="viewer", actor_client_id=owner))

    share, token = _run(mint_member_token(
        oauth_store, workspace=ws_rec, role="viewer"))
    expected = f"memory.read.{ws['project_tag']}"
    assert share.scopes == [expected]  # read-only, via mint_share_token

    claims = _decode(token)
    assert claims["sub"] == "share"
    assert claims["jti"] == str(share.id)
    assert claims["scopes"] == [expected]


def test_mint_member_token_editor(store, repo, oauth_store):
    ws, owner = _make_ws(store, repo)
    ws_rec = _run(store.get_workspace(UUID(ws["id"])))
    share, token = _run(mint_member_token(
        oauth_store, workspace=ws_rec, role="editor"))
    expected = {f"memory.read.{ws['project_tag']}",
                f"memory.write.{ws['project_tag']}"}
    assert set(share.scopes) == expected  # read+write scope language

    claims = _decode(token)
    assert claims["sub"] == "share"
    assert claims["jti"] == str(share.id)
    assert set(claims["scopes"]) == expected


def test_mint_member_token_registers_tag_as_custom(store, repo, oauth_store):
    # The workspace tag must pass mint_share_token's tag validation, which
    # only accepts preset + custom tags.
    ws, _ = _make_ws(store, repo)
    ws_rec = _run(store.get_workspace(UUID(ws["id"])))
    _run(mint_member_token(oauth_store, workspace=ws_rec, role="viewer"))
    tags = {t["tag"] for t in _run(oauth_store.list_custom_tags())}
    assert ws["project_tag"] in tags


def test_mint_member_token_bad_role(store, repo, oauth_store):
    ws, _ = _make_ws(store, repo)
    ws_rec = _run(store.get_workspace(UUID(ws["id"])))
    with pytest.raises(InvalidInput):
        _run(mint_member_token(oauth_store, workspace=ws_rec, role="admin"))


# ---------------------------------------------------------------- membership lifecycle
def test_set_role_and_remove_member(store, repo):
    ws, owner = _make_ws(store, repo)
    wid = UUID(ws["id"])
    member = uuid4()
    _run(invite(store, repo, workspace_id=wid, client_id=member,
                role="viewer", actor_client_id=owner))
    assert not _run(check_access(store, workspace_id=wid, client_id=member,
                                 need="write"))
    _run(store.set_role(workspace_id=wid, client_id=member, role="editor"))
    assert _run(check_access(store, workspace_id=wid, client_id=member,
                             need="write"))
    with pytest.raises(InvalidInput):
        _run(store.set_role(workspace_id=wid, client_id=member, role="nope"))
    with pytest.raises(KeyError):
        _run(store.set_role(workspace_id=wid, client_id=uuid4(), role="viewer"))

    assert _run(store.remove_member(workspace_id=wid, client_id=member)) is True
    assert not _run(check_access(store, workspace_id=wid, client_id=member,
                                 need="read"))
    assert _run(store.remove_member(workspace_id=wid, client_id=member)) is False


def test_delete_workspace(store, repo):
    ws, owner = _make_ws(store, repo)
    wid = UUID(ws["id"])
    assert _run(store.delete_workspace(wid)) is True
    assert _run(store.get_workspace(wid)) is None
    assert _run(store.list_members(wid)) == []
    assert not _run(check_access(store, workspace_id=wid, client_id=owner,
                                 need="read"))
    assert _run(store.delete_workspace(wid)) is False


def test_module_exports():
    # The public surface the REST layer will wire up.
    for name in ("create_workspace", "invite", "check_access", "workspace_tag",
                 "validate_workspace_memory", "mint_member_token",
                 "TeamStore", "InMemoryTeamStore", "PostgresTeamStore",
                 "WorkspaceRecord", "WorkspaceMemberRecord"):
        assert hasattr(teams, name), name
