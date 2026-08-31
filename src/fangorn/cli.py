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
@click.option("--json", "as_json", is_flag=True, help="Emit versioned JSON.")
@click.option(
    "--ndjson",
    "as_ndjson",
    is_flag=True,
    help="Emit one versioned JSON object per Workspace.",
)
def list_workspaces(as_json: bool, as_ndjson: bool) -> None:
    """List registered Workspaces."""
    if as_json and as_ndjson:
        raise click.UsageError("Choose only one of --json or --ndjson")
    try:
        workspaces = Registry.from_environment().list_workspaces()
    except RegistryError as error:
        raise click.ClickException(str(error)) from error

    if as_json:
        _echo_json(
            {
                "schema_version": 1,
                "workspaces": [workspace.as_dict() for workspace in workspaces],
            }
        )
        return
    if as_ndjson:
        for workspace in workspaces:
            _echo_json({"schema_version": 1, "workspace": workspace.as_dict()})
        return
    if not workspaces:
        click.echo("No Workspaces adopted.")
        return
    click.echo("Workspace ID\tBranch\tPath")
    for workspace in workspaces:
        branch = workspace.branch if workspace.branch is not None else "(detached)"
        click.echo(f"{workspace.id}\t{_human(branch)}\t{_human(workspace.path)}")


def _echo_json(payload: dict[str, object]) -> None:
    click.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _echo_workspace(workspace: WorkspaceRecord) -> None:
    branch = workspace.branch if workspace.branch is not None else "(detached)"
    click.echo(f"Path: {_human(workspace.path)}")
    click.echo(f"Branch: {_human(branch)}")
    click.echo(f"HEAD: {workspace.head}")


def _human(value: str) -> str:
    return "".join(
        f"\\x{code_point:02x}"
        if code_point < 0x20 or 0x7F <= code_point <= 0x9F
        else character
        for character in value
        for code_point in (ord(character),)
    )
