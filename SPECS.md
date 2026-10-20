**English** | [中文](SPECS.zh-CN.md)

# Porter Open Specification

**Spec version: 1.0** · Authored 2026-10-11 from the Porter reference implementation
(`porter/` at `~/workspace/porter`, app version `0.1.0`).

This document is the versioned, implementation-anchored specification of Porter's
interop surface: scopes, audit records, snapshots, export payloads, the sync
protocol, and the E2EE memory envelope. A third-party implementation that
satisfies the normative statements here (and the [Conformance](#conformance) section)
may claim to be **porter-compatible**.

**Convention used throughout:**

- `[spec-implemented]` — the normative rule is enforced by the reference
  implementation today.
- `[spec-proposed]` — the rule is a reserved/roadmap surface (P1/P2). It is
  normative for future implementations and for the contract agents build against,
  but the reference implementation does not enforce it yet.
- **CODE WINS.** Where the PRD (`~/workspace/your_files/porter-prd-v20.md`) and
  the code disagree, this spec follows the code and calls out the discrepancy.

---

## 1. Scope model — `spec: scopes/v1` [spec-implemented]

### Rationale

Every third-party harness touches Porter through an OAuth token carrying a scope
set. The scope vocabulary must be fixed and unambiguous so a token issued for
"reading coding-style memories" can never silently widen to everything.

### 1.1 Vocabulary

| Scope | Granted by | Meaning |
|---|---|---|
| `memory.read` | token | Retrieve and view all memory entries (all tags) |
| `memory.read.<tag>` | token | Retrieve and view memory entries whose `scope_tags` include `<tag>` only. `<tag>` must be a preset tag (see below) or a user-registered custom tag (`migration 003`: `^[a-z0-9_]{1,32}$`) |
| `memory.write` | token | Submit new memories. Writes land in the **pending-confirmation queue** first; nothing is written straight to active |
| `memory.admin` | token | Approve/delete memories, read the audit log, manage clients. Implies everything |
| `state.export` | token | Create snapshots (`POST /snapshots`) and read export payloads (`GET /api/v1/export`) |
| `state.import` | token | Apply a snapshot (`POST /snapshots/apply`); applied memories land in the pending queue, never active directly |
| `memory.share` | — | **P2 only.** Minting share tokens. [spec-proposed]; see §1.4 |

Reference implementation source: `BASE_SCOPES` and `PRESET_SCOPE_TAGS` in
`porter/oauth.py`. The 10 preset tags are:
`dietary`, `coding_style`, `workflow`, `work`, `travel`, `health`, `finance`,
`social`, `device`, `general`.

Unknown scopes are rejected at issuance (`validate_scopes` → `InvalidScope`, HTTP
400, code `invalid_scope`).

### 1.2 Implication rules (normative)

`scope_implies(granted, required)` [spec-implemented]:

1. `memory.admin` ∈ granted ⇒ **all** requirements are satisfied.
2. Required `memory.read` is satisfied by `memory.read` itself **or by any
   `memory.read.<tag>`** — but the caller is then restricted to the union of its
   tags (see §1.3).
3. Every other requirement must be literally present in the granted set.

### 1.3 Server-side tag filtering (normative)

`effective_tag_filter(granted)` returns [spec-implemented]:

- `None` (unrestricted) when `memory.admin` or `memory.read` is granted;
- otherwise the **sorted** list of tags parsed from `memory.read.<tag>` scopes.

`apply_tag_filter(requested, allowed)` intersects any client-supplied tag hint
with the server-side allowlist. **Fail-closed rule (normative):** if the
intersection is empty, the query returns **zero rows** — never "no filter".
The reference implementation fails closed in `search_memories`/`list_memories`.

### 1.4 `memory.share` share tokens [spec-proposed]

P2 (per PRD §10 + line 198): a `memory.share` grant lets the holder mint bearer
tokens bound to a **subset of `memory.read.<tag>` scopes** with a fixed expiry.
Issuing a share token requires the grantor to already hold `memory.read` or
`memory.admin` on the covered tags; share tokens never exceed the grantor's own
scope set. The minted token carries `exp`, `aud` (recipient client), and the
reduced scope set, and is validated like any other token.

### Example

Token with scopes `["memory.read.coding_style", "memory.write"]`:

- `memory.read` requirement → **satisfied**, results tag-filtered to `coding_style` only.
- `memory.admin`-gated `DELETE /memories/{id}` → **rejected** (`insufficient_scope`, HTTP 403).

---

## 2. Audit events — `spec: audit/v1` [spec-implemented]

### Rationale

Porter's audit log is the non-repudiable trail of who touched whose memory. It
must be append-only at the storage layer, not just by application convention.

### 2.1 Record schema

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | UUID | yes | Primary key, `gen_random_uuid()` |
| `client_id` | UUID \| null | no | OAuth client; null for first-party/panel operations |
| `action` | enum (see §2.2) | yes | What happened |
| `memory_ids` | UUID[] | yes | Memory records touched by the action (may be empty) |
| `scopes_used` | string[] | yes | Scope names the acting token actually relied on (may be empty) |
| `meta` | JSONB object | yes | Structured context: **ids and counts only** |
| `created_at` | timestamptz | yes | Server-side timestamp, `now()` at insert |

### 2.2 Action vocabulary (normative)

Set by migrations `001_init.sql` + `002_oauth.sql`; matches the reference
implementation's actual `audit(...)` call sites:

`read`, `write`, `delete`, `export`, `snapshot_apply`, `revoke`, `grant`,
`refresh`, `security`, `auto_approve`, `auto_merge`, `auto_archive`, `pin`,
`unpin`.

(`security` covers refresh-token reuse detection — suspected theft.
`auto_*` cover the automation layer, §7.)

### 2.3 Append-only guarantee (normative)

The reference implementation enforces append-only in **Postgres triggers**:
`forbid_audit_mutation()` raises on any `UPDATE` or `DELETE` of `audit_log`
rows (`migrations/001_init.sql`). Application code must not expose any
mutation path either.

### 2.4 Privacy rule (normative)

`meta` carries **ids and counts only** — never memory content and never the
original query text (see also migration comment "ids & counts only, never
content", and PRD §493's telemetry rule "不记录记忆原文与 query 原文").

### Examples

```json
{"id":"9f2c…","client_id":"c41d…","action":"read","memory_ids":["a1…","b2…"],
 "scopes_used":["memory.read.coding_style"],
 "meta":{"result_count":2,"tags":["coding_style"],"query_hash":"e4f1…"},
 "created_at":"2026-10-11T20:02:11Z"}
```

```json
{"id":"77aa…","client_id":"c41d…","action":"security","memory_ids":[],
 "scopes_used":["memory.read","memory.write"],
 "meta":{"reason":"refresh_token_reuse","jti":"5e09…","tokens_revoked":14},
 "created_at":"2026-10-11T20:03:44Z"}
```

---

## 3. Snapshot format — `spec: snapshot/v1` [spec-implemented]

### Rationale

Snapshots are the "move house" unit: a versioned, optionally encrypted,
tamper-evident bundle of active memories (tag-filtered) that can be imported
into another Porter instance — landing in the **pending** queue, never active.

### 3.1 File layout (normative; code-wins)

**Discrepancy note:** the PRD (§9.1) describes a `.porter/` *directory* layout
(`manifest.json` + `memories.jsonl` + `skills/` + `tasks.json` +
`preferences.json` + `signature.sig`). The reference implementation instead
uses a **single file**:

- `snapshot-<YYYYMMDD-HHMMSS>.porter` when encrypted
- `snapshot-<YYYYMMDD-HHMMSS>.porter.json` when unencrypted

Both files contain one JSON body; skills/tasks are currently recorded as zero
counts (placeholders) in the manifest. Per this spec's code-wins rule, the
single-file JSON format below is normative; the directory layout remains a
candidate for `snapshot/v2`.

Payload JSON body:

| Field | Type | Required | Description |
|---|---|---|---|
| `format` | string | yes | Always `"porter-snapshot/v1"` |
| `memories` | array of memory entries | yes | Tag-filtered active memories |
| `signature` | object | recommended | `{sig, pubkey}`, both base64; see §3.2. Legacy files may omit it |

Each memory entry:

| Field | Type | Required | Description |
|---|---|---|---|
| `content` | string | yes | 1–20,000 chars (`MAX_CONTENT_LEN`) |
| `type` | enum | yes | `preference`, `fact`, `episode`, `skill_ref` |
| `scope_tags` | string[] | yes | ≤20 tags, each 1–64 chars |
| `confidence` | number | yes | 0.0–1.0 |
| `source` | object | yes | free-form provenance (e.g. `{client_id, created_at}`) |
| `created_at` | ISO-8601 string | yes | original record timestamp |

### 3.2 Ed25519 signature (normative)

What is signed: the canonical byte encoding of the body **without** the
`signature` block (no chicken-and-egg). Canonical encoding
(`_canonical_bytes` in `porter/snapshots.py`):

```
json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",",":")).encode("utf-8")
```

The `signature` block travels inside the file:

```json
{"signature": {"sig": "<base64 ed25519 signature>", "pubkey": "<base64 raw 32-byte pubkey>"}}
```

The instance keypair is generated on first snapshot creation and stored
`0o600` at `<snapshot_dir>/.keys/ed25519.key`. Verification: base64-decode both
fields, re-canonicalize the body minus `signature`, verify with the raw
32-byte public key. A tampered signature raises (fail closed); a file with no
signature block verifies as `None`/legacy — accepted but flagged
`signature_ok: false` in the report.

### 3.3 Manifest record (normative)

The server stores a manifest row alongside each snapshot file:

| Field | Type | Description |
|---|---|---|
| `snapshot_format` | string | `"porter-snapshot/v1"` |
| `created_at` | ISO-8601 | creation time |
| `source_instance_id` | string | instance that produced the snapshot |
| `filter_tags` | string[] | tag filter used at creation |
| `counts` | object | `{memories: n, skills: 0, tasks: 0}` (skills/tasks currently 0) |
| `content_sha256` | hex string | SHA-256 of the **signed** payload bytes |
| `encrypted` | boolean | whether a password was supplied |
| `signature` | object | copy of the signature block from the body |
| `signature_ok` | boolean | verification result at creation |

### 3.4 Encryption (normative)

Optional, password-based, recommended for transport:

- File bytes layout: `b"PORTER1"` (magic) + 16-byte salt + 12-byte nonce +
  AES-256-GCM ciphertext (payload = the signed JSON body).
- Key derivation: PBKDF2-HMAC-SHA256, **600,000 iterations**, 32-byte key,
  UTF-8-encoded password.
- Detection: the reader checks the magic prefix; an encrypted file without a
  password raises `ValueError("this snapshot is encrypted: password required")`.
  Wrong password or corruption raises on GCM auth failure.
- Snapshot files are written `0o600` (they contain private memories).

### 3.5 Verification & apply steps (normative)

1. Read file; if it starts with `PORTER1`, decrypt with the password (fail
   closed if no password or auth fails).
2. Parse JSON; require `format == "porter-snapshot/v1"`.
3. Verify the Ed25519 signature block (fail closed on tamper; flag legacy
   unsigned).
4. Validate every entry (types, tag syntax, confidence range, content length) —
   any failure aborts the whole snapshot (never partial import).
5. Apply: insert each entry as **pending** memory with `source.imported_via`
   set; audit with `action="snapshot_apply"`.

### Example

```json
{"format":"porter-snapshot/v1",
 "memories":[{"content":"I prefer TypeScript strict mode","type":"preference",
              "scope_tags":["coding_style"],"confidence":0.95,
              "source":{"client_id":"porter_abc123"},"created_at":"2026-10-11T19:00:00Z"}],
 "signature":{"sig":"MEQCI…","pubkey":"nQ3f…"}}
```

---

## 4. Export payload — `porter-export/v1` [spec-implemented]

### Rationale

`GET /api/v1/export` and `GET /panel/export` share one builder
(`porter/exporter.py::build_export_payload`), so the machine and human surfaces
can never drift. Export is for the user's own data portability; embeddings are
deliberately excluded (the importer re-embeds with its own embedder to avoid
dimension/model mismatch), and the audit log exports as metadata only.

### 4.1 Top-level structure (normative)

| Field | Type | Required | Description |
|---|---|---|---|
| `format` | string | yes | Always `"porter-export/v1"` |
| `exported_at` | ISO-8601 string | yes | server time of export |
| `memory_count` | integer | yes | length of `memories` |
| `memories` | array of memory records | yes | full export (active, archived, pending) |
| `note` | string | yes | human note about embeddings/audit handling |

Memory record fields (mirrors `MemoryRecord`/`MemoryOut`):

| Field | Type | Description |
|---|---|---|
| `id` | UUID string | record id |
| `content` | string | memory content |
| `type` | enum | `preference`/`fact`/`episode`/`skill_ref` |
| `scope_tags` | string[] | tags |
| `confidence` | number | 0.0–1.0 |
| `status` | enum | `pending`/`active`/`archived` |
| `source` | object | provenance |
| `created_at` / `updated_at` | ISO-8601 | timestamps |
| `superseded_by` | UUID string \| null | id of the revising record, if any |

### 4.2 Versioning policy (normative)

- Minor evolution is **additive**: new optional fields may appear; readers must
  ignore unknown fields.
- A breaking change (field removal, type change, new required field) introduces
  `"format": "porter-export/v2"`; readers **gate** on the `format` string and
  must refuse with a clear error when they see an unknown `format` instead of
  guessing.

### Example

```json
{"format":"porter-export/v1","exported_at":"2026-10-11T20:05:00Z","memory_count":1,
 "memories":[{"id":"a1b2…","content":"I prefer TypeScript strict mode","type":"preference",
              "scope_tags":["coding_style"],"confidence":0.95,"status":"active",
              "source":{"client_id":"porter_abc123"},"created_at":"2026-10-11T19:00:00Z",
              "updated_at":"2026-10-11T19:00:00Z","superseded_by":null}],
 "note":"Embeddings are not exported; re-embed on import. Audit log exports as metadata only (no memory content)."}
```

---

## 5. Sync protocol — `spec: sync/v1` [implemented, P1]

### Rationale

P1 (hub-and-spoke auto-sync) makes every harness see memories written in any
other harness within 60 seconds, with the Porter server as the single source
of truth. Implemented in `porter/sync.py` (+ REST endpoints in `porter/app.py`,
MCP `sync_delta` tool, panel `/panel/sync`); migration `004_sync.sql`.

### 5.1 Endpoints (contract, normative)

- `GET /api/v1/sync/delta?since=<int>&limit=<int,default 100,max 500>[&project=<name>]` →
  ```json
  {"changes":[{"seq":1234,"memory_id":"a1b2…","op":"approve","status":"active",
               "at":"2026-10-11T20:06:00Z","actor_client_id":"c3d4…",
               "memory":{ /* full memory record, or null */ }}],
   "cursor":1234}
  ```
  - `since`: int >= 0, exclusive (422 otherwise); `limit`: 1..500.
  - `project` (optional): the harness's current project name. The delta then
    carries only that project's working memory — records tagged
    `project:<name>` (normalized match) PLUS untagged/global records (the
    personal domain follows the user everywhere; see §7.1.1). Omit for the
    full feed. This is the "luggage follows you" mechanism: a harness in
    project X never sees project Y's working memory. It is a convenience
    filter, NOT a security boundary — OAuth tag scopes (§1.3) gate
    visibility first, and synced content is always marked `untrusted_data`.
  - `op`: `create` | `approve` | `revise` (subject = the NEW record) |
    `delete` | `archive`. `status` is the record's current status, `null` for delete
    tombstones (the row no longer exists).
  - `memory` is the full memory record, `null` for delete tombstones and for
    `create` of a still-pending record when the caller lacks `memory.admin`
    (inbox rule: unapproved content never leaks to adapters). Non-admin
    callers see active memories plus scope-filtered delete/archive tombstones.
    Archives carry no content to non-admin callers. A revision archives the
    old ID before publishing its replacement. Unknown legacy deleted scopes
    fail closed for project/tag-scoped consumers.
  - Requires `memory.read` (or any `memory.read.<tag>` — results tag-filtered
    server-side per §1.3). Fetches are audited as `action='read'` with
    `meta={"via":"sync"}` and refresh the adapter's `last_seen` WITHOUT
    advancing its cursor.
- `POST /api/v1/sync/cursor` with body `{"cursor": 1234}` →
  `{"adapter_id": "porter_abc123", "cursor": 1234, "last_seen": "…"}`.
  Explicit ack; any valid bearer (no special scope). The adapter is
  identified by its own token's public `client_id` (or `"owner"` with auth
  disabled) — there is deliberately no client-supplied identity field.
  Upserts `sync_cursors(adapter_id TEXT PK, cursor BIGINT, last_seen)`.

### 5.2 Change object schema (normative)

| Field | Type | Required | Description |
|---|---|---|---|
| `seq` | int64 | yes | monotonically increasing change sequence number; also the cursor |
| `memory_id` | UUID | yes | affected record (the NEW record for `revise`) |
| `op` | enum | yes | `create` \| `approve` \| `revise` \| `delete` \| `archive` |
| `status` | enum \| null | yes | record's current status; `null` for delete tombstones |
| `at` | ISO-8601 | yes | server time the change was committed |
| `actor_client_id` | string \| null | no | which client caused it (`clients.id`; null = owner/panel) |
| `memory` | object \| null | yes | full memory record (incl. `version`), or `null` per the inbox/tombstone rules in §5.1 |

### 5.3 Cursor semantics (normative)

- Cursors are **int64 sequence numbers**, strictly monotonic per server.
- `since` is exclusive: the response contains changes with `seq > since`.
- The response's `cursor` is the last **raw sequence scanned on this page**,
  including rows removed by visibility filters. It never skips to the global
  feed head while later pages remain. An empty raw page preserves `since`.
  Apply the page, persist/ack its cursor, and request another page even when
  the visible `changes` array is empty. Fetches never advance the stored
  cursor; only explicit acks do. Sequence publication is serialized through
  transaction commit so concurrent writers cannot skip an uncommitted row.

### 5.4 Conflict rule (normative)

**Last-writer-wins** with **optimistic concurrency** on `memories.version`
(+1 per revise). `PATCH /api/v1/memories/{id}` and MCP
`memory_update(id, content)` accept optional `expected_version`:
- match (or omitted) → plain revise, `conflict: false`;
- mismatch → the write STILL applies (LWW), the overwritten content is
  preserved as a NEW pending inbox record
  (`source={"conflict_of": <new_id>, "client_id": …}`, confidence preserved),
  and the response reports `conflict: true` with `loser_pending_id`
  (HTTP 200, never 409 — a handled outcome, not an error).
- The addressed id is resolved through the `superseded_by` chain to the
  current head first, so a client holding a stale id still gets a correct
  version comparison instead of resurrecting an archived revision.
The loser is **not dropped**: it lands in the pending-confirmation inbox so
the user can arbitrate. Panel `/panel/sync` shows per-adapter online state,
cursor position, lag behind head, and last-seen time.

### 5.5 Adapter responsibilities (normative)

1. **Poll** `GET /sync/delta` at least every **60 seconds** (long-polling;
   SSE/WebSocket is an allowed v1.5 upgrade).
2. **Explicit ack**: send `POST /sync/cursor` only after changes are durable
   locally.
3. **Handle tombstones**: a `tombstone` op removes (or archives) the local
   copy; a deleted memory must not resurrect on the next poll.
4. **Debounce writes**: coalesce local writes for 5s before pushing to Porter,
   to avoid write storms.

---

## 6. E2EE memory envelope — `spec: e2ee/v1` [spec-proposed]

### Rationale

P2 is the "even Porter can't see it" end state: memory content is encrypted on
the client before sync; the server stores ciphertext it can never read. Nothing
in the reference implementation implements this yet — the envelope is reserved
here so adapters and clients can converge on one wire shape.

### 6.1 Envelope format (normative)

A memory's `content` field is replaced by a JSON envelope:

```json
{"content_ciphertext":"<base64>",
 "encryption":{"alg":"xchacha20-poly1305","key_id":"k-2026-10-01"}}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `content_ciphertext` | base64 string | yes | XChaCha20-Poly1305 ciphertext of the plaintext memory (client-chosen nonce, prepended to the ciphertext per the XChaCha20-Poly1305 construction) |
| `encryption.alg` | string | yes | Always `"xchacha20-poly1305"` in v1 |
| `encryption.key_id` | string | yes | opaque client-side key identifier (rotation handle); never the key |

### 6.2 Server guarantees (normative)

- The envelope is stored **as-is** in `content`; the server never decrypts it.
- The server never **embeds, indexes, or logs** the plaintext; ciphertext is
  excluded from semantic search.
- Ciphertext is included in snapshots and exports as ciphertext (portable, but
  only useful to a client holding the key).
- Audit/search metadata (`meta`, scores, ids, tag lists) never contains
  plaintext.

### 6.3 Client key-management recommendations (informative)

- Keys live in the client's OS keychain/hardware store, never in Porter.
- `key_id` rotation: introduce a new key id for new writes; old keys are kept
  only as long as ciphertext under them still exists.
- Losing all keys means losing the memories — there is no server-side recovery.

### 6.4 Honest limitations (normative)

- **No server-side dedupe or semantic search** over encrypted memories
  (matching ciphertext requires identical bytes; semantic search requires
  plaintext).
- **Metadata stays plaintext**: `scope_tags`, `type`, `status`, timestamps,
  and the audit trail are visible to the server — only the memory body is
  sealed. PRD leaves ciphertext-friendly indexing for "另行设计" (a separate
  design); this spec does not promise it.

---

## 7. Automation layer — `spec: automation/v1` [spec-implemented]

### Rationale

A memory layer that requires a human click for every write does not scale
past a single user. The automation layer keeps the human in the loop where
judgment matters (sensitive content, conflicts) and gets out of the way
everywhere else — without weakening any §14 guarantee.

### 7.1 Tiered approval (normative)

Every write (`POST /api/v1/memories`, `memory_add`, panel owner writes,
`memory_capture` facts) passes through triage (`porter/triage.py`):

| Condition | Disposition |
|---|---|
| Personal domain (`personal_domain`) | queue (see §7.1.1) |
| Sensitive scope tag (after normalization) | queue (`sensitive_tag`) |
| Sensitive content (NFKC-normalized pattern scan, EN/ZH; content-based, not tag-based) | queue (`sensitive_content`) |
| ML model flags a sensitivity category | queue (`ml_sensitive:<category>`) |
| Project domain without a `project:<name>` tag | queue (`missing_project_scope`) |
| Max similarity to an active memory in [0.70, 0.92) with the same project tag (project domain) | queue (`possible_conflict`) |
| Max similarity to an active memory in [0.70, 0.92) (personal/unknown domain) | queue (`possible_conflict`) |
| Confidence < 0.6 | queue (`low_confidence`) |
| Similarity in [0.92, 0.97) to an active memory | queue (`duplicate_candidate`) — existing dedup path |
| Similarity ≥ 0.97 to an active memory | **auto-merge** into the target |
| Otherwise | **auto-approve** (`low_risk`), audit-logged |

#### 7.1.1 Two memory domains (normative)

Porter stores two kinds of memory, and triage treats them differently.
This is the core product framing — not a sub-feature of the classifier.

- **Personal domain**: the user's own memories — preferences, health,
  finance, PII, relationships. The sensitivity labels (health / finance /
  pii / credentials) and the human-review queue apply HERE. A personal
  memory always queues for human review (`personal_domain`), as does any
  sensitivity hit regardless of domain.
- **Project domain**: the agent harness's ENTIRE working memory for a
  project/workspace — the "luggage that follows you when you switch from
  Claude Code to Codex/Cursor mid-project". This covers instruction files
  (AGENTS.md / CLAUDE.md / `.cursorrules` / WARP.md / GEMINI.md are just
  examples — ONE source among many), plus task state, decisions made,
  codebase understanding, pitfalls encountered, conventions, and ongoing
  work context. Project memories auto-approve by default (audit-logged),
  with two guardrails:
  - they MUST carry a `project:<name>` tag (derived from the repo or
    working-directory name at ingest). An untagged project memory queues
    with `missing_project_scope` so a human scopes it;
  - conflict detection is **per-project**: a new instruction contradicting
    an existing memory with the SAME project tag queues side-by-side;
    cross-project similarities are NOT conflicts.
- The domain is resolved per write from the explicit `type` (`personal` /
  `project`; v1 `preference` maps to `personal`) and, for rule-passed
  writes, an ML second opinion (see §7.1.2). An unresolved (`unknown`)
  domain triages like the pre-domain default: auto-approve eligible,
  sensitivity/conflict/confidence gates still apply.

#### 7.1.2 ML triage model (informative)

`porter/triage_model.py` adds a learned second opinion on top of the
rules. Backends: `heuristic` (wraps the existing rule patterns, no
behavior change), `zeroshot` (HuggingFace `zero-shot-classification`
with `MoritzLaurer/mDeBERTa-v3-base-mnli-xnli`, ~280M params,
multilingual zh/en, CPU-runnable; domain hypotheses first, then the 4
sensitivity hypotheses, `multi_label=True`, threshold 0.65), `finetuned`
(local `microsoft/mdeberta-v3-base` + 6-label head trained by
`scripts/finetune_triage.py`). Selection via `PORTER_TRIAGE_MODEL`
(`auto` default: finetuned-if-present → zeroshot → heuristic) and
`PORTER_TRIAGE_MODEL_PATH` (default `models/triage-v1`).

Rules stay the fast pre-filter AND the fallback: the model only ever
runs on rule-passed writes, scores are LRU-cached per content hash,
inference exceeding `PORTER_TRIAGE_TIMEOUT` (default 2s) falls back
to the heuristic for that write (audit-logged), and E2EE memories never
reach the model (ciphertext-blind, same as the rules). Expected CPU
latency for the zero-shot model: ~100–500ms per write (first load
downloads ~1GB of weights).

Normative rules:
- Queues are fail-closed: similarity/confidence gates cannot be forced by an
  attacker-controlled confidence value (the conflict gate ignores confidence).
- E2EE memories are NEVER auto-approved, auto-merged, content-scanned, or
  decayed (ciphertext-blind).
- `PORTER_APPROVAL_MODE=strict` restores v1 behavior: every write queues.
- Auto-merge folds content into the target (confidence = max, `updated_at`
  bumped, history chain kept) but does NOT union scope tags (T1: tags drive
  scoped reads).

### 7.2 Confidence decay (normative)

Active memories lose confidence while idle: `effective = stored × 0.95^(idle_days/30)`,
computed lazily on read (search/get). Below 0.2 a memory is auto-archived
(audited `auto_archive`, restorable). Retrieval resets the idle clock.
Pinned memories (`POST /memories/{id}/pin`, admin-only) and E2EE memories
never decay.

### 7.3 Auto-capture (normative)

The `memory_capture` MCP tool extracts durable facts (preferences, decisions,
project context) from a conversation transcript via the `porter/llm.py`
`extract_facts` protocol (regex stub by default; `PORTER_LLM_EXTRACTOR`
selects a real backend). Extracted facts are **untrusted data** and each one
passes through the same triage as `memory_add`. Bounds: ≤ 50k chars input,
≤ 10 facts per call, each fact burns one rate-limit token (T5).

### 7.4 Digest (normative)

`GET /api/v1/digest` (requires `memory.read`) returns `{pending_count,
pending_by_reason, recent_auto_approved, conflicts}`. Pending COUNTS are
visible to any reader; pending CONTENTS (previews, conflict text) are
admin-only (`memory.admin`) and tag-scoped — the same inbox visibility rule
as the sync feed.

## Conformance

A third-party implementation may claim **porter-compatible** for a spec module
iff:

1. **Scope enforcement is server-side.** Client-supplied tag hints are
   intersected with the token's allowlist; empty intersection returns zero rows
   (fail-closed), never an unrestricted query. `memory.admin` is the only scope
   that implies the others.
2. **The audit log is append-only.** Rows can be inserted and read, never
   updated or deleted (storage-level enforcement is required, not just
   application convention). `meta` carries ids/counts only — never memory
   content or query text.
3. **Untrusted-data marking.** Every memory returned to an agent is marked as
   untrusted input (`untrusted_data: true`) so downstream harnesses treat it as
   data, never instructions (PRD principle 5).
4. **Snapshot integrity.** Snapshots are verified (hash + signature) before
   any import, and imported memories land in the **pending** queue — never
   directly into active.

Implementations may implement `spec: scopes/v1`, `spec: audit/v1`,
`spec: snapshot/v1`, `spec: porter-export/v1`, `spec: sync/v1`, and
`spec: e2ee/v1` independently; a porter-compatible claim must name the exact
module set it satisfies (e.g. "porter-compatible: scopes/v1, audit/v1").

---

## Appendix A. Code-vs-PRD discrepancies (code wins)

1. **Snapshot layout.** PRD §9.1: `.porter/` directory (`manifest.json`,
   `memories.jsonl`, `skills/`, `tasks.json`, `preferences.json`,
   `signature.sig`). Code: single JSON file (`*.porter` encrypted /
   `*.porter.json` plaintext), skills/tasks as zero counts. This spec follows
   the code (§3.1).
2. **Error codes.** PRD §11 lists `duplicate_memory` and `snapshot_conflict`;
   `porter/errors.py` does not implement them. This spec documents only the
   implemented error codes (e.g. `invalid_scope`, `insufficient_scope`,
   `token_revoked`, `rate_limited`, `sync_not_available`).
3. **Sync endpoints.** PRD §9.5 describes the hub-and-spoke protocol; the code
   only reserves the stub (501 `sync_not_available`) and the `sync_cursors`
   table. This spec marks the protocol [spec-proposed].
4. **P2 E2EE.** PRD describes intent ("server 只存密文…去重/索引的密文友好方案
   另行设计"); nothing is implemented. This spec marks the envelope
   [spec-proposed] and normatively forbids server-side dedupe/search until that
   separate design exists.

## Appendix B. Changelog

- **1.0 (2026-10-11):** Initial release. Covers scopes/v1, audit/v1,
  snapshot/v1, porter-export/v1 as `[spec-implemented]` from the v1 reference
  implementation, and sync/v1, e2ee/v1 plus the `memory.share` scope as
  `[spec-proposed]` per the PRD roadmap.
