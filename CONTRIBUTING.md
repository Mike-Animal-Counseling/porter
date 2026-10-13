<p align="center">
  <a href="CONTRIBUTING.md"><b>English</b></a> &nbsp;|&nbsp; <a href="CONTRIBUTING.zh-CN.md">中文</a>
</p>

# Contributing to Porter

Thanks for wanting to help. Porter is early and the surface area is small on purpose — good contributions are focused ones.

## Ground rules

- **Two memory domains.** Every feature should know which domain it serves: **personal** (sensitivity-gated, human-reviewed) or **project** (the agent's working memory for a project, portable across harnesses). If your change blurs them, say so in the PR.
- **Portability is the point, sensitivity is secondary.** The default is that things flow automatically; the review queue keeps junk out, not you out.
- **English-first, bilingual docs.** Every doc ships as an EN main file plus a `*.zh-CN.md` sister file, linked at the top. Code comments in English.
- **No junk in the repo.** No `.venv`, `__pycache__`, `.env`, snapshots, or temp files. Check `.gitignore` before committing.

## Development setup

```bash
git clone https://github.com/Mike-Animal-Counseling/porter.git && cd porter
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q          # full suite, must be green
```

The test suite runs against SQLite by default and **stubs out** heavy optional deps (`transformers`, `torch`, BGE-M3/Qwen3). You don't need a GPU or model downloads to contribute. Docker/Postgres paths are covered by `docker compose up` (see `docs/USER_SETUP_CHECKLIST.md`).

## Workflow

1. Open an issue first for anything non-trivial — a paragraph on *why* beats a surprise PR.
2. Branch from `main`: `feat/<short-name>` or `fix/<short-name>`.
3. Keep PRs focused; one concern per PR.
4. Every PR must:
   - keep `python -m pytest -q` green (add tests for new behavior),
   - keep `python -m pyflakes porter scripts` clean,
   - update docs in **both** EN and zh-CN (README / SPECS / DEVELOPER / CHANGELOG as applicable),
   - fill in the PR template, including the security notes section.

## Security contributions

Porter stores things people care about. If your change touches auth, OAuth scopes, crypto, E2EE, sync, or anything that could become a trust boundary, run the relevant PRD §14 (T1–T7) adversarial probes and say which ones in the PR. See `SECURITY.md` for how to report vulnerabilities — **never file a public issue for a suspected vulnerability**.

## Code of conduct

Be kind, be direct, assume good intent. Details in `CODE_OF_CONDUCT.md`.
