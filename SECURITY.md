**English** | [中文](SECURITY.zh-CN.md)

# Security policy

Porter is alpha software for self-hosted AI memory. Regression tests cover
authorization, persistence and cryptographic operations; they are not an
independent security audit or a production certification. Security fixes
currently target the latest `main` and its next release. Older versions do
not have a separate maintenance branch or a promised support period.

## Reporting a vulnerability

Do not post exploit details, private memories, access tokens or key material
in a public issue. Use [GitHub private vulnerability reporting](https://github.com/Mike-Animal-Counseling/porter/security/advisories/new)
when it is enabled. If unavailable, use a private contact listed on the
[maintainer's profile](https://github.com/Mike-Animal-Counseling), or request
a private reporting channel without describing the vulnerability publicly.
The project has no dedicated security mailbox or response-time guarantee.

Include the affected commit/version, deployment method, a minimal synthetic
reproduction, impact and a suggested fix if available. Coordinate disclosure
with the maintainer; a report is not permission to test other people's
instances. The [maintainer guide](docs/MAINTAINERS.md) explains the release
and repository settings that maintainers must verify.

## Threat model and implementation

| Threat | Controls and practical limits |
| --- | --- |
| T1: client reads outside its scope | HTTP and MCP verify tokens and enforce server-side read scopes. Tag-scoped retrieval, sync, handoff and writeback previews filter visible active records; client filters cannot widen access. Owner/admin credentials are trusted and can access the instance. |
| T2: token theft or replay | Access tokens are HS256 JWTs with a one-hour lifetime and a pinned algorithm. Refresh tokens rotate; reuse outside the grace window revokes the client's tokens. Revocation is checked on requests. TLS and client-side secret storage remain operator responsibilities. |
| T3: prompt injection in memory | Retrieved memory is marked `untrusted_data`; MCP descriptions instruct clients to treat it as data. This does not enforce a downstream model's behavior. In default tiered mode, eligible low-risk project writes can become active immediately or merge with near-duplicates. Personal, sensitive, conflicting, low-confidence and encrypted writes queue for review; Ollama-extracted facts always queue. Set `PORTER_APPROVAL_MODE=strict` to require review for all new writes. Heuristic sensitivity detection is not a confidentiality guarantee. |
| T4: stolen storage or compromised server | Memory content is plaintext on the server unless the client uses E2EE. E2EE hides content but leaves tags, type and timestamps visible; encrypted memories cannot be semantically searched. Optional password-encrypted snapshots protect exported content. Use OS/disk encryption, restricted file permissions and tested backups. The server remains a trusted component for ordinary memories. |
| T5: malicious writes or server-side file changes | Per-process rate limiting uses verified client identity; its default is 60 requests/minute/client. `memory.write` permits triaged writes and can auto-activate eligible records in tiered mode. Administrative merge/resolve and actual server-side writeback require `memory.admin`. Writeback defaults to preview; real writes use marked zones and backups. Do not grant write/admin access to an untrusted client. |
| T6: snapshot tampering or impersonation | Ed25519 verification rejects modified signed payloads, and AES-256-GCM detects changes to password-encrypted snapshots. The verification key travels inside the file and is not pinned to a trusted sender: replacing the complete file and its key is possible. A valid signature alone does not establish who sent it. Legacy unsigned files are accepted with a warning. Inspect and confirm imports; applied snapshots always enter review. |
| T7: audit records are erased | PostgreSQL triggers reject normal audit UPDATE/DELETE operations; deletion events retain metadata rather than memory text. This protects against ordinary application writes, not a database administrator or compromised storage. External log retention is an operator choice. |

## Deployment trust assumptions

1. **Owner access is powerful.** Native loopback connections are trusted.
   Browsers forwarded through Docker unlock `/owner/login` with
   `PORTER_SETUP_TOKEN`; the signed HttpOnly session lasts 12 hours.
   Protect the setup token, which also gates initial client registration.
   Forwarded headers cannot grant owner access.
2. **Network boundaries must be explicit.** Bundled Compose ports bind to
   loopback. Allowed hosts default to localhost, 127.0.0.1 and [::1]. Custom
   domains require an owner session even behind a local proxy. TLS proxies
   must preserve Host and use `PORTER_PUBLIC_ORIGIN`; bundled commands disable
   forwarded-header trust. Panel and OAuth mutations use same-origin checks.
   Do not expose an unprotected owner service to an untrusted network.
3. **Credentials must survive restarts.** Set strong, stable
   `PORTER_JWT_SECRET`, `PORTER_SETUP_TOKEN` and `POSTGRES_PASSWORD` values.
   An unset JWT secret generates a temporary process key and invalidates
   tokens on restart. Invalid storage modes and boolean flags fail startup.
   Keep `AUTH_ENABLED=true` outside deliberate local development.
4. **Model configuration affects privacy.** Hash and local BGE-M3 inference
   keep memory text local; first-time model downloads need network access.
   `EMBEDDER_KIND=openai` sends memory text to the configured provider.
   Ollama extraction is opt-in and sends supplied transcripts to the
   configured Ollama endpoint, which may be remote. Review its operator
   and retention policy before enabling it.
5. **Prefer scoped HTTP clients.** Streamable HTTP needs the client's own
   token. PostgreSQL stdio clients also need direct database access and the
   server signing secret, and therefore belong on the same trusted machine.
   Do not distribute those server credentials with client templates.
6. **Key handling still needs human care.** E2EE device/envelope operations
   require admin access. Private device keys belong on clients. POSIX key
   modes do not establish Windows ACL isolation; verify local OS permissions.
   Keep snapshot passwords and private keys out of source control and logs.
7. **Some protections are process-local.** Rate limits use an in-memory
   bucket. Multiple workers need a shared limiting mechanism; the bundled
   deployment uses one worker. No distributed rate-limit guarantee is made.

## Evidence and remaining acceptance

See [the review report](docs/REVIEW.md) for dated results and limitations.
Automated checks include scope denial, token tampering/revocation, refresh
reuse, real PostgreSQL audit-trigger rejection, destructive-delete behavior,
signed/encrypted snapshots, compound-operation rollback and Docker persistence.
Fourteen migrations have been exercised on fresh PostgreSQL databases and
with migration-name adoption for older review databases.

The installed Codex CLI parsed the supplied HTTP and stdio configurations;
this is not a real conversation/tool-execution test. Per-harness conversations,
real user imports, visual/browser acceptance, Windows ACLs and optional ML
quality still require the checks in [acceptance](docs/ACCEPTANCE.md).
Fixture-based model results do not establish general extraction or retrieval
quality. Team and encrypted-device APIs do not establish a fully accepted
multi-user onboarding experience.

## Operator checklist

- [ ] Strong, persistent credentials and authentication enabled.
- [ ] Loopback bindings or an explicitly configured authenticated TLS proxy.
- [ ] Least-privilege client scopes; review tiered versus strict approval.
- [ ] Deliberately selected embedding/extraction endpoints.
- [ ] Encrypted backups and a restore drill on a separate instance.
- [ ] Private key/file permissions checked on the host OS.
- [ ] Current version and acceptance evidence recorded before public exposure.

Deployment instructions: [DEPLOY.md](DEPLOY.md).
