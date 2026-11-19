**English** | [中文](PROJECT_HANDOFF_ACCEPTANCE.zh-CN.md)

# Project handoff acceptance

The Windows native runs used Codex CLI 0.160.0 and the official DeepSeek
Harness bundled CLI 0.2.2-rc.1 with its DeepSeek account model. They used
isolated client settings, Windows Credential Manager, PostgreSQL 16 with
pgvector and Docker 27.4.0. All facts and file references were synthetic.

| Check | Observed result |
|---|---|
| Codex → DeepSeek Harness | Codex read a fixture file and left a verified conclusion, file and line, and next step. After approval, a fresh DSH conversation read the same record through MCP and quoted its result and evidence. |
| DeepSeek Harness → Codex | DSH submitted an operator-verified fixture and its evidence over the managed stdio adapter. After approval, a fresh Codex conversation used its first-turn Hook context to return the exact result and evidence. |
| Credential renewal and disconnect | An expired local credential deadline caused each client to rotate a genuine Porter refresh token in the system store. Disconnect removed the local credentials and the server rejected the revoked token. |
| Isolation and offline behavior | A fresh Codex session in a different directory obtained no fixture context. A session with an unreachable Porter server still finished its model turn. An existing Hook executed with the installed Porter Hook. |
| Correction and withdrawal | The next scoped API recall returned version 2 after correction. After deletion it returned neither version. |
| Clean Linux candidate installation | Debian Linux x64, Python 3.12, the native Codex 0.160.0 binary and GNOME Secret Service. A fresh Codex turn recovered the approved fixture and renewed its system-held token. |

DSH recall is an explicit MCP call on the first turn. Codex recall comes
through its `UserPromptSubmit` Hook. The Claude Code Hook adapter is covered
by synthetic checks. This acceptance used DSH in the live cross-client lane.
Linux DSH and the Desktop GUI are outside the tested live compatibility range.

## Performance and synthetic comparison

- Both native directions succeeded on the reader's first turn, with no manual
  restatement of the result. The final four model turns took 8.16–11.41 seconds
  each in the managed Windows adapter run. These are conversation timings.
- A separate 24-event serial replay per Windows configuration measured
  prompt Hook p95 of 641 ms for a plain path and 656 ms for a path containing
  spaces. These timings include starting the Hook process and reading
  PostgreSQL through the local API. The Hook's network deadline is 1.5 seconds.
- In the [synthetic retrieval suite](GRAPHITI_SHADOW.md), the old ranking
  query hit 16 expired records in 18 searches. Current Postgres hit zero.
  Memmy v1.2.6 Memory Service 2.1.7 ran the same nine questions twice over
  the same facts. Its Recall@5 was 0.3333 for ordinary questions, 0.0000 for
  relationship questions and 0.3333 for temporal questions. HTTP p95 was
  3,204 ms. It returned no withdrawn IDs and two candidates from the sibling
  project under the tested single-user namespace configuration. The Memmy
  comparison used its local HTTP service and an OpenAI embedding model, with
  automatic agent-source discovery disabled and a Windows file-flush startup
  workaround. These figures describe this fixture and configuration.
- The native-turn test used existing model accounts. It did not attribute
  per-turn billing or measure native first-turn behavior for the Memmy or
  legacy Porter comparator. The Graphiti report records observed token usage.

## Automated coverage

- A Windows source run reported 733 passed and four conditional skips.
  The isolated Docker verifier also passed real PostgreSQL and deployed API
  cases. Those cases cover idempotency, approval and its committed change,
  permission denial, versions, expiration, snapshots and API recreation.
- The upgrade rehearsal applied migration 015 over a backed-up 14-migration
  database. Restoring the backup into a new database preserved the original
  active memory ID and restored the 14-migration schema.
- See the separate [Graphiti shadow evaluation](GRAPHITI_SHADOW.md) for the
  synthetic ranker comparison. Graphiti does not serve production reads.

## Pause or roll back

Pause managed recall for each enrolled project with `porter-connect pause`
and the client and project arguments. Existing sessions may retain context
they already received. Use `disconnect` to revoke the project's credential,
then `uninstall` to remove the managed Hook or DSH overlay.

Take a PostgreSQL custom-format backup before upgrading and record the
current package, image and configuration. Freeze writes before switching
back. Restore the pre-upgrade backup into a separate database, verify the
migration count and expected active records, and point the previous package
or image at the restored database. Keep the newer database until any data
written after the backup has been reconciled. Migration 015 adds a capture
queue and does not have an automatic downgrade. Stopping a shadow Graphiti
run does not alter the database selection or the existing Wiki tools.
