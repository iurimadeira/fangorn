from __future__ import annotations

from pathlib import Path

import click

from fangorn import __version__


@click.group()
@click.version_option(__version__)
def main() -> None:
    """Worktree-native workspace families for humans and agents."""


@main.command()
@click.argument(
    "path",
    default=".",
    type=click.Path(path_type=Path, file_okay=False, resolve_path=True),
)
def adopt(path: Path) -> None:
    """Adopt an existing Git worktree without changing it."""
    raise click.ClickException(f"adopt is not implemented for {path}")


@main.command()
@click.argument(
    "path",
    default=".",
    type=click.Path(path_type=Path, file_okay=False, resolve_path=True),
)
def info(path: Path) -> None:
    """Inspect the Workspace bound to a Git worktree."""
    raise click.ClickException(f"info is not implemented for {path}")


@main.command(name="list")
def list_workspaces() -> None:
    """List registered Workspaces."""
    raise click.ClickException("list is not implemented")
