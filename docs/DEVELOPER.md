**English** | [中文](DEVELOPER.zh-CN.md)

# Porter — Developer Integration Guide

## 1. What Porter is

Porter is a **user-owned memory and state layer** for AI coding harnesses (Claude Code, Codex, Cursor, Zed, …).
The user holds the single source of truth; every app is a **guest** that only ever has temporary, scope-limited
access. The design principles: **deny-by-default least-privilege grants** — every read needs an explicit scope —
**full auditability** of who read or wrote what and when, **one-click portability** of everything in open formats,
and **treating all memories returned to a client as untrusted input** that must never be executed as instructions.
In v1 Porter solves "one person's many tools sharing memory and state"; winning over third-party app ecosystems
is explicitly out of scope (PRD §5).

Base URL: `http(s)://<porter-host>:8000`. All API routes below are relative to it. Error bodies are always
`{"error": {"code": "<code>", "message": "<message>"}}` (PRD §11).

---

## 2. OAuth 2.0 authorization-code flow

Porter is both authorization server and resource server. Standard authorization-code flow (PRD §10.2).
Token lifetimes: **authorization code 10 minutes, single-use**; **access token 1 hour**; **refresh token 30 days**
(sliding renewal).

### Step 0 — Register your client

The very first client registration requires the one-time setup token (see server startup logs or
`PORTER_SETUP_TOKEN`); afterwards it requires `memory.admin`. The secret is shown **once** — store it.

```bash
curl -s -X POST http://localhost:8000/api/v1/clients \
  -H "Content-Type: application/json" \
  -H "x-porter-setup-token: <setup-token>" \
  -d '{
    "name": "My AI App",
    "redirect_uris": ["https://myapp.example.com/oauth/callback"]
  }'
# -> 201 {"id": "<uuid>", "client_id": "porter_...", "client_secret": "<shown-once>",
#         "name": "My AI App", "redirect_uris": [...], "created_at": "..."}
```

### Step 1 — Authorization request

Send the user to the consent screen. Request **least-privilege** scopes (see §3):

```http
GET /oauth/authorize?response_type=code
  &client_id=porter_...
  &redirect_uri=https://myapp.example.com/oauth/callback
  &scope=memory.read.dietary%20memory.write
  &state=<random-csrf-state>
```

The consent screen shows your client name, a per-scope explanation in Chinese, and lets the user
approve/deny each scope individually (PRD §10.2). Verify `state` on the way back (CSRF).

### Step 2 — Exchange the code for tokens

```bash
curl -s -X POST http://localhost:8000/oauth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code" \
  -d "code=<authorization-code>" \
  -d "redirect_uri=https://myapp.example.com/oauth/callback" \
  -d "client_id=porter_..." \
  -d "client_secret=<client-secret>"
# -> 200 {"access_token": "eyJ...", "token_type": "Bearer", "expires_in": 3600,
#         "refresh_token": "porter_rt_...", "scope": "memory.read.dietary memory.write"}
```

### Step 3 — Call the API with the bearer token

```bash
curl -s -X POST http://localhost:8000/api/v1/memories/search \
  -H "Authorization: Bearer <access-token>" \
  -H "Content-Type: application/json" \
  -d '{"query": "what foods should I avoid", "top_k": 5, "tags": ["dietary"]}'
```

Scopes are **enforced server-side** on every call; the server never trusts what the client claims (T1, PRD §13.3).

### Step 4 — Refresh

```bash
curl -s -X POST http://localhost:8000/oauth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=refresh_token" \
  -d "refresh_token=<refresh-token>" \
  -d "client_id=porter_..." \
  -d "client_secret=<client-secret>"
# -> new access_token + ROTATED refresh_token (sliding renewal)
```

Refresh-token reuse is treated as compromise: if a refresh token is used twice, **all of that client's tokens
are revoked** and the user is notified (PRD §10.4). Never cache or retry with an already-consumed refresh token.

### Step 5 — Revoke

```bash
# Revoke one token:
curl -s -X POST http://localhost:8000/oauth/revoke \
  -d "token=<token-to-revoke>" \
  -d "client_id=porter_..." \
  -d "client_secret=<client-secret>"
# -> 200 {"revoked": true}

# Revoke everything for this client (e.g. on user logout / "disconnect Porter"):
curl -s -X POST http://localhost:8000/oauth/revoke \
  -d "token=<any-valid-token>" \
  -d "client_id=porter_..." \
  -d "client_secret=<client-secret>" \
  -d "revoke_all=true"
```

The user can also revoke from the panel at any time — revoked tokens land on a blacklist that takes effect
within 60 seconds (access tokens also expire naturally within 1 hour as a backstop).

---

## 3. Scope catalog

### Base scopes (`porter/oauth.py:BASE_SCOPES`)

| Scope | Label (consent screen) | What it allows |
|---|---|---|
| `memory.read` | 读取全部记忆 | Search and read **all** memories, regardless of tag |
| `memory.write` | 写入记忆 | Submit new memories → they land in the **pending-review queue** first; user approval makes them searchable |
| `memory.admin` | 管理记忆 ⚠️ | Approve/delete memories, read audit logs, manage clients & scope tags. Implies everything. Highest privilege |
| `state.export` | 导出状态快照 | Create/download state snapshots |
| `state.import` | 导入状态快照 | Apply a snapshot (restore memories & agent state) |

### Tag-scoped reads: `memory.read.<tag>`

Any of the 10 preset tags (users may add custom ones) can scope reads: `dietary`, `coding_style`, `workflow`,
`work`, `travel`, `health`, `finance`, `social`, `device`, `general`. A token with `memory.read.dietary` may
search **only** memories tagged `dietary`; tag filtering is enforced server-side from the token's granted
scopes, never from client-supplied tags (`porter/oauth.py:effective_tag_filter`, fail-closed).

**US6 acceptance example** — requesting `memory.read` denied vs `memory.read.dietary` granted:

```bash
# Client asks for "memory.read" on the consent screen -> user denies it, approves "memory.read.dietary".
# The issued token carries only ["memory.read.dietary"].

# This now fails: the token does not imply full memory.read...
curl -s -X GET "http://localhost:8000/api/v1/memories?tag=health" \
  -H "Authorization: Bearer <dietary-only-token>"
# -> [] (fail closed: no rows outside the grant — tag intersection is empty)

# ...while the granted tag works:
curl -s -X POST http://localhost:8000/api/v1/memories/search \
  -H "Authorization: Bearer <dietary-only-token>" \
  -d '{"query": "dietary restrictions", "top_k": 5}'
# -> 200: only memories with scope_tags ⊆ [dietary]

# A token with NO read scope at all:
curl -s -X POST http://localhost:8000/api/v1/memories/search \
  -H "Authorization: Bearer <write-only-token>" \
  -d '{"query": "dietary restrictions"}'
# -> 403 {"error": {"code": "insufficient_scope", "message": "missing required scope: memory.read"}}
```

Notes on scope matching (`porter/auth.py:scope_implies`): `memory.admin` implies every scope. Any
`memory.read.<tag>` satisfies the `memory.read` gate (the endpoints gate on `memory.read`; the tag filter
narrows what is visible). ⚠️ **Asymmetry to know:** the REST `PATCH /api/v1/memories/{id}` (history-preserving
edit) requires `memory.admin`, while the MCP `memory_update` tool requires only `memory.write`. Both create a
new revision linked via `superseded_by`; the old version is archived, never mutated in place.

### Endpoint → required scope

| Method & path | Scope | Notes |
|---|---|---|
| `POST /api/v1/memories/search` | `memory.read` (or `memory.read.<tag>`) | Semantic search over **active** memories; tag-filtered server-side |
| `GET /api/v1/memories` | `memory.read` (or `memory.read.<tag>`) | List/filter; non-admin tokens only ever see `active` memories |
| `POST /api/v1/memories` | `memory.write` | → `202 {"id", "status": "pending_review", "duplicate_candidates"}` |
| `POST /api/v1/memories/{id}/approve` | `memory.admin` | Panel/owner flow; visibility change is audited (T7) |
| `PATCH /api/v1/memories/{id}` | `memory.admin` | History-preserving edit; returns a **new** id |
| `DELETE /api/v1/memories/{id}` | `memory.admin` | Hard delete (row + vector gone, US5) |
| `GET /api/v1/audit-log` | `memory.admin` | Who read/wrote what, when (ids and counts only — never content) |
| `POST /api/v1/clients` | `memory.admin` (after bootstrap) | Secret shown once |
| `GET /api/v1/clients`, scope-tag management | `memory.admin` | |
| `GET /api/v1/export` | `memory.admin` | Full export (porter-export/v1, PRD §15) |
| `POST /api/v1/snapshots`, `GET /api/v1/snapshots`, `GET /api/v1/snapshots/{id}/download` | `state.export` | Ed25519-signed snapshots |
| `POST /api/v1/snapshots/apply` | `state.import` | |
| `GET /api/v1/sync/delta`, `POST /api/v1/sync/cursor` | `memory.read` (or `memory.read.<tag>`) | **P1** — live (see §5) |
| `GET /oauth/authorize`, `POST /oauth/authorize`, `POST /oauth/token`, `POST /oauth/revoke` | client credentials / user session | OAuth itself |
| `POST /api/v1/shares`, `GET /api/v1/shares`, `DELETE /api/v1/shares/{id}` | `memory.admin` | **P2** — owner-minted share tokens (see below) |

### Share tokens: `memory.share` (P2)

The owner can mint restricted, tag-scoped read tokens — e.g. to let a friend's agent read your
`dietary` memories — without a full OAuth client registration. Mint via `POST /api/v1/shares`
(`memory.admin`) or the panel (`/panel/shares`):

```http
POST /api/v1/shares
Authorization: <redacted>
Content-Type: application/json

{"name": "friend's agent", "tags": ["dietary"], "expires_in_hours": 168}
```

```jsonc
// 201 — the token is shown exactly ONCE; it is never stored or logged
{"id": "…", "name": "friend's agent", "scopes": ["memory.read.dietary"],
 "created_at": "…", "expires_at": "…", "revoked": false, "token": "<JWT>"}
```

**Security contract** (`porter/oauth.py:mint_share_token`):

- Every tag must be a real preset or custom tag — `memory.admin`, `../`, `""`, duplicates, and
  unknown tags are rejected with `400 invalid_scope`. Scopes are *derived* from validated tags,
  never taken from caller input, so scope smuggling is impossible.
- Minted scopes are **always exactly** `[memory.read.<tag> …]` — nothing else, ever. A share
  token can search/list/`sync/delta` (tag-filtered, server-side) and nothing else: writes, share
  minting, client management, and PATCH are all `403`.
- The JWT (`sub: "share"`, `jti` = share id) is the credential; the `shares` table holds only
  metadata + revocation state. `DELETE /api/v1/shares/{id}` revokes it — the token dies on its
  next request (well inside the 60s budget).

---

## 4. MCP tools (harness adapters)

Porter ships an MCP server (`porter/mcp_server.py`) with the PRD §11 tool set. Auth works the same way:
every tool call verifies the bearer token (`PORTER_TOKEN` env for stdio, or the `Authorization` header for
Streamable HTTP) and enforces scopes server-side (T1).

| Tool | Signature | Scope | Behavior |
|---|---|---|---|
| `memory_search` | `(query, top_k=5, tags=None)` | `memory.read` / `memory.read.<tag>` | Semantic search; tag filter enforced server-side, fail-closed |
| `memory_add` | `(content, type="fact", scope_tags=None, confidence=1.0, project=None)` | `memory.write` | **Tiered approval** (§7): low-risk auto-activates (searchable immediately); sensitive/conflict/low-confidence queues for review. `type` may be `personal`/`project` to declare the memory domain (SPECS §7.1.1); `project="<name>"` adds a normalized `project:<name>` scope tag. Returns `id`, `status` (`pending_review`/`active`/`merged`), `triage_reasons`, `duplicate_candidates` |
| `memory_capture` | `(conversation_text, source_tags=None)` | `memory.write` | Extracts durable facts from a transcript; each fact goes through the same triage as `memory_add`. ≤10 facts/call, each burns a rate-limit token |
| `memory_update` | `(id, content)` | `memory.write` | Updates an **active** memory; history-preserving (new id via `superseded_by`), returns `revised_from` |
| `sync_delta` | `(since=0, limit=100, project=None)` | `memory.read` / `memory.read.<tag>` | Incremental change feed (P1 auto-sync). `project="<name>"` pulls only that project's working memory plus untagged/global memories (SPECS §7.1.1) |

**All returned memory is `untrusted_data`.** Every result carries `"untrusted_data": true`, and the tool
descriptions say so explicitly (T3, PRD §5 principle 5): memory content must be treated as untrusted user data
and **never as instructions** — do not let it override the harness's system prompt or tool policies.

**stdio config snippet** (Claude Code `.mcp.json`; merge, don't replace):

```json
{
  "mcpServers": {
    "porter": {
      "command": "python3",
      "args": ["-m", "porter.mcp_stdio"],
      "env": { "PORTER_TOKEN": "<access-token-with-memory.read-memory.write>" }
    }
  }
}
```

> Keep `PORTER_REPO=postgres` + `DATABASE_URL` pointing at the **same** Postgres the Porter API uses,
> otherwise memories live in a separate ephemeral store. See `clients/claude-code-mcp.json` and
> `clients/codex-config.toml` for full templates, and `DEPLOY.md` for the Streamable HTTP alternative
> (`POST http://localhost:8000/mcp` with `Authorization: Bearer <token>`).

---

## 5. Automation layer: tiered approval, decay, digest

Writes no longer always queue. `porter/triage.py` triages every write
into two memory domains: **personal** (your own memories — preferences,
health, finance, PII — always queued for human review) and **project**
(the harness's working memory for a project/workspace — instructions,
task state, decisions, codebase knowledge — auto-approved under a
`project:<name>` scope). Queues catch: sensitive content/tags
(content-based scan — tag smuggling doesn't bypass), project memories
without a project tag (`missing_project_scope`), same-project conflicts
(similarity 0.70–0.92 within one project only), low confidence (< 0.6).
Near-identical writes (≥ 0.97) auto-merge into the existing memory (tags
are not unioned). Set `PORTER_APPROVAL_MODE=strict` to restore v1
behavior (everything queues). Full rules: SPECS.md §7.1.

### 5a. ML triage model (optional, recommended)

An open-source second opinion on top of the rules
(`porter/triage_model.py`):

- **Model**: `MoritzLaurer/mDeBERTa-v3-base-mnli-xnli` (~280M params,
  multilingual zh/en, CPU-runnable) via HuggingFace
  `zero-shot-classification`. Chosen over larger English-only models for
  cross-lingual zero-shot quality at acceptable per-write latency.
- **Setup**: `pip install transformers torch` (in `requirements-dev.txt`),
  then just write — the model lazy-loads on first use with a bounded
  load timeout.
- **Knobs**: `PORTER_TRIAGE_MODEL=auto` (default: finetuned if a model
  dir exists → zeroshot if importable → heuristic) / `zeroshot` /
  `heuristic` / `finetuned`; `PORTER_TRIAGE_MODEL_PATH` (default
  `models/triage-v1`); `PORTER_TRIAGE_THRESHOLD` (default 0.65);
  `PORTER_TRIAGE_TIMEOUT` (default 2s).
- **Safety**: rules stay the fast pre-filter AND the fallback — the model
  only sees rule-passed writes; a >2s inference falls back to the
  heuristic for that write (audit-logged); E2EE memories never reach the
  model (ciphertext-blind). Scores are LRU-cached per content hash
  (model scores only; rules always run fresh).
- **Latency**: expect ~100–500ms CPU per write for the zero-shot pass
  (first load downloads ~1GB of weights once).

### 5b. Triage model training loop (predict → correct → train → deploy)

Zero-shot is a good start; a few hundred of YOUR hand-corrected labels
beat it on your distribution. The loop:

1. **Predict** — `python scripts/triage_label_batch.py --input texts.txt
   --output labels.jsonl` (or `--from-db --limit 200` to sample recent
   memories). Runs the current stack and writes one JSONL line per
   text: `{"text": "...", "domain": "personal|project",
   "project": "<name|null>",
   "sensitivity": {"health": 0/1, "finance": 0/1, "pii": 0/1,
   "credentials": 0/1}, "predicted_by": "zeroshot|heuristic"}`.
2. **Correct** — hand-edit the JSONL: fix `domain` and the 0/1
   sensitivity bits in place. This is the training signal — your inbox
   approvals/rejections are the ground truth.
3. **Train** — `python scripts/finetune_triage.py --input
   labels_corrected.jsonl --output models/triage-v1`. Fine-tunes
   `microsoft/mdeberta-v3-base` with a 6-label head (2 domain + 4
   sensitivity; CPU-friendly defaults: 3 epochs, batch 8, eval split,
   best checkpoint saved). Prints per-label precision/recall/F1 on the
   eval split at the end. Needs `transformers torch scikit-learn`.
   `--dry-run` checks the plumbing without training.
4. **Deploy** — set `PORTER_TRIAGE_MODEL_PATH=models/triage-v1` (or
   `PORTER_TRIAGE_MODEL=finetuned`); `auto` picks it up with top
   priority. Re-run step 1 with the finetuned model to spot-check.

Volume guidance: a few hundred labeled examples is enough to beat
zero-shot; past ~2000 the gains flatten for this 6-label task.

- **Decay:** active memories lose confidence while idle (×0.95 per 30 days,
  lazy on read); below 0.2 they auto-archive (audited, restorable). Pin
  important memories: `POST /api/v1/memories/{id}/pin` (admin).
- **Digest:** `GET /api/v1/digest` shows pending counts by reason, recent
  auto-approvals, and conflicts. Pending *counts* are visible to any reader;
  pending *contents* need `memory.admin` (same inbox rule as the sync feed).
- **E2EE** memories always queue and never decay/are scanned (ciphertext-blind).

## 6. Sync polling (P1 auto-sync)

> **Status: P1 — live.** `GET /api/v1/sync/delta` returns the incremental change feed (v1-only
> servers answer `501 {"error": {"code": "sync_not_available", ...}}`; v1 is manual snapshots only,
> PRD §9.5). The contract below is the implemented behavior.

Hub-and-spoke: the Porter server is the single source of truth; each harness adapter keeps a per-device
`cursor` and long-polls for the delta.

```http
GET /api/v1/sync/delta?since=<int>&limit=100
Authorization: Bearer <token>
```

```jsonc
// 200 response
{
  "changes": [
    {
      "seq": 1042,                    // int64, strictly increasing
      "memory_id": "mem_abc123",      // UUID string
      "op": "create",                // "create" | "revise" | "approve" | "delete"
      "status": "active",            // "pending" | "active" | "archived"
      "at": "2026-10-11T12:34:56Z",  // ISO-8601 timestamp
      "actor_client_id": "porter_claude",
      "memory": {                    // full memory object, or null for "delete"
        "id": "mem_abc123",
        "content": "...",            // E2EE memories: the base64 ciphertext,
                                     // with "encrypted": true + "key_id"
        "type": "preference",
        "scope_tags": ["dietary"],
        "confidence": 0.95,
        "untrusted_data": true        // T3: synced content is untrusted input
      }
    }
  ],
  "cursor": 1042                     // the highest seq returned
}
```

Acknowledge your position so the next poll continues where you left off:

```http
POST /api/v1/sync/cursor
Authorization: Bearer <token>
Content-Type: application/json

{"cursor": 1042}
```

**Poll loop guidance (60 s default):**

```
cursor = load_cursor_from_disk() or 0
loop:
    r = GET /sync/delta?since=<cursor>&limit=100
    for change in r.changes: apply(change)      # idempotent by seq
    if r.changes: cursor = r.cursor; persist; POST /sync/cursor {"cursor": cursor}
    sleep(60)
```

- Poll **at most** every 60 seconds (long-polling/SSE may arrive in v1.5); back off on `429 rate_limited`.
- Apply changes idempotently keyed by `seq`; dedupe on re-delivery.
- Tag-scoped tokens receive only changes visible under their scopes (same `effective_tag_filter` rules as §3).

**Conflicts.** Last-writer-wins is the default, but nothing is silently dropped: conflicting writes both land
in the user's pending-review queue for a decision, never overwritten in place (PRD §9.3–9.5). `revise` ops
create new ids linked via `superseded_by`; use the new id from that point on.

---

## 7. End-to-end encryption (P2)

> **Status: implemented (P2).** The server now consumes `content_ciphertext` per the contract below:
> `POST /api/v1/memories` (and the MCP `memory_add` tool) accepts either `content` or
> (`content_ciphertext` + `encryption`) — never both, never neither (422 otherwise). Reads (list, sync
> delta, export, snapshots) return the ciphertext in `content` with `encrypted: true` + `key_id`;
> the server never decrypts (it holds no keys).

For memories the Porter server itself must never see, encrypt on the client and post the ciphertext:

```http
POST /api/v1/memories
Authorization: Bearer <token-with-memory.write>
Content-Type: application/json
```

```jsonc
{
  // NOTE: no "content" field when encrypted.
  "content_ciphertext": "<base64 of XChaCha20-Poly1305(plaintext)>",
  "encryption": {
    "alg": "xchacha20-poly1305",
    "key_id": "2026-10-dietary-01"
  },
  "type": "preference",
  "scope_tags": ["dietary"],
  "confidence": 0.95,
  "source": {"client_id": "porter_claude"}
}
```

**Server guarantees:** the ciphertext is stored as-is; it is **never embedded, never indexed, never logged**
in plaintext. It is returned verbatim on reads.

**Key management is the client's job.** The sample clients (`clients/python/porter_client.py`,
`clients/typescript/porter-client.ts`) implement this approach:

- Keys live at `~/.porter/keys/<key_id>.key`, mode **0600**.
- Generation: `crypto_box`/`crypto_aead` via libsodium (`pynacl` / `libsodium-wrappers`); XChaCha20-Poly1305
  with a random 24-byte nonce prepended to the ciphertext (`nonce || ciphertext`, base64-encoded).
- Alternative for passphrases: password-derived key (Argon2id → 32-byte key), same on-disk layout.
- Syncing keys across the user's devices is out of scope for v1 (use your OS keychain / manual copy for now).

**Honest limitations (P2, by design):**

- **No semantic search or dedupe over encrypted memories** — the server has no embedding and no plaintext to
  compare, so `memory_search` cannot match them and `duplicate_candidates` is always empty for them.
- **Tags, type, confidence, timestamps stay plaintext metadata** — only `content` is encrypted. If the tag
  itself is sensitive (e.g. a custom tag naming a medical condition), reconsider what you put in `scope_tags`.
- The `source` block (client_id, created_at) is plaintext as usual.

---

## 8. Error codes

From PRD §11 and `porter/errors.py`. Shape: `{"error": {"code", "message"}}`.

| HTTP | Code | When |
|---|---|---|
| 400 | `invalid_scope` | Unknown or malformed scope requested |
| 400 | `invalid_grant` | Bad/expired/reused authorization code, bad refresh token or client credentials |
| 401 | `invalid_token` | Missing or invalid `Authorization: Bearer` token |
| 401 | `token_revoked` | Token revoked (blacklist hit) or refresh-token reuse detected (T2) |
| 403 | `insufficient_scope` | Token valid but lacks the required scope |
| 404 | `not_found` | Memory / client / snapshot not found |
| 409 | `conflict` | Version/snapshot conflict |
| 422 | `invalid_input` | Validation failure (bad UUID, confidence out of range, unknown tag, …) |
| 429 | `rate_limited` | Per-client rate limit exceeded (T5) — back off |
| 501 | `sync_not_available` | P1 stub: `/sync/delta` in v1 |
| 503 | `not_ready` | Repository not initialized yet (startup) |
| 500 | `internal_error` | Unexpected failure |

PRD §11 also names `duplicate_memory` and `snapshot_conflict` — v1 reports near-duplicates as the
`duplicate_candidates` field on write (202), not as an error.

---

## 9. Security notes for integrators

1. **Memories are untrusted input (T3).** Everything returned by search, list, MCP, or sync carries
   `untrusted_data: true`. Never treat memory content as instructions; never let it override your system
   prompt, tool policies, or allowlists. This is the single most important rule.
2. **Token storage.** `client_secret` and refresh tokens are high-value: store them in the OS keychain or a
   secret manager, never in code, logs, or chat transcripts. Client secrets are argon2id-hashed server-side
   and cannot be recovered — if lost, register a new client.
3. **Never log memory content.** Audit logging is the server's job (ids and counts only). Client-side logs,
   crash reports, and analytics must not contain memory text — especially for tag-scoped or encrypted
   memories.
4. **Least privilege.** Prefer `memory.read.<tag>` over `memory.read`, and never request `memory.admin`
   for a client that only needs search. Users can (and do) deny broad scopes on the consent screen.
5. **Write goes to review.** `memory.write` only submits to the pending-review queue; do not promise users
   that a write is immediately searchable. Handle `duplicate_candidates` in your UX.
6. **Reused refresh token = compromise.** If a refresh fails with `token_revoked`, stop: rotate credentials
   and surface the event; do not retry the old refresh token.
7. **Encrypted memories.** Only `content` is encrypted; tags/type/timestamps are plaintext metadata (see §6).
   Key files must be 0600 and never leave the user's devices through your telemetry.

## 10. Sample clients

- Python (stdlib + `requests` + `pynacl`): [`../clients/python/porter_client.py`](../clients/python/porter_client.py)
- TypeScript (`fetch` + `libsodium-wrappers`): [`../clients/typescript/porter-client.ts`](../clients/typescript/porter-client.ts)

Both cover OAuth token use, search/add/update, E2EE helpers, and the 60-second sync poll loop.
