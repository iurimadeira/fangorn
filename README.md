# Fangorn

Worktree-native workspace families for humans and agents.

Fangorn records each Workspace as one immutable binding to one real Git
worktree. This first release can adopt existing worktrees, inspect their
current Git facts, and enumerate registered Workspaces without moving or
changing any checkout.

## Requirements

- Python 3.12 or newer
- Git
- Linux or macOS

Click is Fangorn's only third-party runtime dependency.

## Install

With uv:

```sh
uv tool install fangorn-cli
```

With pipx:

```sh
pipx install fangorn-cli
```

## Use

Adopt the worktree containing the current directory:

```sh
fangorn adopt
```

Inspect it later or list all adopted Workspaces:

```sh
fangorn info
fangorn list
```

Pass another worktree path to `adopt` or `info`. Adoption is idempotent: an
equivalent retry returns the existing Workspace instead of creating another
identity.

Fangorn records the Git administrative directory's filesystem device and inode
as a non-mutating generation witness. It refuses a later directory instance at
the same canonical path. A filesystem that reuses both values after deletion
can defeat that witness; Fangorn does not write marker files into Git metadata
to manufacture a stronger identity.

Human-readable output is the default. `adopt` and `info` accept `--json`;
`list` accepts `--json` or `--ndjson`. Every machine record includes
`schema_version: 1`, and errors go to stderr with a nonzero exit status.

State is stored in `$XDG_STATE_HOME/fangorn/registry.sqlite3`, or in
`$HOME/.local/state/fangorn/registry.sqlite3` when `XDG_STATE_HOME` is unset.

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
