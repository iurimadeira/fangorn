# Deepen Fangorn's workspace architecture

Status: approved

## Problem statement

Fangorn already adopts, inspects, and lists worktrees safely, but callers must
assemble the lifecycle across the CLI, Git observation, generation-marker
storage, and SQLite Registry. That exposes ordering, retry, and mutation details
that only Fangorn should know. The publication gate also repeats wheel and source
distribution layout knowledge outside their archive readers, while the main CLI
test module owns unrelated lifecycle, Git, filesystem, SQLite, and presentation
behavior.

## Solution

Expose one deep `fangorn.workspaces.Workspaces` interface for Workspace lifecycle
behavior. It owns adoption, inspection, listing, immutable Binding enforcement,
authorization to establish missing identity, consistent observation, bounded
retry, and persistence coordination. Git remains a read-only adapter, generation
markers move to one private module, and Registry remains a private persistence
implementation.

Keep publication validation outside the installed runtime. Wheel and source
distribution functions adapt their native archives into one format-neutral
artifact representation before common publication policy runs. Reorganize tests
by the module or interface that owns each behavior.

## User stories

1. As a CLI user, I want `adopt`, `info`, and `list` to retain their current
   behavior and machine schemas, so that the architectural change does not break
   automation or existing Registry state.
2. As a Python caller, I want one Workspace interface, so that safe lifecycle
   behavior does not require knowledge of Git, marker, transaction, or retry
   ordering.
3. As a maintainer, I want generation storage, Git observation, Registry
   persistence, publication formats, and presentation to have clear ownership,
   so that failures and changes remain local.

## Behavior and acceptance

- `Workspaces.from_environment()` returns a lifecycle object using Fangorn's
  existing state-home resolution and Registry.
- `Workspaces.adopt(path)` adopts one existing Git worktree and returns an
  adoption result containing the Workspace and whether this call created it.
- Equivalent repeated or concurrent adoption converges on one Workspace and one
  immutable Binding; exactly one successful adoption reports creation.
- `Workspaces.inspect(path)` refreshes Current Git Facts only for an adopted
  Workspace. It does not adopt unknown worktrees or establish missing identity.
- `Workspaces.list()` returns deterministically ordered stored facts without
  reading Git or mutating marker or Registry state.
- A Workspace remains permanently bound to its original Repository and worktree
  generations. Missing, malformed, changed, symlinked, or replacement identity
  fails closed and never causes rebinding or silent repair.
- Adoption may establish identity only when lifecycle and Registry evidence prove
  that the Repository or worktree is unbound. A recorded missing generation is
  identity drift, not permission to recreate it.
- Git observation is read-only and continues to sanitize repository-local Git
  environment, require Git 2.31 or newer, preserve UTF-8 paths and refs, and use
  consistent observation.
- Generation establishment preserves the current marker names, exact payload,
  mode, directory-relative no-follow access, bounded lock, concurrent-winner
  convergence, atomic publication, durability, cleanup, and directory-swap
  rejection.
- Registry schema, existing state location, immutable constraints, observation
  ordering, contention deadline, and existing Registry data remain compatible.
- CLI commands, defaults, human output, `schema_version: 1` JSON/NDJSON records,
  stdout/stderr contract, safe single-line errors, and exit behavior remain
  compatible.
- Publication validation still requires exactly one version-matching wheel and
  one source distribution and applies the same metadata, license, notices,
  privacy, archive-safety, and installed-artifact checks to both.
- The full supported Python, lint, type, build, publication, and installed
  artifact checks remain green.

## Implementation decisions

- Add one public `src/fangorn/workspaces.py` module with a concrete `Workspaces`
  class and explicit `adopt`, `inspect`, and `list` methods. Do not add a generic
  command dispatcher, public dependency interfaces, or methods for unimplemented
  future operations.
- Domain results distinguish immutable Binding data from Current Git Facts.
  Serialization remains the CLI adapter's responsibility.
- Workspace failures are translated at the lifecycle interface and preserve
  native causes. Add only failure categories required by current behavior; do
  not predeclare a speculative error catalogue.
- Keep existing Git and Registry modules as implementation seams during this
  change; renaming or packaging them solely to signal privacy is unnecessary.
- Add one private `src/fangorn/_generations.py` module. Its closed interface reads
  or establishes Repository/worktree generations; marker paths, descriptors,
  locks, pending files, randomness, durability, and cleanup remain hidden.
- Remove marker-creation flags and marker mutation from Git observation.
  Workspaces decides whether establishment is authorized.
- Keep `scripts/check_publication.py` as one build-tool module. Private wheel and
  source-distribution adapter functions own filename, native archive reading,
  member rules, and required internal paths, returning one private
  format-neutral artifact value for shared validation.
- Do not add archive abstract classes, Protocols, registries, plugin loading, or
  runtime publication code.
- Keep tests flat and split only at actual ownership seams:
  `test_cli.py`, `test_workspaces.py`, `test_git.py`, `test_generations.py`,
  `test_registry.py`, and `test_publication.py`.

## Testing decisions

- `Workspaces` public interface — prove adoption, inspection, listing, immutable
  Binding, concurrency, transaction-bound final observation, and failure mapping
  with real temporary Git worktrees and SQLite Registries.
- Private generation interface — prove marker validation, no-follow behavior,
  atomic publication, durability, cleanup precedence, bounded contention,
  concurrent winner reuse, and directory-swap rejection with real temporary
  directories.
- Git adapter interface — prove read-only Git facts, environment isolation,
  canonical paths, stable observation, unborn/detached states, and failure
  translation with real temporary repositories.
- Registry implementation — prove schema defenses, migrations, causal ordering,
  contention, transaction semantics, and private filesystem state with real
  SQLite databases.
- CLI adapter — retain focused command, rendering, schema, terminal-safety, and
  joining-seam tests without duplicating the lifecycle behavior matrix.
- Publication gate — retain one test module with cross-format parameterization
  and narrow format-specific archive failures.

## Failure, compatibility, and rollout

This is an internal architectural migration with no Registry schema migration,
marker-format migration, CLI schema change, or release-format change. Existing
Registry files and markers must work unchanged. Partial successful generation
establishment remains durable progress and is reused on retry rather than rolled
back. Native Git, filesystem, SQLite, ZIP, and tar causes remain chained while
public errors stay concise and terminal-safe.

Implementation proceeds in independently green slices: extract generation
storage, introduce the deep Workspaces lifecycle and read-only Git observation,
deepen publication adapters, then relocate tests after their production owners
exist. Each slice preserves the currently supported public behavior.

## Out of scope

- Parent/child Workspace commands, persistence, tree navigation, or terminal UI.
- Workspace creation, removal, reparenting, merging, or ticket orchestration.
- Rebinding an existing Workspace to another worktree.
- Migration from private workspace tooling, private configuration integration,
  or related agent-launcher changes.
- New CLI output schemas or Registry schema versions.
- A third publication format, archive streaming, signatures, or deterministic
  build comparison.
- Generic dependency injection, filesystem adapters, archive plugins, or public
  extension registries.

## Further notes

The future parent/child model belongs to the same Workspaces lifecycle module,
but gains explicit methods only when its behavior is separately specified and
approved. The one-to-one immutable Workspace/worktree Binding remains invariant
regardless of parentage.
