**English** | [中文](NATIVE_CLIENT_ACCEPTANCE.2026-10-22.zh-CN.md)

# Native-client acceptance — 2026-10-22

**Result: the selected Codex → OpenCode HTTP flow, review boundary, scoped
denials and independent credential revocation passed. Browser visual and user
sign-off remain open.** These are explicit MCP calls, not automatic capture or
context injection.

## Environment and evidence boundary

| Item | Tested value |
|---|---|
| Source baseline | `0b9e83d`; the embedding error-handling fix described below was tested as a subsequent working-tree change |
| Writer A | Native Codex CLI **0.160.0**, HTTP MCP |
| Reader B | Native OpenCode **2.1.2**, HTTP MCP, model `openai/gpt-6.1-sol` |
| Disposable reader C | Separate credential with the same `memory.read.work` scope as B; used only for revocation |
| Porter deployment | Local review service on port **18010**, persistent PostgreSQL and the existing CPU BGE-M3 embedder |
| Provider authentication | OpenCode's saved ChatGPT OAuth; `OPENAI_API_KEY` removed from each OpenCode test child; Codex used its existing signed-in CLI account |

The normal new-deployment port remains **8000**. The review address is local to
the review machine. No credentials or private local-state files are included
here. The two memory IDs below belong to synthetic acceptance data.

The final commit and its CI must be recorded on the candidate PR. This report
does not assign a later untested commit the results of an earlier baseline.

## Completed native flows

| Check | Observed result | Outcome |
|---|---|---|
| Codex saves project context | Actual `memory_add` completed; returned active project ID `c22da059-240e-4bbf-b59b-30e6001f7b1e` | Pass |
| Fresh OpenCode conversation retrieves it | Actual `porter_memory_search` completed and returned that exact project ID | Pass |
| Sensitive candidate queues | Codex created synthetic personal/health ID `613fa002-39ba-4400-8366-73c91d5faba1`; initial status was `pending_review` | Pass |
| Owner approves exact candidate | Owner approved that exact ID through the panel's HTTP flow; the record became active | Pass for HTTP behavior; no visual claim |
| Work-only reader cannot request health data | Fresh native OpenCode search with the health tag returned zero results | Pass |
| Omitting tags does not widen access | Fresh native unfiltered search did not return the private synthetic ID | Pass |
| Knowing an ID does not disclose its record | Native contradictions lookup for the known private ID returned `porter:not_found` | Pass |
| Reader cannot write | Actual native `memory_add` returned `porter:insufficient_scope`; owner query found no denied-write marker in active, pending or archived records | Pass |

A successful call means the native client's tool event completed and its actual
output was inspected. Configuration parsing, an MCP `connected` label or the
model's own claim of success was not used as a substitute.

## Persistence and revocation controls

Both local API containers were recreated using their existing volumes. Health
checks returned 200; rollback containers were retained. After recreation,
disposable OpenCode C completed a fresh native search and retrieved the same
project ID.

Only C's credential was revoked. Its access token still had more than 300
seconds of validity, so expiration did not explain the rejection:

1. A real REST request was rejected with **401 / `token_revoked` in 0.14 seconds**.
2. A fresh native OpenCode process using C received HTTP 401 and
   `token_revoked` while connecting MCP. Its tool catalog did not load; it
   produced **no tool call or memory result**. This is a transport rejection,
   not a completed `memory_search` response.
3. Original Codex A then completed `memory_search` and received the same project
   ID; original OpenCode B did the same in another fresh conversation.

The original A/B configurations and credentials remained operational. These
controls verify that revoking the disposable credential blocks that credential
without disabling the other clients, and that the selected native flow still
works after API recreation.

## Fix discovered during acceptance

The initial native Codex save hit a cold BGE-M3 request timeout. A dedicated
`/embed` warm-up probe then returned a 1024-dimensional vector in 1.0 second,
and the retried native save succeeded. The fix improves failure reporting;
it does not remove cold-start latency.

HTTP embedding failures previously could escape as generic errors. The follow-up
fix maps embedding timeouts to a safe **504** Porter error and connection,
upstream HTTP or invalid-JSON failures to a safe **503** Porter error. It keeps
the existing timeouts and adds **no automatic retry**. The focused verification
recorded **40 passing tests**, including 22 new embedding cases.

The earlier full-suite record of 650 passing tests and its CI belong to the
earlier reviewed baseline; they are not a new full-suite result for this patch.
See [acceptance gates](ACCEPTANCE.md) for final-candidate checks.

## Remaining gate and limits

No browser was connected to the review runtime (`No browser is available`).
HTTP, HTML and static accessibility checks cannot prove layout, touch behavior,
focus movement or usability. The owner still needs the short
[desktop and narrow-screen check](USER_SETUP_CHECKLIST.md#your-remaining-step-check-the-panel).

This result covers these two versions and explicit HTTP MCP operations. It does
not certify other client templates, legacy OpenCode V1, automatic conversation
capture, automatic context injection, every model/account, public multi-tenant
hosting or retrieval quality on arbitrary private data. Optional Ollama,
ML triage, imports and model comparisons are not prerequisites for this result.
