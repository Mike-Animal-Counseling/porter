**English** | [中文](THIRD_PARTY.zh-CN.md)

# Third-party components

Porter's own source is covered by [MIT](LICENSE). Dependencies retain their
respective licenses; installing Porter does not relicense those projects.

## Vendored browser code

| File | Upstream version | License |
|---|---|---|
| [porter/static/htmx.min.js](porter/static/htmx.min.js) | [htmx 2.0.4](https://github.com/bigskysoftware/htmx/tree/v2.0.4) | [Zero-Clause BSD (0BSD)](https://github.com/bigskysoftware/htmx/blob/v2.0.4/LICENSE) |

The checked-in file has SHA-256
`e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447`.
Verify this provenance when updating it, review upstream release notes and
licenses, and exercise panel navigation, forms, fragments and error handling.
No CDN request is needed to load this asset.

## Installed packages and model weights

Python dependencies are declared in [pyproject.toml](pyproject.toml) and the
[API requirements](api/requirements.txt), with separate
[model requirements](api/requirements-embedder.txt) and tested constraint files.
Their installed package metadata contains their own license information.
The [dependency audit](docs/dependency-audit.2026-10-20.json) records a
point-in-time vulnerability check; it is not a license audit or a guarantee
against future advisories.

BGE-M3 weights and optional Ollama models are downloaded separately. Check
the chosen model's upstream license and usage conditions before redistribution
or commercial deployment; the repository's MIT license does not cover weights.

The base container, PostgreSQL and pgvector also retain their own licenses.
This inventory documents what is bundled or installed; it is not a complete
software bill of materials for a particular deployment.
