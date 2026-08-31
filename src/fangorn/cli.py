from __future__ import annotations

import json
from pathlib import Path

import click

from fangorn import __version__
from fangorn.git import GitError, observe_worktree
from fangorn.registry import Registry, RegistryError, WorkspaceRecord


@click.group()
@click.version_option(__version__)
def main() -> None:
    """Worktree-native workspace families for humans and agents."""


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit versioned JSON.")
@click.argument(
    "path",
    default=".",
    type=click.Path(path_type=Path, file_okay=False, resolve_path=True),
)
def adopt(path: Path, as_json: bool) -> None:
    """Adopt an existing Git worktree without changing it."""
    try:
        observation = observe_worktree(path)
        workspace, created = Registry.from_environment().adopt(observation)
    except (GitError, RegistryError) as error:
        raise click.ClickException(str(error)) from error

    if as_json:
        _echo_json(
            {
                "schema_version": 1,
                "created": created,
                "workspace": workspace.as_dict(),
            }
        )
        return
    action = "Adopted" if created else "Already adopted"
    click.echo(f"{action} Workspace {workspace.id}")
    _echo_workspace(workspace)


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit versioned JSON.")
@click.argument(
    "path",
    default=".",
    type=click.Path(path_type=Path, file_okay=False, resolve_path=True),
)
def info(path: Path, as_json: bool) -> None:
    """Inspect the Workspace bound to a Git worktree."""
    try:
        observation = observe_worktree(path)
        workspace = Registry.from_environment().get_by_worktree(observation)
    except (GitError, RegistryError) as error:
        raise click.ClickException(str(error)) from error

    if as_json:
        _echo_json({"schema_version": 1, "workspace": workspace.as_dict()})
        return
    click.echo(f"Workspace {workspace.id}")
    _echo_workspace(workspace)


@main.command(name="list")
def list_workspaces() -> None:
    """List registered Workspaces."""
    raise click.ClickException("list is not implemented")


def _echo_json(payload: dict[str, object]) -> None:
    click.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _echo_workspace(workspace: WorkspaceRecord) -> None:
    branch = workspace.branch if workspace.branch is not None else "(detached)"
    click.echo(f"Path: {workspace.path}")
    click.echo(f"Branch: {branch}")
    click.echo(f"HEAD: {workspace.head}")
