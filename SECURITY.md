# Security Policy — Porter v0.1.0

Porter stores your personal AI memories. Security is a first-class
requirement, not a backlog item: every build phase (W1, W2, panel, MCP)
was followed by an adversarial code + security review against the threat
model below, and all P0/P1 findings were fixed before release
(see CHANGELOG.md).

## Threat model (PRD §14) and mitigations as implemented

| # | Threat | Mitigation (v0.1.0) |
|---|---|---|
| T1 | Malicious / compromised client reads memories beyond its scope | Scopes enforced **server-side** on every request (`require_scopes`); `memory.read.<tag>` filters at the repository layer, **fail-closed** (empty scope∩tag intersection → empty result, audited). Client-supplied `tags` can never widen scope. The OAuth consent POST has a same-origin CSRF guard (`porter/origin.py`) so a malicious website cannot forge an approval and escalate scopes; permissive CORS is removed entirely. Verified by adversarial probes. |
| T2 | Token theft / replay | Access tokens are HS256 JWTs, 1h TTL, `alg` pinned (algorithm-confusion probes → 401). Refresh tokens are opaque, rotated on use; **reuse after the grace window revokes ALL of the client's tokens** and emits a `security` audit event. Revocation via jti blacklist — revoked tokens 401 well inside the 60s budget (US3). |
| T3 | Memory content poisons a downstream agent (prompt injection) | Every retrieval path marks results `untrusted_data: true`; MCP tool descriptions instruct the model to treat returned content as data, never instructions. Writes go through the pending-review inbox (human gate). |
| T4 | Local DB stolen / server compromised | Self-hosted: physical-machine security is the operator's responsibility (documented in DEPLOY.md). Optional at-rest posture: bind localhost, encrypted backups (operator-managed). `.env.example` contains placeholders only; no telemetry, no external calls by default. |
| T5 | Malicious client floods writes | `memory.write` only reaches the pending inbox (never auto-published); rate limit 60 req/min/client (in-memory token bucket, keyed on **verified** client identity — a forged-`sub` bypass found in review was fixed); panel one-click client ban. |
| T6 | Snapshot file tampered before apply | Ed25519 signatures verified on `inspect`/`apply` — fail closed on mismatch. Instance keypair (0o600); signature travels inside the file for cross-machine verification. Trust-on-first-use: any *valid* key verifies (your machines each have their own key); legacy unsigned files are accepted only with an explicit warning, and applied content always lands in the pending inbox (human gate), never directly into active memory. Dry-run listing shown before any write; optional AES-256-GCM encryption with user password. |
| T7 | Audit log tampered to hide reads | `audit_log` is append-only: Postgres trigger forbids UPDATE/DELETE (defense in depth — an app-layer bug can't erase it); the app has no update/delete path on the table. Deletion events keep metadata only, never content (US5). Approvals are audited as visibility transitions. |

## Trust assumptions (read before deploying)

1. **Network access ≈ owner (v1).** The consent screen and panel have no
   login in v0.1.0. The server **binds `127.0.0.1` by default** and the
   very first client registration requires a one-time setup token
   (`X-Porter-Setup-Token`, auto-generated per process unless
   `PORTER_SETUP_TOKEN` is set). Do not expose the API port to an
   untrusted network without adding authentication in front of it.
2. **JWT secret.** Set `PORTER_JWT_SECRET` in production. If unset, a
   random key is generated per process (loud warning at startup) and
   all tokens die on restart.
3. **Embedder choice is a privacy choice.** `EMBEDDER_KIND=openai`
   sends your memory text to OpenAI. Default (`hash` stub on small
   machines, `sentence-transformer`/BGE-M3 in Docker) keeps everything
   local.
4. **Rate limiter is per-process.** Multi-worker deployments need a
   shared store (Redis) — P2.

## What was verified vs. explicit residual gaps

**Verified in this environment** (89/89 tests + adversarial probes):
scope fail-closed behavior, JWT tampering/replay rejection, revocation
immediacy, refresh-reuse mass-revoke, rate-limit bypass closure,
audit append-only (in-memory mirror + trigger SQL inspection),
true-delete (row + vector gone, audit content-free), snapshot
signature verification, importer read-only behavior.

**NOT verified here — must be verified in CI / on first real deploy:**
- Migration SQL (`001`–`003`) and the audit trigger against a real
  `pgvector/pg16` Postgres (no Docker in the build environment; SQL
  validated with a parser + trigger logic mirrored in tests).
- BGE-M3 embedding quality/latency (build VM has <300 MB free RAM;
  interface + HTTP plumbing tested, model download deferred to Docker).
- Real harness handshakes (Codex/Zed/Cursor configs are written to the
  MCP spec but untested against the actual CLIs).
- `docker compose up` end-to-end (compose YAML validated, Dockerfiles
  reviewed, not executed here).

## Reporting a vulnerability

This is a personal self-hosted project. If you find a security issue:

- **Do not** open a public issue with exploit details.
- Contact: `[security contact — maintainer to fill in]` with a
  description and, if possible, a minimal reproduction.
- Aim: acknowledge within 7 days; fix + release + CHANGELOG entry.

## Hardening checklist for operators (see DEPLOY.md)

- [ ] `PORTER_JWT_SECRET` and `PORTER_SETUP_TOKEN` set to strong randoms
- [ ] `POSTGRES_PASSWORD` changed from the compose default
- [ ] Bound to localhost or behind authenticated reverse proxy + TLS
- [ ] Encrypted backups configured and restore tested
- [ ] `EMBEDDER_KIND` consciously chosen (local vs OpenAI)
