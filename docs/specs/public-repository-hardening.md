# Harden Fangorn as a public repository

Status: approved

## Problem statement

Fangorn has a tested local implementation, but its public repository does not
yet contain the Python project, a stable required CI check, contribution and
security guidance, automated dependency and code scanning, or protections that
make external contributions safe to accept. The local publication gate also
allows private infrastructure identifiers that the public boundary forbids and
currently treats its own locally excluded review context as publishable source.

## Solution

Make the existing Fangorn bootstrap safe to publish and maintain as a public
Python project. Establish one stable aggregate CI contract, enforce the current
quality baseline with branch-aware subprocess coverage, add concise public
contribution, conduct, and confidential-reporting paths, strengthen publication
privacy checks, and enable repository security and merge settings only after
their required files and checks exist on the default branch.

## User stories

1. As a contributor, I want one documented Issue-first contribution path and
   deterministic local checks, so that I know when work is accepted and whether
   a pull request is ready.
2. As a maintainer, I want one stable required `CI` check, so that internal job
   layout can change without repeatedly changing branch protection.
3. As a user or security reporter, I want a confidential reporting path, so
   that vulnerabilities, sensitive diagnostics, and conduct incidents do not
   become public Issues.
4. As a maintainer, I want source and release artifacts checked for private
   data, expected project files, archive safety, and installed behavior, so that
   publication fails closed before private or malformed content leaves the
   repository.
5. As a repository administrator, I want dependency, code-scanning, merge, and
   branch settings to converge after the project reaches the default branch, so
   that automation protects development without deadlocking the first merge.

## Behavior and acceptance

- The development environment directly declares `coverage>=7.10,<8` and locks
  it.
- Coverage erases prior data, runs pytest with branch measurement and subprocess
  patching, includes `src` and `scripts`, combines subprocess data, reports the
  result, and fails below 85.0% combined coverage. The first committed baseline
  is at or above the threshold.
- Pytest rejects unknown configuration and markers.
- Ruff includes its security rules. Test assertions are the only broadly
  ignored security rule; intentional subprocess, executable lookup, and
  allowlisted SQL construction receive only narrow, justified suppressions.
- Strict mypy checks `src`, `scripts`, and `tests` and reports unreachable code.
- Pull requests run one Ubuntu/Python 3.12 `quality` job owning formatting,
  linting, typing, coverage, build, publication validation, and installed wheel
  and source-distribution smoke tests.
- Compatibility jobs run tests only on Ubuntu Python 3.13 and 3.14 and macOS
  Python 3.12, 3.13, and 3.14.
- Quality and aggregate CI jobs have a 15-minute timeout; compatibility jobs
  have a 25-minute timeout so the full macOS suite can finish. Superseded
  pull-request runs are cancelled without cancelling default-branch runs.
- One terminal job named exactly `CI` always runs after quality and
  compatibility and succeeds only when both dependencies finish successfully;
  a failed, cancelled, or skipped dependency cannot produce a green or missing
  terminal result.
- Every third-party GitHub Action reference is a full immutable commit SHA.
- The source tree contains concise `.github/SECURITY.md`, `CONTRIBUTING.md`,
  Contributor Covenant 2.1, structured bug and proposal forms, Issue-form
  configuration, and a pull-request template.
- Contribution guidance requires an accepted Issue before implementation,
  references deterministic checks, and requires sanitized public examples.
- Bug intake asks for minimal reproducible, sanitized diagnostics and never asks
  for complete environment dumps or log bundles.
- Security intake directs confidential reports to GitHub Private Vulnerability
  Reporting and rejects public security Issues. Until a dedicated private
  contact is justified, the same channel receives conduct reports.
- The publication gate requires the public project files, validates stable
  semantic markers, and scans source plus built artifacts for private paths,
  credentials, keys, tokens, and exact forbidden private infrastructure
  identifiers.
- Locally excluded review context is omitted only from untracked working-tree
  discovery. If such a path is tracked or included in an artifact, normal
  publication validation still rejects it.
- Public architecture and policy documents describe excluded private concerns
  generically and contain no private tool names, account-routing details,
  machine names, private network details, personal paths, credentials, or logs.
- After the aggregate `CI` check has run successfully, the default branch
  requires that exact check, requires branches to be current, and requires
  conversation resolution. Administrators remain subject to protection;
  force-push and deletion remain disabled.
- Pull requests remain mandatory with zero formal review approvals. For an
  administrator-authored pull request, the administrator's merge after green
  CI is the explicit human approval; impossible self-review is not required.
- After all workflow references pass the immutable-SHA audit, repository policy
  requires full-length Action SHAs.
- The repository accepts squash merges only, deletes merged branches
  automatically, and disables the unused wiki and Projects surfaces.
- After Python manifests and dependency configuration reach the default branch,
  grouped weekly Python and GitHub Actions updates are enabled. Dependency
  alerts are enabled before automated security updates.
- After Python source reaches the default branch, CodeQL default setup scans
  Python.
- After `.github/SECURITY.md` reaches the default branch, private vulnerability
  reporting is enabled and its public contact link resolves to that channel.
- Source checks, all supported Python tests, build validation, publication
  validation, installed-artifact smoke tests, and the integration pull request
  are green before merge. After merge, required-check protection is enabled and
  its exact API state is verified.

## Implementation decisions

- Use coverage.py directly. Do not add pytest-cov or an external coverage
  service.
- Keep one normal CI flow: `quality` and `compatibility` feed the stable `CI`
  result. Compatibility jobs do not repeat static, build, or publication work.
- Keep the existing publication checker and standard-library archive readers.
  Do not add another security scanner for rules already enforced there.
- Add only narrow Ruff suppressions at deliberate trust boundaries; do not
  disable subprocess or dynamic-SQL rules globally.
- Use the canonical Contributor Covenant 2.1 text and attribution without
  rewriting its policy body.
- Use GitHub-native Issue forms, Private Vulnerability Reporting, Dependabot,
  CodeQL default setup, branch protection, and repository settings.
- Sequence remote enforcement after prerequisites exist. Never require an
  unavailable check or enable a scanner before its language and configuration
  are present on the default branch.

## Testing decisions

- Installed `fangorn` CLI from the built wheel and source distribution — prove
  version/help behavior and that both artifact types are installable.
- Publication checker CLI — prove required files and markers, source and
  artifact privacy, archive safety, UTF-8 handling, exact private-identifier
  rejection, and that locally excluded untracked review context is skipped
  without exempting tracked or archived content.
- `coverage erase`, `coverage run --branch -m pytest`, `coverage combine`, and
  `coverage report --fail-under=85.0` — prove isolated measured test execution
  and the committed quality floor.
- Ruff, Ruff format, and strict mypy CLIs — prove the declared source, script,
  and test quality policy.
- GitHub Actions pull-request run — prove every matrix job and the stable `CI`
  aggregate before requiring it in branch protection.
- GitHub repository APIs — verify every approved branch, merge, dependency,
  scanning, and private-reporting setting after deployment.

## Failure, compatibility, and rollout

Existing `fangorn` CLI behavior, JSON/NDJSON schemas, Registry and generation
formats, supported Python versions, and built artifact formats remain
compatible. Hardening failures block publication or merge; they do not alter
runtime state.

Rollout is ordered to avoid a protection deadlock: land and observe the project
and aggregate CI first; then deploy and verify required checks and repository
settings. Dependency and code scanning wait for manifests and Python source on
the default branch. Confidential reporting waits for its public policy file.
Any failed remote-setting deployment stops, reports exact observed state, and
leaves already-safe protections enabled.

## Out of scope

- Package-index publication, Trusted Publishing, release workflows, tags, or a
  first release.
- External coverage hosting, coverage badges or artifacts.
- Additional scanners such as Bandit, Semgrep, Pyright, OpenSSF Scorecard, or
  dependency-review workflows.
- tox, nox, pre-commit, xdist, changelog automation, support policy, or
  ownership files.
- A generic plugin system, public extension registry, or new runtime behavior.
- Formal independent-review requirements for administrator-authored pull
  requests.

## Further notes

The public repository uses GitHub Issues as its accepted-work source of truth.
Repository hardening must be complete before moving public implementation work
from another tracker into that repository. Remote settings are a separate,
explicitly approved deployment after code integration and green CI.
