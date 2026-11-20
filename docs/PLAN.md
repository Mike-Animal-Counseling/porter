# Porter production roadmap

[中文](PLAN.zh-CN.md)

Porter should let Codex and Claude Code resume a task from the other client's verified findings, evidence and next step. The automatic-memory preview already covers project-scoped capture, review and recall with synthetic hook events. The production milestone requires real clients.

## Delivery sequence

1. **Native integration.** Reuse the existing hooks, MCP, OAuth and review inbox. Add connect, diagnose and disconnect flows that preserve existing hooks. Make capture and recall separately configurable per project. Refresh short-lived access tokens and use the OS credential store for durable credentials. Bound hook latency, and continue the conversation when Porter is offline. Run isolated Codex-to-Claude and Claude-to-Codex tests, including expiry, pause, disconnect and timeout. Verify Windows and Linux separately.
2. **Facts and retrieval.** Keep Postgres authoritative for facts, permissions and versions. Record source, project, revision, effective time and recorded time. Distinguish verified findings from hypotheses and next steps. Make event replay idempotent, commit approval and outbox writes together, and retry background indexing. Enforce scope, validity and revocation before ranking text and vector results. Include verifiable provenance. A correction or revocation must take effect on the next read.
3. **Optional graph index.** Shadow-index approved facts with Graphiti and revalidate graph candidates against Postgres before returning them. Evaluate FalkorDB Lite for embedded deployments and use an optional FalkorDB Compose profile if necessary. Kuzu is deprecated by Graphiti. Enable graph retrieval gradually only when its relationship or temporal answers improve and ordinary recall, cost and latency meet the agreed thresholds. Preserve legacy Wiki callers during migration and retain Postgres retrieval as the fallback.

## Release acceptance

- Demonstrate native two-way handoff, cross-project isolation, provenance, correction, revocation and continued operation during an outage.
- Compare the current build, the upgraded build and Memmy on the same scenarios. Record setup time, first-turn handoff success, manual context re-entry, recall, stale results, latency and cost. Proposed targets include Recall@5 at or above baseline, at least 50% fewer stale hits when the baseline is nonzero and at least 90% precision on 50 sampled extracted facts. Under a recorded workload, target recall p95 at or below one second and retain the 1.5-second timeout.
- Exercise queue backlogs, index retries, restore and graph rollback before widening the rollout. If authorization or stale facts regress, disable automatic recall or the affected graph path. Publish the tested compatibility matrix and rollback instructions.

Start with native handoff and diagnostics, then ship each stage in a separately verifiable change.

[Automatic-memory preview](https://github.com/Mike-Animal-Counseling/porter/pull/14) · [Memmy](https://github.com/MemTensor/memmy-agent) · [Graphiti backends](https://github.com/getzep/graphiti#installation)
