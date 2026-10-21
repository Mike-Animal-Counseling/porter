## What

<!-- One-paragraph summary of the change. -->

## Why

<!-- The problem this solves, or the issue it closes. Closes #NNN -->

## How it was verified

- [ ] `python -m pytest -q` passes
- [ ] `python -m ruff check .` passes
- [ ] `python -m ruff format --check --quiet .` passes
- [ ] `python scripts/check_repository.py` passes
- [ ] `python -m build` succeeds (if packaging/dependencies changed)
- [ ] Relevant Postgres / Docker acceptance run (if persistence/auth/deployment changed)
- [ ] New behavior covered by tests (or: no new behavior)

<!-- Record the exact commands/results and skipped checks. For UI changes,
include desktop/mobile, keyboard and empty/error-state checks when available.
Use synthetic examples and redact credentials from logs/screenshots. -->

## Docs

<!-- Which docs were updated (README / SPECS / DEVELOPER / CHANGELOG), in both EN and zh-CN? -->

## Security notes

<!-- Anything touching auth, scopes, crypto, E2EE, or triage-as-security-boundary?
If yes, which T1–T7 probes were run? Explain data migration/rollback needs.
Never include secrets or memory plaintext in acceptance artifacts. -->
