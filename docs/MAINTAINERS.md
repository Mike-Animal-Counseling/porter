**English** | [中文](MAINTAINERS.zh-CN.md)

# Maintainer guide

Porter is an alpha project. Passing CI is evidence for a specific commit,
not completion of every product workflow. Keep the [acceptance record](ACCEPTANCE.md)
and [review report](REVIEW.md) honest about automated, manual and unverified
checks. The `initial push (1/13)` through `(13/13)` messages describe upload
batches, not completed milestones.

## Repository administration

These settings live on GitHub; adding YAML to a branch does not enable them.

- Keep the repository description and topics consistent with the README.
  Use a homepage only when there is a maintained public site; localhost is
  not a project homepage. The repository license is MIT and the package
  name is `porter-memory`.
- Keep private vulnerability reporting, dependency vulnerability alerts,
  automated security-fix proposals and secret push protection enabled.
  Review dependency changes; a proposed update is not a verified fix.
- Protect `main` with required pull requests and successful CI checks.
  Check names come from `.github/workflows/ci.yml`: `quality`, `package`,
  `test (ubuntu-latest)`, `test (windows-latest)`, `postgres`, `deployment`.
  Select the names actually reported by Actions. Require another reviewer
  when a second maintainer is available; avoid a review policy that blocks
  the sole maintainer from applying urgent fixes. Prepare the rule for
  `refs/heads/main`, review who can bypass it, and verify those six checks
  have run before activating it. Agree on a PR workflow with Muse or other
  automation first: requiring PRs will stop its existing direct pushes.
- Publish a private maintainer contact for conduct reports. Private security
  advisories are for security issues, not a substitute for conduct reporting.
  Do not invent an email address or promise a response time the project
  cannot sustain.

On 2026-10-20 the description/topics, private vulnerability reporting,
dependency alerts and automated security-fix proposals were configured.
Secret scanning and push protection were already enabled. `main` did not
yet have protection/rulesets; there was no published release. Re-check the
live settings when releasing; this is a dated record, not an ongoing guarantee.

## Review a change

Use Python 3.12 and install the lightweight developer environment from
[CONTRIBUTING](../CONTRIBUTING.md). From the repository root:

```bash
python -m ruff check .
python -m ruff format --check --quiet .
python scripts/check_repository.py
python -m pre_commit validate-config
python -m pip check
python -m pytest -q
python -m build
```

The repository checker uses only the standard library and performs no network
requests or writes. It checks UTF-8 Markdown, local file destinations with
case-sensitive spelling, bilingual sibling links, required community documents,
package metadata and unique migration numbers. It does not validate remote
URLs, Markdown fragments or the truth of prose.

The regular test run skips external-service tests unless their `PORTER_TEST_*`
variables are configured. A green unit run alone is not a deployment acceptance.
For changes to auth, persistence, migrations, containers or MCP, also run:

```bash
python scripts/verify_deployment.py
```

This creates the isolated `porter-review` Compose project using `.env.review`,
runs real PostgreSQL/API tests and checks persistence across API recreation.
It retains named volumes; `--keep-running` also leaves the services running.
For direct PostgreSQL tests use a dedicated database ending in `_test`, never
a user's production database. Do not remove user volumes to make a test pass.
Real BGE-M3 quality and real harness conversations are separate from the
hash-embedder infrastructure acceptance; record which one was exercised.

Known-advisory checks are separate from runtime acceptance. The dated
[dependency audit](DEPENDENCY_AUDIT.md) records the original findings, fixes
and full final OSV lookup, including its coverage limits. Refresh that record
after dependency changes; zero Dependabot alerts alone are not sufficient.

UI changes need desktop/mobile, keyboard, empty/error states and accessibility
checks. Client adapters need at least one real conversation in the intended
harness. Record browser/harness versions and limitations. Never publish real
memories, credentials, cookies or unredacted `.env` output in artifacts.

## Prepare a release

There is no automatic publishing workflow and no implied PyPI publication.
The Python distributions built by CI are review artifacts. A maintainer must
deliberately create a GitHub release after the checks below.

1. Choose a version and update `VERSION`, the single package version source.
   Check `porter.__version__` from the built/installed package. Keep the
   project marked alpha until the intended acceptance criteria are met.
2. Move the relevant `CHANGELOG` Unreleased entries into a dated version
   section in English and Chinese. Summarize resulting behavior, compatibility,
   migration requirements and known limits. Update README and acceptance
   links as needed; do not turn pending manual checks into passed checks.
3. Apply migrations both to a fresh database and to a backed-up previous
   version in an isolated environment. Preserve already shipped migration
   names; add a new migration for new behavior. Database rollback may require
   restoring a backup, not merely starting the previous image. Take a backup
   and test restoration before upgrading a real instance.
4. Build source and wheel distributions from the candidate commit. Verify the
   installed wheel outside the checkout, including migrations, panel assets
   and console commands, as the `package` CI job does. Inspect source archives
   for private data, credentials, `.env` files other than `.env.example`, model
   caches and local acceptance output. Run `pip check` with the tested constraints.
5. Confirm every required CI job passed on the exact candidate commit and
   complete the applicable [acceptance gates](ACCEPTANCE.md). A draft PR and
   successful config parsing do not establish a working harness conversation.
   Resolve security reports privately before publishing exploit details.
6. After review/merge, create an annotated `vX.Y.Z` tag at the accepted commit
   and a GitHub release pointing to that tag. Use a prerelease label for alpha
   releases. Include bilingual notes, upgrade/backup instructions, tested
   Python/Docker/client versions and known gaps. If attaching distributions,
   attach those built from that exact commit and include SHA-256 checksums.

Tagging, merging and publishing are separate deliberate actions. Neither the
checker nor the Docker acceptance helper performs them. If PyPI distribution
is introduced later, document package ownership, trusted publishing and
release provenance before adding a publishing workflow.

## After release

Test installation from the published artifact in a clean environment. Verify
the tag, release notes and download checksums match the accepted commit.
Triage bug reports with synthetic reproductions. Re-run the relevant checks
for fixes; retain explicit limitations rather than promising all integrations.
