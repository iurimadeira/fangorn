# Fangorn

Worktree-native workspace families for humans and agents.

Fangorn records each Workspace as one immutable binding to one real Git
worktree. This compatibility release can inspect current Git facts and
enumerate registered Workspaces without moving or changing any checkout.

## Requirements

- Python 3.12 or newer
- Git 2.31 or newer
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
parses arguments and renders results.

Human-readable output is the default. `info` accepts `--json`; `list` accepts
`--json` or `--ndjson`. Every machine record includes
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
