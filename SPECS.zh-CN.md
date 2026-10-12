[English](SPECS.md) | **中文**

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
  - `project`（可选）：harness 当前项目名。此时 delta 只携带该项目的
    工作记忆——带 `project:<name>` 标签的记录（归一化匹配）加上无标签/
    全局记录（个人域跟随用户到处走，见 §7.1.1）。省略则返回全量。这就是
    "行李跟人走"机制：项目 X 的 harness 看不到项目 Y 的工作记忆。这只是
    便利过滤，**不是**安全边界——OAuth 标签 scope（§1.3）先做可见性门控，
    同步内容一律标记 `untrusted_data`。
  - `op`: `create` | `approve` | `revise` (subject = the NEW record) |
    `delete`. `status` is the record's current status, `null` for delete
    tombstones (the row no longer exists).
  - `memory` is the full memory record, `null` for delete tombstones and for
    `create` of a still-pending record when the caller lacks `memory.admin`
    (inbox rule: unapproved content never leaks to adapters). Non-admin
    callers see only currently-active memories + delete tombstones.
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
| `op` | enum | yes | `create` \| `approve` \| `revise` \| `delete` |
| `status` | enum \| null | yes | record's current status; `null` for delete tombstones |
| `at` | ISO-8601 | yes | server time the change was committed |
| `actor_client_id` | string \| null | no | which client caused it (`clients.id`; null = owner/panel) |
| `memory` | object \| null | yes | full memory record (incl. `version`), or `null` per the inbox/tombstone rules in §5.1 |

### 5.3 Cursor semantics (normative)

- Cursors are **int64 sequence numbers**, strictly monotonic per server.
- `since` is exclusive: the response contains changes with `seq > since`.
- The response's `cursor` is the current feed **head** (not the newest
  delivered change): adapters persist it only after applying what they
  consumed, and ack via `POST /sync/cursor` (at-least-once delivery;
  adapters must be idempotent). Delta fetches never advance the stored
  cursor — only explicit acks do.

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

要求每次写入都人工点一次的记忆层，撑不过单用户规模。自动化层把人留在
真正需要判断的地方（敏感内容、冲突），其他地方让路——且不削弱 §14 的任
何保证。

### 7.1 分级审批（normative）

每次写入（`POST /api/v1/memories`、`memory_add`、面板 owner 写入、
`memory_capture` 事实）都经过 triage（`porter/triage.py`）：

| 条件 | 处置 |
|---|---|
| 个人域（`personal_domain`） | 排队（见 §7.1.1） |
| 敏感 scope 标签（归一化后） | 排队（`sensitive_tag`） |
| 敏感内容（NFKC 归一化 pattern 扫描，中英；基于内容而非标签） | 排队（`sensitive_content`） |
| ML 模型命中敏感类别 | 排队（`ml_sensitive:<类别>`） |
| 项目域但缺少 `project:<name>` 标签 | 排队（`missing_project_scope`） |
| 与同 project 标签的活跃记忆相似度在 [0.70, 0.92)（项目域） | 排队（`possible_conflict`） |
| 与活跃记忆最大相似度在 [0.70, 0.92)（个人/未知域） | 排队（`possible_conflict`） |
| 置信度 < 0.6 | 排队（`low_confidence`） |
| 与活跃记忆相似度在 [0.92, 0.97) | 排队（`duplicate_candidate`）——既有去重路径 |
| 与活跃记忆相似度 ≥ 0.97 | **自动合并**进目标 |
| 其他 | **自动批准**（`low_risk`），审计记录 |

#### 7.1.1 两个记忆域（normative）

Porter 存两类记忆，triage 区别对待。这是核心产品定位，不是分类器
的子功能。

- **个人域（personal）**：用户自己的记忆——偏好、健康、金融、PII、
  关系。敏感度标签（health / finance / pii / credentials）和人工审核
  队列用在这里。个人记忆永远排队人工审核（`personal_domain`），任何
  域的敏感度命中也一样。
- **项目域（project）**：agent harness 某个项目/工作区的**全部工作记
  忆**——"你从 Claude Code 切到 Codex/Cursor 继续做同一个项目时跟着你
  走的行李"。包括指令文件（AGENTS.md / CLAUDE.md / `.cursorrules` /
  WARP.md / GEMINI.md 只是例子——是项目域的来源之一，不是它的定义）、
  任务状态、做过的决定、代码库理解、踩过的坑、约定和正在进行的工作上
  下文。项目记忆默认自动批准（审计记录），两条护栏：
  - 必须带 `project:<name>` 标签（入库时从 repo 名或工作目录名派生）。
    无标签的项目记忆以 `missing_project_scope` 排队，等人工打标签；
  - 冲突检测**按项目限定**：新指令与**同一 project 标签**的已有记忆
    矛盾才并排排队；跨项目的相似不算冲突。
- 域按写入解析：显式 `type`（`personal` / `project`；v1 的
  `preference` 映射到 `personal`）优先，规则通过的写入再由 ML 给第二
  意见（见 §7.1.2）。解析不出（`unknown`）时按域模型之前默认处理：
  可自动批准，敏感/冲突/置信度门照常。

#### 7.1.2 ML triage 模型（informative）

`porter/triage_model.py` 在规则之上加了学来的第二意见。后端：
`heuristic`（包装既有规则 pattern，行为不变）、`zeroshot`
（HuggingFace `zero-shot-classification` +
`MoritzLaurer/mDeBERTa-v3-base-mnli-xnli`，约 280M 参数，中英多语言，
CPU 可跑；先判域再判 4 个敏感度假设，`multi_label=True`，阈值 0.65）、
`finetuned`（本地 `microsoft/mdeberta-v3-base` + 6 标签头，由
`scripts/finetune_triage.py` 训练）。`PORTER_TRIAGE_MODEL` 选择
（默认 `auto`：有 finetuned 用 finetuned → zeroshot → heuristic），
`PORTER_TRIAGE_MODEL_PATH`（默认 `models/triage-v1`）。

规则既是快速预过滤也是兜底：模型只跑在规则已通过的写入上；分数按内
容哈希做进程内 LRU 缓存；推理超过 `PORTER_TRIAGE_TIMEOUT`
（默认 2s）就回退到 heuristic（审计记录）；E2EE 记忆永不进模型
（密文盲，和规则一样）。zero-shot 模型 CPU 预期延迟：每次写入约
100–500ms（首次加载需下载约 1GB 权重）。

Normative 规则：
- 队列是 fail-closed：攻击者控制的 confidence 值无法强制绕过相似度/冲突门
  （冲突门忽略 confidence）。
- E2EE 记忆永不自动批准、自动合并、内容扫描或衰减（密文盲）。
- `PORTER_APPROVAL_MODE=strict` 恢复 v1 行为：每次写入都排队。
- 自动合并把内容并入目标（confidence 取最大，`updated_at` 更新，保留历史
  链），但不做标签并集（T1：标签驱动 scope 读取）。

### 7.2 置信度衰减（normative）

活跃记忆闲置时掉置信度：`effective = stored × 0.95^(idle_days/30)`，读取时
（search/get）懒计算。低于 0.2 自动归档（审计 `auto_archive`，可恢复）。
检索会重置闲置时钟。Pinned 记忆（`POST /memories/{id}/pin`，仅 admin）和
E2EE 记忆永不衰减。

### 7.3 自动捕获（normative）

`memory_capture` MCP 工具经 `porter/llm.py` 的 `extract_facts` 协议从对话
文本提取持久事实（偏好、决策、项目上下文；默认 regex stub，
`PORTER_LLM_EXTRACTOR` 可选真实后端）。提取的事实是**不可信数据**，每个都
走与 `memory_add` 相同的 triage。边界：输入 ≤ 50k 字符，每 call ≤ 10 个事
实，每个事实烧一条 rate-limit token（T5）。

### 7.4 摘要（normative）

`GET /api/v1/digest`（需 `memory.read`）返回 `{pending_count,
pending_by_reason, recent_auto_approved, conflicts}`。待确认**计数**对任何
reader 可见；待确认**内容**（预览、冲突文本）仅 admin（`memory.admin`）可
见且按 tag scope 过滤——与 sync feed 相同的 inbox 可见性规则。

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
