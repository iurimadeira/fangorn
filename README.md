# Fangorn

Recoverable disposable workspaces for humans and agents.

Fangorn is named for a forest of related worktrees. A Workspace is a durable,
structurally immutable aggregate that owns its Resources. A Repository is the
technical Git identity shared by Worktree Resources; it is not a second
user-managed project object.

This release creates root headless Workspaces containing one built-in Git
Worktree Resource. Terminal, Service, parent-lineage, and later lifecycle
commands are not shipped yet. Existing `info`, `list`, and hidden migration
`adopt` compatibility commands retain their schema-1 meanings.

## Requirements

- Python 3.12 or newer
- Git 2.31 or newer
- Linux or macOS

Git is required for every Workspace. Headless Workspaces need no terminal
multiplexer. tmux will be required only when the built-in Terminal Resource is
selected in a later release. Configured services and third-party adapters will
require their own tools. Repository-configured executable checkout filters and
filesystem monitors and local configuration includes are rejected; Git hooks
are disabled during creation. Local Git administrative directories and config
files must be owned by the current account or root and not writable by another
account. When group-write is present, any Linux extended access ACL is
conservatively rejected even if its named entries are read-only; macOS extended
ACLs that allow writes are rejected. Click is Fangorn's only third-party runtime
dependency.

## Install

With uv:

```sh
uv tool install fangorn-cli
```

With pipx:

```sh
pipx install fangorn-cli
```

## Create

Create from a local checkout or a credential-free clone URL:

```sh
fangorn workspace create --repo ./my-repository --branch feature --headless
fangorn workspace create --repo https://example.com/acme/repository.git \
  --branch feature --path /work/feature --headless
```

Create starts by default and succeeds as `ready` only after the Worktree
Resource is observed at its resolved `created_from_sha`, requested branch, and
immutable ownership token. Branch and HEAD can change after creation as normal
operational Git facts. `--no-start` provisions the Worktree and returns
`stopped`. Use `--base REF` to resolve another commit once, `--request-id KEY`
for caller idempotency, and `--config PATH` to snapshot an explicit
`fangorn.toml`.

Without `--path`, Fangorn chooses a deterministic target below
`$XDG_DATA_HOME/fangorn/worktrees/<repository>/` (falling back to
`$HOME/.local/share`) from the source, branch, base, and request ID. Target
paths are canonicalized before state is written. Existing parent components
must be real directories owned by root or the current user; non-sticky group-
or world-writable parents, dangling symlinks, and symlink loops are rejected.
An existing target is accepted only when retry evidence proves it is the
Worktree Resource owned by the same create operation.

F2 configuration accepts only `schema_version = 1` and an optional empty
`[services]` table because Service Resources are not available yet. Without
`--config`, Fangorn reads `fangorn.toml` from the resolved commit; when no file
exists, the value defaults to `schema_version = 1`. Configuration is capped at
1 MiB, and its exact bytes, parsed value, and digest are snapshotted for retries.
Every component of an explicit configuration path must be a real, non-symlink
filesystem entry.

Equivalent retries return the same Workspace ID, resolved `created_from_sha`,
target path, and completed operation. Reusing a request ID or target path with
different immutable fields fails before Git effects. Clone URLs use a shared
cache under `$XDG_CACHE_HOME/fangorn/repositories/<registry-namespace>/`
(falling back to `$HOME/.cache`). The namespace is the SHA-256 identity of the
resolved Registry path, and each normalized source has its own SHA-256-named
bare repository. Cache namespace directories are private to the current user.
Cache acquisition and every Workspace mutation use recoverable fenced leases.

Supervised Git effects have a one-hour deadline and retain at most 8 MiB per
output stream. Hitting either limit fails creation with retryable journal
evidence. Fangorn releases the lease after proving the Git process group stopped;
if proof remains inconclusive, a detached guardian retains the lease until no
live process remains. Repeated process-probe failures persist a non-empty
`quiescence-unknown` marker and let detached helpers exit without weakening the
fence. After independently confirming that no Git process from that invocation
remains, an operator may remove its marker from
`$XDG_STATE_HOME/fangorn/invocations/` (default
`~/.local/state/fangorn/invocations/`) to permit retry.

Machine create output uses schema 2:

```sh
fangorn --json workspace create --repo ./my-repository \
  --branch feature --headless
```

The top-level shape is `schema_version`, `created`, `workspace`, and
`operation`. `workspace` separates immutable `definition` (including the
configuration snapshot and Resource definitions) from `resource_states`, then
reports aggregate `state`, `version`, `path`, and `branch`. `operation` reports
its `id`, `kind`, and `status`.

## Inspect compatibility records

Inspect the Workspace containing the current directory or list registered
Workspaces:

```sh
fangorn info
fangorn list
```

Pass another worktree path to `info`. Inspection and listing are strictly
read-only: they do not initialize or migrate state, update timestamps, establish
identity markers, or modify Git metadata.

Python callers use the same application facade as the CLI:

```python
from pathlib import Path

from fangorn.workspaces import Workspaces

workspaces = Workspaces.from_environment()
workspace = workspaces.inspect(Path("."))
registered = workspaces.list()
```

`fangorn.workspaces.Workspaces` owns Workspace lifecycle behavior; Click only
parses arguments and renders results. Python callers create with
`CreateWorkspace` and receive `CreateWorkspaceResult` domain values.

Human-readable output is the default. New create output uses schema 2. `info`
accepts `--json`; `list` accepts `--json` or `--ndjson`; those compatibility
records remain schema 1. Errors go to stderr with a nonzero exit status.

State is stored in `$XDG_STATE_HOME/fangorn/registry.sqlite3`, or in
`$HOME/.local/state/fangorn/registry.sqlite3` when `XDG_STATE_HOME` is unset.
Every path component must be a real directory owned by root or the current
user; writable ancestors are accepted only when sticky. The final `fangorn`
directory and `registry.sqlite3` must be owned by the current user, private
(`0700` and `0600`), and free of macOS ACLs granting read, search, or write
access. Read commands refuse unsafe existing state without changing its
permissions. Repair a custom state location before retrying; Fangorn creates
missing components privately.

## Develop

```sh
uv sync --locked --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
uv run python scripts/check_publication.py --source . dist/*
```

## License

Fangorn is released under the MIT License. Runtime dependency notices are in
`THIRD_PARTY_NOTICES.md`.
