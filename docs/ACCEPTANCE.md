**English** | [中文](ACCEPTANCE.zh-CN.md)

# Porter acceptance and release gates

This defines acceptance of the **self-hosted, single-owner alpha**: two chosen
AI tools can deliberately save and retrieve project context, owner review and
client restrictions work, and data survives service recreation. It does not
promise automatic conversation capture, complete team onboarding or support
verified in every tool named by a template.

## Candidate and existing evidence

Native-client checks completed on **2026-10-22**, starting from source baseline
`0b9e83d`, with the subsequent embedding error-handling fix tested in the working
tree. See the [sanitized native-client report](NATIVE_CLIENT_ACCEPTANCE.2026-10-22.md).
Candidate: review branch
`fix/porter-review-hardening`; use the final head and CI recorded on the PR.
[PR #1](https://github.com/Mike-Animal-Counseling/porter/pull/1) is a draft and
has not been merged into `main`. An upload label such as `13/13` describes a
commit batch, not acceptance or functional completion.

- The earlier full review recorded **650 local tests passing with no skips**, with real
  PostgreSQL and the isolated Docker stack configured. See the
  [review report](REVIEW.md) and executable
  [Postgres](../tests/test_postgres_integration.py) /
  [deployment](../tests/test_deployed_api.py) acceptance cases.
- Verified code baseline:
  [`08fb424`](https://github.com/Mike-Animal-Counseling/porter/commit/08fb424dadf7f316b5e539d0487c07691acea776)
  passed all six jobs in [CI run 39552038204](https://github.com/Mike-Animal-Counseling/porter/actions/runs/39552038204):
  quality, packaging, Linux tests, Windows tests, PostgreSQL and deployment.
  This evidence applies to that commit; the final candidate needs its own CI.
- Native Codex CLI **0.160.0** saved a synthetic project memory; a fresh native
  OpenCode **2.1.2** conversation actually searched and retrieved the same ID.
  Sensitive-record review, scoped denials, API recreation and independent
  credential revocation also passed. The follow-up embedding fix passed
  **40 focused tests**; that count is not a new full-suite result.
- On 2026-10-25, the official DSH Desktop bundled CLI **0.2.1-rc.3**, using saved
  DeepSeek account login, retrieved a new Codex memory and passed scoped denials,
  independent revocation and retrieval after API restart. See the
  [DSH report](DSH_ACCEPTANCE.2026-10-25.md). This PostgreSQL/hash run also records
  bounded browser checks; it does not replace semantic or full browser acceptance.
- On 2026-10-27, the retained PostgreSQL/BGE-M3 deployment was restored into
  separate Porter volumes. A fresh DSH Desktop GUI conversation retrieved the
  new native Codex memory; the dedicated reader's server audit independently
  confirmed the actual MCP read. Scoped denials, signed encrypted snapshot
  recovery, API recreation and database/embedder restarts passed. See the
  [formal recovery report](FORMAL_RESTORE.2026-10-27.md).
- The application source tested in that recovery, `fd5f2ff`, passed all six jobs
  in [CI run 40275772180](https://github.com/Mike-Animal-Counseling/porter/actions/runs/40275772180).
  Later documentation commits still need their own CI status.
- Historical baseline: all six jobs passed at `c3c5fdf` in [CI run 39385023666](https://github.com/Mike-Animal-Counseling/porter/actions/runs/39385023666):
  quality, packaging, Linux tests, Windows tests, PostgreSQL and deployment.
- Fourteen migrations were exercised on fresh PostgreSQL, with legacy-name
  adoption and interrupted-merge rollback. OAuth, independent HTTP/stdio MCP
  clients, encrypted snapshot restore, revocation and API recreation were tested.
- Real CPU BGE-M3 passed the service flow. Its
  [small bilingual evaluation](embedding-eval.2026-10-18.json) is evidence for
  those fixtures, not a general retrieval-quality guarantee.

These results belong to the tested commits and environment. After changes,
maintainers must attach current CI and acceptance results to the actual release
candidate; this document does not make an untested head pass.

## P0: gates for alpha sign-off

| Gate | Required result | Evidence / current state | Responsible |
|---|---|---|---|
| Reproducible candidate | Commit identified; necessary CI jobs pass on that commit | Final head and six-job CI results are recorded on PR #1; verify the candidate matches | Maintainer |
| Installation and persistence | Docker boots; migrations preserve existing data; memories, credentials and settings survive API recreation | Automated cases passed; formal BGE-M3 recovery verified an old record remained, then synthetic records and unchanged test credentials survived API/database/embedder restarts | Maintainer |
| Review and access boundaries | Pending records are not searchable; approved records obey scopes; revoke blocks the intended client | Automated checks and native Codex/OpenCode synthetic review, private-ID and denied-write checks passed | Maintainer |
| Safe memory changes and backups | Compound merges roll back on failure; snapshot tampering fails; encrypted restore queues content for review | Automated repository/Postgres/snapshot cases passed | Maintainer |
| Real cross-tool use | A writes; a fresh conversation in B searches and receives the same active ID; revoking a separate same-scope credential preserves A/B | **Passed: Codex + OpenCode; Codex + DSH bundled CLI; formal BGE-M3 Codex + DSH GUI search**. Independent revocation was covered by the dated native CLI runs, not repeated in this GUI check | Maintainer, with the chosen tools |
| Browser interaction | Key pages usable on desktop and narrow screen; keyboard focus, review, copy and download work | HTTP/HTML and the dated 600px browser flow passed; **full desktop/mobile, touch and visual acceptance open** | User; maintainer fixes failures |
| Accurate public handoff | README, deployment, security, repository metadata and release notes match implemented behavior and remaining limits | Bilingual review and repository checks passed; public metadata and security settings verified | Maintainer |

The selected native-client checks are complete; the user does not need to repeat
them or evaluate optional models. Browser interaction and the owner's decision
that the workflow is useful remain open. Follow the
[short remaining checklist](USER_SETUP_CHECKLIST.md). Only the selected versions
and explicit operations are verified compatibility claims.

## What the user needs to do

1. Use the panel on desktop and at narrow width: check the test memory, language,
   keyboard navigation and Handoff copy/download.
2. Record whether it is usable for your own workflow and any concrete blocker.
   The [checklist](USER_SETUP_CHECKLIST.md) also retains repeatable client steps
   for a different deployment; they are not extra work for this completed run.

The retained maintainer preview uses `127.0.0.1:18010`; a new standard deployment
uses `127.0.0.1:8000`. The preview and its private credentials are local review
artifacts, not a publicly hosted demo.

## Maintainer checks before publishing

- Record the final commit and inspect CI for that exact head. Run the repository
  checks in [CONTRIBUTING.md](../CONTRIBUTING.md); ensure the PostgreSQL and Docker
  jobs ran, rather than counting skipped opt-in tests as coverage. The isolated
  deployment runner is `python scripts/verify_deployment.py`.
- Resolve P0 failures and append the human sign-off below. A functional blocker,
  unauthorized disclosure or lost record leaves the relevant gate open.
- Review README, English/Chinese instructions, description, topics, license,
  contribution/security policy and changelog. Keep credentials, user data, model
  caches and local reports with secrets out of source and built artifacts.
- Confirm the support matrix distinguishes templates from verified clients.
  Keep alpha limits and known issues visible in release notes.
- Merge and publish only after review, preserving the evidence and commit link.
  A green PR is not a published release; the current PR remains a draft until
  the missing gates are closed.

## Known limits and work after alpha

| Area | Current boundary | Required before alpha? |
|---|---|---|
| Client automation | Tools and sync feed exist; templates do not schedule capture or inject context | Document clearly; a full adapter is separate work |
| Other clients | Templates do not prove per-client conversations; dated Codex/OpenCode and Codex/DSH flows are verified | Only claim the versions and operations actually accepted |
| Workspaces and device keys | Role/store foundations exist; complete membership onboarding and delegated key UX remain unfinished | No, if alpha stays single-owner and is described that way |
| Extraction | Default regex extractor; optional Ollama proposes reviewable facts; OpenAI extraction unimplemented | No; manual `memory_add` works without it |
| Retrieval quality | Small BGE-M3 fixtures tested; no guarantee on arbitrary private corpora or all languages | User judges the synthetic flow; broader evaluation follows |
| Public hosting / scale | Bundled setup binds localhost; multi-worker rate limiting and production operations need further design | No public multi-tenant claim |

## Sign-off record

Copy this into the candidate PR or keep it in your own acceptance notes. Never
include token values, passwords or real personal memories.

```text
Candidate commit:
Date:
Deployment URL/port (local only):
Tool A + version / transport:
Tool B + version / transport:
Project memory ID / private synthetic memory ID:
Fresh B search returns project ID: pass / fail
Private synthetic ID inaccessible: pass / fail
Revoke disposable same-scope credential blocks it and preserves A/B: pass / fail
Browser + desktop/narrow widths:
Review / language persistence / handoff copy-download / keyboard: pass / fail
Open issues (redacted steps):
Decision: accept this single-owner alpha / fixes required
Reviewer:
```
