**English** | [中文](FORMAL_RESTORE.2026-10-27.zh-CN.md)

# Formal deployment recovery acceptance — 2026-10-27

**Passed: the retained Porter backup was restored into separate Docker volumes;
the existing BGE-M3 database and a new native Codex write are readable. Scoped
access, encrypted snapshot recovery, API recreation and database/embedder restart
also passed. A fresh official DSH Desktop GUI conversation retrieved the same
Codex-created ID through MCP, confirmed independently in the server audit.**

This report covers the local, single-owner deployment. It supplements the
[2026-10-25 DSH bundled-CLI report](DSH_ACCEPTANCE.2026-10-25.md), which used hash
embeddings. A successful HTTP request or configured Desktop plugin does not
establish that a DSH GUI conversation executed a tool.

## Environment and restored data

| Component | Tested configuration |
|---|---|
| Running source | `fd5f2ff0d139ce54f467990907ed1ee012078bd4` |
| Compose project | `porter-daily` |
| Local endpoints | API/panel `127.0.0.1:8000`; embedder `127.0.0.1:8001`; PostgreSQL `127.0.0.1:5432` |
| Restored database | Existing `porter_bge`, PostgreSQL + pgvector |
| Embeddings | Real CPU `BAAI/bge-m3`, 1024 dimensions |
| Native writer | Codex CLI 0.160.0, HTTP MCP |
| GUI reader | Official DSH Desktop 0.2.1-rc.3; DeepSeek-V4.1-Flash, High |
| Dedicated GUI reader | Only `memory.read.gui_c1e5fc45dc31` |
| Recovery isolation | Four Porter volumes restored into newly named volumes; the original full archive retained |

The complete archive's checksum was verified before restoration. Only the four
Porter volumes were selected; the recovery did not restore other projects.
The database initially contained **27 existing memory records**, before this
run's new synthetic fixtures. The earlier public synthetic ID
`c22da059-240e-4bbf-b59b-30e6001f7b1e` was still present.

All **14 current migration files** were recorded as applied. The migration
table also retained the historical alias `008_memory_domains.sql`, giving
**15 rows**; this does not mean 15 new migrations were introduced.

The tests used the running source listed above without modifying application
code. Operational configuration, credentials, raw logs, backup paths and
existing memory text remain private.

## New synthetic flow and access boundaries

| Fixture | Value |
|---|---|
| Project | `porter-gui-c1e5fc45dc31` |
| Query marker | `PORTER-GUI-20261027-c1e5fc45dc31` |
| Dedicated tag | `gui_c1e5fc45dc31` |
| Codex-created memory | `ce54bd24-9c47-49fd-a1a6-ba4ac2409226` |
| Snapshot-imported copy | `e200570a-e73e-4a07-956f-60c442af5e6f` |

Native Codex actually called MCP `memory_add`; the returned record was initially
`active`. A subsequent HTTP MCP read with the writer credential returned the
same ID and the `untrusted_data` flag.

The separate GUI-reader credential could read that synthetic record over HTTP.
An attempted write returned **403 / `insufficient_scope`**. A search constrained
to `work`, outside its granted tag, returned no records. These are server-side
credential checks, not evidence of a DSH GUI tool call.

## BGE-M3 checks

A real embedding request returned a 1024-dimensional vector with norm
approximately 1. The first measured short-text request took **5.38 seconds**;
this is an observed request latency, not the complete cold-service startup time.

The unchanged [small bilingual fixture](../scripts/eval_embeddings.py) contains
14 memories, 12 queries, 15 duplicate pairs and 10 distinct pairs:

| Metric | Result |
|---|---|
| Recall@1 | 7/12 (58.3%) |
| Recall@3 | 11/12 (91.7%) |
| Recall@5 | 12/12 (100%) |
| Duplicate precision at cosine 0.92 | 5/6 (83.3%) |
| Duplicate recall at cosine 0.92 | 5/15 (33.3%) |

These match the BGE-M3 results in the
[2026-10-18 baseline](embedding-eval.2026-10-18.json). No threshold or fixture
tuning was performed for this run. This small evaluation establishes that the
restored model behaves consistently on these fixtures; it is not a guarantee
of retrieval or duplicate detection quality for an arbitrary memory collection.

## Snapshots and persistence

The encrypted, signed snapshot selected only the new synthetic tag and contained
one record. A wrong password returned **422 / `invalid_input`**. A correct
dry-run reported `signature_ok: true`. Applying it created the copy listed above,
whose SQL status was **`pending`**; it was not silently activated.

After API `--force-recreate`, the original credential still retrieved the
synthetic record and the snapshot download's bytes were unchanged. API recovery
took **10.63 seconds** in this run.

The database and embedder were then restarted separately from the API
recreation check. They recovered in **27.71 seconds**. With unchanged
credentials, HTTP MCP still returned the expected synthetic ID, the GUI-reader
credential's write still returned 403, its `work` search was empty, and the
snapshot download's SHA-256 was unchanged. This verifies service restart
recovery; the user's computer was not rebooted.

At the idle observation, container memory use was API **73.45 MiB**, embedder
**734.2 MiB**, and database **30.89 MiB**. These are point-in-time values, not peak
or whole-machine requirements. All three showed no OOM event and restart count
zero; the configured restart policy was `unless-stopped`.

## DSH Desktop GUI acceptance

The Desktop profile has a private Porter MCP entry with the dedicated read-only
tag credential; the other existing plugins were preserved. A private one-click
refresh helper handles the one-hour access token. Its forced manual-refresh
path was exercised: it called `/oauth/token`, rotated the dedicated reader's
credentials and updated only its marker-delimited Desktop MCP block while
preserving the other plugins. With the refreshed token, HTTP read, denied write,
empty `work` search, snapshot-hash and writer HTTP MCP checks all passed.
No second GUI conversation was sent after refresh, so this does not establish
post-refresh GUI retrieval, automatic renewal or every future Desktop session.

GUI automation could not enter the prompt, so the user manually sent the
synthetic query in the fresh Desktop conversation. The prompt did not supply
the expected memory ID. The Desktop control tree independently showed the
matching ID and fictional content, and the user also pasted the result back.

More decisively, SQL audit inspection found exactly one MCP read for this
dedicated GUI-reader client:

```json
{
  "created_at": "2026-10-27T08:19:55.402947+00:00",
  "action": "read",
  "memory_ids": ["ce54bd24-9c47-49fd-a1a6-ba4ac2409226"],
  "meta": {
    "via": "mcp",
    "hits": 1,
    "tool": "memory_search",
    "top_k": 5,
    "wiki_boost": false
  }
}
```

No earlier MCP calls had used that reader credential. Earlier credential checks
used HTTP REST, while the separate writer credential performed the HTTP MCP
read. This separates the GUI's actual tool execution from setup probes. The
Desktop result also reported score `0.4492` and `untrusted_data: true`; those
are client-observed response fields, not fields verified by the SQL audit.

The actual GUI prompt used tags `["gui_c1e5fc45dc31", "project"]`, rather than
the originally suggested full `project:porter-gui-c1e5fc45dc31` tag. Server
scope filtering intersects requested tags with the credential's allowlist,
leaving the dedicated synthetic tag. **This verifies a tag-scoped GUI search,
not the full project-filter case.** The model's explanation that Porter rewrote
`project` into `project:<name>` was incorrect: `apply_tag_filter` only intersects
tags. The returned record already had its normalized project tag from the
writer's `project` parameter. No query-rewriting fix was made or required.

This GUI check establishes one explicit `memory_search`. It does not establish
automatic capture, automatic token refresh or GUI credential revocation.
Revocation was tested separately with the bundled CLI in the dated report.

No screenshot was retained: automatic approval review rejected a full-window
capture because it might include unrelated chats. The GUI control text and
independent server audit provide the evidence here without publishing private
chats or account details. Chrome-based checks were cancelled at the user's
request.

## Remaining gates

Still required for this expanded acceptance:

- Retain the open browser/mobile/accessibility scope and the owner's final
  usability decision in the [overall acceptance gates](ACCEPTANCE.md).

The [sanitized machine record](formal-restore.2026-10-27.json) distinguishes
completed checks from pending work. This recovery report does not merge the
draft PR, publish a release or confer acceptance on a later source revision.
