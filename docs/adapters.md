# Service configuration and adapter API

Fangorn discovers installed distributions through the `fangorn.resource_adapters`
entry-point group. Configuration never installs packages. Missing adapters,
duplicate IDs, incompatible API majors, and unsupported Resource kinds reject
execution before Resource effects. Lifecycle mutations preflight before recording
their operation; preliminary create intent and repository preparation may precede
configuration resolution.

## Command Services

```toml
schema_version = 1

[services.app]
adapter = "fangorn.command"
adapter_api_major = 1
inspect = ["./hooks/app", "inspect"]
start = ["./hooks/app", "start"]
stop = ["./hooks/app", "stop"]
delete = ["./hooks/app", "delete"]
timeout = 60
environment = ["DATABASE_URL"]
```

Names are unique and ordered as declared. `worktree` is reserved. Adapter major
defaults to 1. `external_reference` is an optional opaque string. Command Services
require inspect/start/stop/delete argv; optional `create` provisions without
starting. Without create argv, declaration itself provisions an absent Service.
Every mutation reconciles against a fresh probe and verifies its result.

Commands use argv with `shell=False`. An explicit interpreter remains possible.
`cwd` defaults to the Workspace Worktree; an explicit path is resolved relative
to it. `timeout` is an integer from 1 to 3600 seconds, default 60, per invocation.
The supervisor captures at most 1 MiB per stream, terminates the process group
with TERM, allows two seconds of grace, then KILL and a bounded termination
check. It drains descendants even when the leader exits first. Captured hook
output never enters machine stdout. Unknown quiescence retains lease fencing;
it is never reported as a successful mutation.

The environment supplies PATH, C locale, and these immutable Resource values:
`FANGORN_WORKSPACE_ID`, `FANGORN_RESOURCE_NAME`, `FANGORN_RESOURCE_LOCATOR`, and
`FANGORN_OWNERSHIP_TOKEN`. `environment` contains additional variable names to
resolve at invocation, never their values. Missing references fail execution.
Hook output is not persisted in failure receipts.

The read-only inspect command must emit exactly this JSON object:

```json
{"schema_version":1,"status":"ready","locator":"RESOURCE_LOCATOR","ownership_token":"OWNERSHIP_TOKEN"}
```

Status is `absent`, `stopped`, `ready`, `degraded`, or `unknown`. Locator must
match `FANGORN_RESOURCE_LOCATOR`. Present observations must carry the matching
ownership token; absent observations require null. Malformed JSON, wrong versions,
foreign ownership, unsuccessful probes, and missing consent produce unknown
evidence. Probe authors must honor the read-only contract; consent is not OS
isolation.

## Snapshots and consent

The default manifest and direct scripts come from the resolved Git commit.
Explicit configuration uses its parent directory as source root. A relative
executable argv path containing `/` is directly covered. Interpreter arguments
must be listed explicitly, for example `scripts = ["./hooks/app.py"]` with
`start = ["python3", "./hooks/app.py", "start"]`.

Covered paths must remain beneath the source root, without symlink components,
and name regular files of at most 1 MiB. Verified bytes and mode are copied to
content-addressed snapshots in the XDG state directory. Configuration bytes,
direct script names, bytes, and modes determine the digest. New bytes or modes
require a new grant. With no scripts, the digest remains SHA-256 of exact
configuration bytes, preserving earlier definitions.

Absolute installed executables, interpreter strings, transitive files, containers,
and network dependencies are outside this digest claim. Local validation covers
local modes; Git snapshots use committed 0644/0755 modes. When these differ, use
the exact digest reported by create. Consent is stored separately under
`$XDG_STATE_HOME/fangorn/consent/` (default `~/.local/state/fangorn/consent/`).
Revocation applies before the next probe or mutation; reads never create grants.
Snapshots are under the sibling `snapshots/` directory. Neither lives in the
repository, and retries retain the original snapshot.

## Python adapter contract

An entry point resolves to an adapter object or zero-argument factory. Its
`descriptor` is `AdapterDescriptor(id, api_major, kinds, capabilities)` from
`fangorn.resource_adapters`. The ID is qualified and stable; API major is 1.
The five required methods accept `(definition, context)`: `create`, `inspect`,
`start`, `stop`, and `delete`. Definitions expose immutable name, kind, adapter
ID/major, configuration, opaque external reference, locator, and ownership token. Context supplies Workspace
and operation IDs, Worktree cwd, digest, consent store, verified script paths,
optional liveness descriptor, and the force request.

The built-in `fangorn.git-worktree` adapter requires `context.git` to contain a
frozen `GitWorktreeContext(repository, common_dir, common_generation, commit,
branch)`. The caller supplies the resolved repository path, canonical Git common
directory, previously established repository generation, resolved creation commit,
and creation branch. These values belong to immutable operation context; existing
Resource definitions do not gain configuration fields. The Resource locator must
equal `context.worktree`. Missing context produces an unknown observation or an
unsuccessful mutation with unsafe continuation. All mutations additionally require
the caller's live operation liveness descriptor; the caller keeps its invocation
alive until the operation and its supervised children settle.

Git create reconciles an owned interrupted create against the pinned commit and
branch. Inspection verifies current ownership and reports ready or proven absence;
it does not require HEAD or branch to remain at their creation values. Start
requires a present owned checkout. Stop verifies ownership or absence and preserves
the checkout. Git has no independently stopped process: aggregate lifecycle
evidence supplies stopped state, while a present checkout still probes ready.
Delete proves ownership, preserves dirty files unless `context.force` is true,
and verifies absence; force never bypasses ownership or submodule restrictions.
Repeated operations reconcile existing evidence. Git operations do not run command
Service hooks or require command consent. `Workspaces` retains its existing Git
effect path to persist the richer Git observation used by the F3 facade.

`inspect` returns `AdapterObservation(status, locator, ownership_token, error)`.
Mutations return `AdapterResult(success, error, continuation)`. Continuation is
`safe`, `unsafe`, or `unknown` for that exact attempt, default unknown. It never
overrides ownership or the Worktree deletion barrier. Mutation methods must be
idempotent reconcilers and must not return until their effects are quiescent.
Raise `GitQuiescenceError` from `fangorn.git` if quiescence cannot be proved;
retain the context liveness descriptor in supervision until all children stop.
Adapters are trusted installed Python code, must keep inspection read-only, and
must not print to machine stdout or persist resolved secrets.

Unexpected adapter exceptions become attributable failed steps; their exception
type is recorded without persisting arbitrary exception text. Explicit unknown
quiescence retains the mutation fence. Start and stop journal fresh verification
of every Resource after their effects. Delete journals a fresh Service absence
barrier before removing the Worktree. If that same delete attempt is interrupted
after Worktree removal, retry reconciles its absence using the durable barrier,
without replaying probes that require the removed cwd.

After successful deletion, inspection retains terminal `deleted` state and
freshly observes Worktree absence. Current Service observations and aggregate
observation remain `unknown`: their cwd no longer exists. The response labels
successful Service absence receipts as historical `completed_delete` evidence,
with their operation ID, rather than claiming fresh current Service absence.

A separately packaged command-based Service adapter can reuse the built-in
contract:

```python
from fangorn.resource_adapters import AdapterDescriptor, CommandAdapter


class AppAdapter(CommandAdapter):
    descriptor = AdapterDescriptor("acme.app", 1, frozenset({"service"}))
```

Its package declares:

```toml
[project.entry-points."fangorn.resource_adapters"]
app = "acme_app:AppAdapter"
```

`Workspaces` remains the public application facade. In addition to Workspace
lifecycle methods it exposes `validate_configuration`, `list_adapters`,
`list_consents`, `grant_consent`, and `revoke_consent`. The matching CLI commands
support global or local `--json` and emit schema 2. Existing schema-1 compatibility
commands retain their original meanings.
