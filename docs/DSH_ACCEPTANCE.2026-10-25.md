**English** | [中文](DSH_ACCEPTANCE.2026-10-25.zh-CN.md)

# DeepSeek Harness native acceptance — 2026-10-25

**Passed: Codex writes a synthetic memory; a fresh DeepSeek Harness conversation
retrieves the same ID. Scoped denials, independent credential revocation and
retrieval after restarting the API also passed.**

This run uses the official Desktop application's bundled CLI, rather than an
HTTP script standing in for the agent. It does not certify the Desktop GUI's
MCP setup, automatic capture or every Harness feature. The official application
files were not modified.

## Environment and scope

| Component | Tested configuration |
|---|---|
| Writer | Native Codex CLI 0.160.0, HTTP MCP |
| Reader | Official DeepSeek Harness Desktop bundled CLI 0.2.1-rc.3, dedicated headless profile |
| Model | `deepseek-account` / `deepseek-flash`, saved DeepSeek account authorization |
| Porter | MCP core baseline `4e63fe9` plus the candidate's UI/template corrections; isolated PostgreSQL + pgvector, local HTTP MCP |
| Embeddings | Deterministic 1024-dimensional `hash` embeddings |
| Reader permissions | Separate Porter credential with only `memory.read.work` |
| Model-provider keys | Inherited OpenAI, Anthropic and DeepSeek API keys removed from test children |

The dedicated headless profile explicitly enabled the managed credential,
authorization and DeepSeek account providers, which headless mode normally
disables. It selected the account model and mounted Porter's MCP client.
File, shell, plugin-management, web and subagent tools were disabled in that
test profile. A read-only sandbox and the server's scoped credential constrained
the test; this is not a claim of complete operating-system isolation.
Session-log contribution and feedback telemetry were disabled for these test
children without changing the user's Desktop preferences.

Hash embeddings keep this protocol and persistence test small. They do not
validate semantic retrieval quality. Earlier real BGE-M3 results remain dated
separately in the [existing acceptance record](ACCEPTANCE.md).

## Actual operations

All records were fictional fixtures. The test prompt supplied the query marker,
not the expected project memory ID; server-side MCP responses were checked
independently of the model's final answer.

| Check | Observed result |
|---|---|
| Codex `memory_add` | Active ID `5ca86983-0035-4c22-be5f-81001fb329f8` |
| Fresh DSH `memory_search` | Returned that exact active ID |
| Work-only DSH searches `health` | Decoded server response contained zero results |
| DSH requests a known private ID | `porter:not_found`; private fixture was active |
| Work-only DSH attempts `memory_add` | `porter:insufficient_scope`; direct PostgreSQL check found zero matching rows in any state |
| Disposable DSH reader before revocation | Retrieved the same project ID |
| Same reader after revocation | Native MCP connection received HTTP 401 `token_revoked`, before JWT expiry; no `tools/call` occurred |
| Original DSH reader after API restart | Fresh conversation retrieved the original ID with its unchanged credential |

The revoked-reader process exited zero after reporting its unavailable tool.
The revocation assertion therefore uses the actual HTTP denial and absence of
tool execution, not the process exit code.

The [sanitized machine-readable evidence](dsh-acceptance.2026-10-25.json) records
the actual MCP calls and server checks. Raw logs, grants, tokens, database
passwords and model-account details stay outside source and build artifacts.

## Porter browser checks

The local panel was also exercised in the in-app browser, with a 600-pixel-wide
screenshot: full-text search, memory detail, copy feedback, Escape closing the
drawer and returning focus to its trigger, Chinese language selection,
collapsed navigation, and Handoff preview/copy/download. The downloaded text
contained the actual project marker and Codex-to-DSH direction.

This found a styling omission: Handoff's three text inputs omitted `type="text"`,
so the shared text-input styles did not apply. Explicit types now make those
controls use the existing full-width form styling.

![Local Handoff acceptance using fictional data](../assets/dsh-handoff-acceptance.2026-10-25.png)

This is a bounded interaction check. A full desktop/mobile, touch,
screen-reader and cross-browser review, plus the owner's final sign-off, remain
open; the screenshot does not close those gates.

## Automated validation

- Final full local suite with real PostgreSQL and deployed HTTP/stdio services:
  **672 passed, 1 skipped**. It includes persistence, security/rollback, OAuth,
  HTTP/stdio MCP and encrypted snapshot checks.
- Panel, Handoff and accessibility regression cases: **73 passed** after the
  text-input styling correction; these cases are also included in the final suite.
- The optional Ollama model remains unconfigured; manual memory operations and
  the tested native flow do not require it.
- Ruff, formatting, dependency consistency, pre-commit configuration and local
  repository checks passed. The release candidate still needs CI for its exact
  committed head; historical CI is not a substitute.

## Connection configuration

For an already configured DSH profile, the MCP insertion is:

```yaml
- insert:
    - id: mcp-porter
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: porter
        transport: streamable-http
        url: http://127.0.0.1:18010/mcp
        headers:
          Authorization: !!js "'Bearer ' + process.env.PORTER_TOKEN"
        toolCallTimeoutMs: 60000
        failOnStartupError: true
```

Use the URL for your own deployment; standard Compose uses port 8000. Supply a
separate scoped Porter OAuth access token through the child environment and
keep it out of Git. Apply a private overlay with `--patch` to a dedicated
profile; do not replace an existing Desktop profile or assume a bare headless
profile inherits its account setup. Access-token renewal and per-client
configuration remain deployment responsibilities.
