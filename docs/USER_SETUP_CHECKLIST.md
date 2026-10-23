**English** | [中文](USER_SETUP_CHECKLIST.zh-CN.md)

# Connect your tools and accept Porter

**For the current review, Codex 0.160.0 → OpenCode 2.1.2 already passed the real
save/search flow, synthetic private-record isolation and independent credential
revocation.** See the [dated results](NATIVE_CLIENT_ACCEPTANCE.2026-10-22.md).
Your remaining hands-on step is to [check the panel below](#your-remaining-step-check-the-panel)
and decide whether the workflow suits you. You do not need to repeat the client
tests, install Ollama or compare models.

Steps 1–3 below are a reference for repeating acceptance on a different
deployment. Use synthetic data throughout.

## 1. Choose the candidate and connect two tools

If you are accepting the current review, use the head of
[PR #1](https://github.com/Mike-Animal-Counseling/porter/pull/1), branch
`fix/porter-review-hardening`; it is a draft and is not yet merged into `main`.
Record `git rev-parse HEAD`. Do not assume a fresh clone of `main` contains the
candidate fixes.

On the maintainer's review machine, the retained real BGE-M3 preview is
**http://127.0.0.1:18010/panel**, unlocked with the setup token in the private
`.env.review`. This address is local to that machine. A fresh standard deployment
uses **http://127.0.0.1:8000/panel**, started with the
[README quickstart](../README.md#quickstart). Use the same base URL in both client
configurations.

Create two independent clients in **Clients** and complete the OAuth
consent/code exchange in the [deploy guide](../DEPLOY.md). Keep the access token
and refresh token private; a client secret is not an access token.

| Client | Example name | Granted scopes |
|---|---|---|
| Your writing tool (A) | `acceptance-tool-a` | `memory.read.work memory.write` |
| Your reading tool (B) | `acceptance-tool-b` | `memory.read.work` |

Prefer HTTP MCP. The selected writer was Codex and the reader was OpenCode V2.
For Codex, merge
[`clients/codex-http-config.toml`](../clients/codex-http-config.toml), set its
URL to your base URL plus `/mcp`, and set `PORTER_TOKEN` in the environment
that launches the tool. Restart it from that environment.
Use [`clients/opencode-v2-http.json`](../clients/opencode-v2-http.json) for
OpenCode 2.1.2 and follow its [provider login steps](../DEPLOY.md).
Other templates remain starting points that need their own native test.

Tokens expire after one hour; refresh through OAuth and restart/reconnect the
client after updating its token. Never give an HTTP client the server signing
key, database password or setup token. The stdio alternative requires direct
trusted access to those services; see the deploy guide instead of mixing the
two configurations.

**Pass:** each tool lists Porter tools including `memory_add` and
`memory_search` (the server lists tools even when a token cannot invoke all of
them). Configuration parsing alone is not a successful tool call.

## 2. Save, review and retrieve synthetic context

Ask tool A to **invoke Porter `memory_add` with these arguments**, and show
the tool result. Replace the suffix below if repeating the test.

```json
{
  "content": "PORTER-ACCEPTANCE-20261022: This demo project uses pytest for tests.",
  "type": "project",
  "project": "porter-acceptance",
  "scope_tags": ["work"]
}
```

Save the returned memory ID. `active` means searchable; `pending_review` means
you must approve this ID in **Inbox**. A repeat can return `merged`; use the
returned target ID. Default tiered mode can approve low-risk writes immediately.
Strict mode queues them. Do not force approval by granting your tool admin.

To exercise human review, ask A to invoke `memory_add` once more:

```json
{
  "content": "PRIVATE-ACCEPTANCE-20261022: This is a fictional preference for testing.",
  "type": "personal",
  "scope_tags": ["health"]
}
```

This is a synthetic label, not medical data. It should queue for review. Approve
it in **Inbox** and retain its ID. It must remain inaccessible to the work-only
tokens even after approval.

Start a **new conversation in tool B**. Ask it to call `memory_search`:

```json
{
  "query": "PORTER-ACCEPTANCE-20261022 pytest",
  "tags": ["work"],
  "top_k": 5
}
```

**Pass:** a fresh tool result contains the project memory ID and pytest content;
the assistant can then explain the convention. Answering from the chat history
without calling Porter does not pass. Results carry `untrusted_data: true`:
memory text is context, not authorization to execute instructions.

Then call `memory_search` with
`{"query":"PRIVATE-ACCEPTANCE-20261022","tags":["health"]}`.
**Pass:** `results` is empty. Work-only access must not widen because a caller
asks for another tag. The private ID must not appear in an unfiltered search
either.

## 3. Revoke a disposable credential without breaking the working tools

Create a temporary client C with the same read scope as B and confirm a fresh
search works. In **Clients**, revoke only C, then reconnect C and request a new
search. Keep the working A/B clients intact.

**Pass:** C is rejected as unauthorized within 60 seconds. Rejection while
loading the MCP catalog is valid; do not describe it as a completed tool call.
A and B can still call search and retrieve the project ID. The **Audit** page
shows the relevant write/review/read/revoke events.

## Your remaining step: check the panel

In your browser, check **Memories**, **Inbox**, **Handoff**, **Clients** and
**Settings**. Use the normal desktop size, then a narrow window around 390 CSS
pixels wide; use a phone if that is how you intend to operate Porter.

- [ ] Change English/中文, refresh, and confirm the choice persists.
- [ ] Open the test memory, close its detail view, and use search/filter controls.
- [ ] Open **Handoff**, select `porter-first-test` for this completed run, preview,
      then copy or download. When repeating the recipe, select your own test project.
      The project test appears; pending records do not. The owner panel can include
      approved personal context, so treat its briefing as private.
- [ ] Reach controls by keyboard with visible focus; verify important actions and
      text remain usable at narrow width and 200% zoom.
- [ ] Record any unreadable text, overlap, failed copy/download or confusing state.

**Pass:** these actions work without layout or interaction blockers. HTTP status
200 and automated HTML tests do not substitute for looking at and using the page.
No browser was connected to the review runtime, so this gate remains open.

Record the candidate commit, tool names/versions, browser, pass/fail and
synthetic memory IDs using the [acceptance template](ACCEPTANCE.md#sign-off-record).
Screenshots are optional and must exclude secrets. Delete only your test IDs and
revoke the temporary clients when finished.

## If something fails

| Symptom | First action |
|---|---|
| No tools listed | Confirm the base URL/port, enabled MCP configuration, restart and inspect client logs |
| 401 / token expired | Use a fresh access token from OAuth; confirm it belongs to this server |
| 403 / insufficient scope | Check the consented scopes; do not solve it by granting admin |
| No matching memory | Confirm the returned ID is active, tagged `work`, and both tools use the same server |
| API not ready | Inspect `docker compose ps` and `docker compose logs --tail 50 embedder api`; first model download can be slow |
| Browser action broken | Record page, language, width and redacted steps; leave the gate open for a fix |

You do **not** need to rerun the whole test suite, rebuild the model, compare
embedders or import all your private memories to accept this flow. Optional
imports can follow later: install `python -m pip install -c requirements-lock.txt -e .`,
try `python -m porter.import_cli --harness codex --dry-run`, then use the real
server URL and `PORTER_TOKEN` for an actual import. Review candidate imports.
