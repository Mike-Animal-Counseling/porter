<p align="center">
  <a href="CONTRIBUTING.md"><b>English</b></a> &nbsp;|&nbsp; <a href="CONTRIBUTING.zh-CN.md">中文</a>
</p>

# Contributing to Porter

Thanks for wanting to help. Porter is early and the surface area is small on purpose — good contributions are focused ones.

## Ground rules

- **Two memory domains.** Every feature should know which domain it serves: **personal** (sensitivity-gated, human-reviewed) or **project** (the agent's working memory for a project, portable across harnesses). If your change blurs them, say so in the PR.
- **Make automation explicit.** Tiered mode can activate eligible project writes immediately; personal/sensitive/conflicting writes and Ollama-extracted facts require review. Strict mode queues all new writes. Preserve these boundaries and describe policy changes in the PR.
- **English-first, bilingual docs.** Every doc ships as an EN main file plus a `*.zh-CN.md` sister file, linked at the top. Code comments in English.
- **No junk in the repo.** No `.venv`, `__pycache__`, `.env`, snapshots, or temp files. Check `.gitignore` before committing.

## Development setup

```bash
git clone https://github.com/Mike-Animal-Counseling/porter.git && cd porter
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -c requirements-lock.txt -e ".[dev]"
python -m pre_commit install
python -m ruff check .
python -m ruff format --check --quiet .
python scripts/check_repository.py
python -m pytest -q
python -m build
```

Use Python 3.12 (the CI baseline in `.python-version`). The default tests use an **in-memory repository**, with heavy model backends stubbed. No GPU or model download is needed. The development extra contains only test, lint, hook and packaging tools; `.[ml]` is an explicit, large optional install. Docker uses CPU-only PyTorch by default.

Run `python scripts/verify_deployment.py` for the isolated Docker acceptance test (Compose 2.24.4+). It checks real Postgres migrations, OAuth, HTTP/stdio MCP, snapshots and persistence across API recreation. It uses `.env.review`, separate ports and the `porter-review` project; named volumes are retained. `--keep-running` leaves the panel available on port 18000. The normal test run skips external-service tests unless their `PORTER_TEST_*` variables are supplied.

Ruff configuration and pytest discovery live in `pyproject.toml`; `VERSION` is the package version source. Run `python -m ruff check . --fix` and `python -m ruff format .` to apply mechanical fixes. Hooks and CI use the same commands. CI also builds the source archive and wheel, then checks the installed wheel outside the checkout, including migrations and static assets.

`requirements-lock.txt` and `requirements-embedder-lock.txt` are tested version constraints, not hash-verified universal lockfiles. After a dependency update, resolve in a clean Python 3.12 environment, run `pip check`, the unit/integration tests and Docker acceptance, and refresh the relevant constraints. Never mark an unexecuted check as passed.

## Workflow

1. Open an issue first for anything non-trivial — a paragraph on *why* beats a surprise PR.
2. Branch from `main`: `feat/<short-name>` or `fix/<short-name>`.
3. Keep PRs focused; one concern per PR.
4. Every PR must:
   - keep `python -m pytest -q` green (add tests for new behavior),
   - keep `python -m ruff check .` and `python -m ruff format --check --quiet .` clean,
   - run `python scripts/check_repository.py` to catch local documentation links, bilingual pairs and packaging-contract mistakes,
   - update docs in **both** EN and zh-CN (README / SPECS / DEVELOPER / CHANGELOG as applicable),
   - fill in the PR template, including the security notes section.

## Security contributions

Porter stores things people care about. If your change touches auth, OAuth scopes, crypto, E2EE, sync, or anything that could become a trust boundary, run the relevant PRD §14 (T1–T7) adversarial probes and say which ones in the PR. See `SECURITY.md` for how to report vulnerabilities — **never file a public issue for a suspected vulnerability**.

## Code of conduct

Be kind, be direct, assume good intent. Details in `CODE_OF_CONDUCT.md`.

Release and repository administration are covered by the [maintainer guide](docs/MAINTAINERS.md).
Use the [acceptance gates](docs/ACCEPTANCE.md) to distinguish automated coverage from real-user validation.
