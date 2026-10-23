**English** | [中文](REPOSITORY_STANDARDS.zh-CN.md)

# Repository standards

Porter follows the engineering patterns of the maintained
[FastAPI full-stack template](https://github.com/fastapi/full-stack-fastapi-template):
one Python project definition, shared local/CI checks, optional development
tools, automated tests and container acceptance. Its React frontend and
SQLModel database stack are not prerequisites for these practices; Porter
retains its existing FastAPI panel and asyncpg repositories.

## Implemented checks

| Concern | Repository implementation |
| --- | --- |
| Packaging | PEP 621 metadata in `pyproject.toml`, version from `VERSION`, MIT license and project URLs; source archive and wheel include migrations and panel assets |
| Development | Lightweight `.[dev]`; explicit `.[ml]` model extra; Python 3.12 baseline |
| Style | Pinned Ruff lint/import sorting, Python modernization and formatting across application, scripts, Python client and tests |
| Local checks | Optional pre-commit hooks run the same Ruff commands as CI |
| Documentation | CI checks local link targets including filename case, bilingual pairs, project metadata and unique migration numbering |
| Cross-platform files | UTF-8/LF policy in `.editorconfig` and `.gitattributes`; runtime files ignored |
| CI | Read-only token, checkout credentials disabled, actions pinned by commit, job timeouts and cancellation of superseded runs |
| Verification | Windows/Linux tests, real Postgres migrations, isolated Docker OAuth/MCP/snapshot/persistence checks, installed-wheel smoke check |
| Dependencies | Tested core/model constraints, weekly Python/Actions and monthly Docker update proposals via Dependabot; no automatic merge |
| Runtime configuration | Exact host allowlist, explicit public origin for TLS proxies, consistent boolean flags and rejection of invalid storage modes |
| Contribution | Matching EN/zh-CN instructions, PR verification checklist and explicit private vulnerability reporting availability |

Configuration uses the official [Ruff documentation](https://docs.astral.sh/ruff/configuration/),
[Python packaging guidance](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)
and [Dependabot options](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference).
The constraints are not a hash-verified, cross-platform resolver lockfile.
Dependency updates require the checks in [CONTRIBUTING](../CONTRIBUTING.md).

These checks do not certify every harness integration or model quality.
Private vulnerability reporting, dependency alerts and automated security-fix
proposals are enabled on the public repository. Remote Actions verify the PR
head after each push. Branch protection still needs a coordinated PR workflow
with all contributors; it is not currently enforced. See the
[maintainer guide](MAINTAINERS.md), [dependency audit](DEPENDENCY_AUDIT.md)
and [third-party inventory](../THIRD_PARTY.md).
No mandatory whole-project type-check gate is claimed: it should be adopted
incrementally after the existing dynamic repository/SDK interfaces are typed.
