**English** | [中文](DEPENDENCY_AUDIT.zh-CN.md)

# Dependency advisory check

On 2026-10-20, the exact Python package versions in both public constraints
files were queried against the official [OSV batch API](https://google.github.io/osv.dev/post-v1-querybatch/).
The final **94 unique package/version pairs returned no matching advisory
records**. This is a dated known-advisory check, not proof that the project
has no vulnerabilities.

The [machine-readable record](dependency-audit.2026-10-20.json) retains both
the original findings and the final `post_remediation` results. The original
96 pairs included two different versions each of `filelock` and `setuptools`;
aligning the core/model constraints reduced the final count to 94.

| Original finding | Final constraint change |
| --- | --- |
| `certifi 2026.7.22`: affected CA certificates, [GHSA-6q48-6c7w-9gw2](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2) | `certifi 2026.10.19` |
| Model `setuptools 78.1.0`: deprecated downloader path traversal, [CVE-2025-47273](https://github.com/pypa/setuptools/security/advisories/GHSA-5rjg-fvgr-3xxf) | `setuptools 84.0.0` in both constraints |
| Model `setuptools 78.1.0`: macOS source-package exclusion bypass, [CVE-2026-59890](https://github.com/pypa/setuptools/security/advisories/GHSA-h35f-9h28-mq5c) | `setuptools 84.0.0` in both constraints |
| Conflicting core/model pins for `filelock` and `setuptools` | `filelock 4.2.3` and `setuptools 84.0.0` in both constraints |

Six original OSV records represent three distinct advisories: the PYSEC
entries are aliases, not three additional vulnerabilities. Upstream advisory
pages and OSV differ in some patched-version fields; the record preserves
those differences and the final lookup rather than assuming they agree.

## Scope and limits

- Only public package names and exact versions were sent. Source code,
  environment variables, credentials and user memories were not submitted.
- All listed development, optional and platform-specific entries were queried;
  environment markers were not used to exclude packages. The final source
  hashes and complete queried list are recorded under `post_remediation`.
- Dependencies absent from the constraints, container operating-system
  packages, model weights and vendored browser libraries were not scanned.
- An advisory match does not prove a reachable exploit; an empty result does
  not prove safety. Installation compatibility, application tests, container
  builds and model acceptance require separate verification.
- GitHub Dependabot returned zero open alerts at both checks. Its default-branch
  indexing can lag and may not cover a local review branch; it did not replace
  the direct version queries.

No new auditing package was installed and no network-dependent CI gate was
introduced. Re-run a dated advisory check after dependency updates or before
a release; retain the results and re-run the relevant build/runtime checks.
See the [maintainer guide](MAINTAINERS.md) and [security policy](../SECURITY.md).
