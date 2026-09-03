from __future__ import annotations

import base64
import json
from pathlib import Path

import click

from fangorn import __version__
from fangorn.workspaces import (
    CreateWorkspace,
    Operation,
    Workspace,
    WorkspaceAggregate,
    WorkspaceError,
    Workspaces,
)

COMMAND_PATH = click.Path(
    path_type=Path,
    exists=False,
    file_okay=True,
    dir_okay=True,
    readable=False,
    resolve_path=False,
)


@click.group()
@click.option("--json", "root_json", is_flag=True, help="Emit versioned JSON.")
@click.version_option(__version__)
@click.pass_context
def main(context: click.Context, root_json: bool) -> None:
    """Worktree-native workspace families for humans and agents."""
    context.ensure_object(dict)
    context.obj["json"] = root_json


@main.group()
def workspace() -> None:
    """Create and manage Workspace aggregates."""


@workspace.command(name="create")
@click.option("--repo", "repository", required=True, help="Local path or clone URL.")
@click.option("--branch", required=True, help="Branch for the Worktree Resource.")
@click.option("--path", type=COMMAND_PATH, help="Target Worktree path.")
@click.option("--base", help="Git ref resolved once as creation provenance.")
@click.option("--config", type=COMMAND_PATH, help="Explicit fangorn.toml snapshot.")
@click.option("--request-id", help="Caller idempotency key.")
@click.option("--headless", is_flag=True, help="Omit a Terminal Resource.")
@click.option("--no-start", is_flag=True, help="Provision without starting.")
@click.option("--json", "as_json", is_flag=True, help="Emit versioned JSON.")
@click.pass_context
def create_workspace(
    context: click.Context,
    repository: str,
    branch: str,
    path: Path | None,
    base: str | None,
    config: Path | None,
    request_id: str | None,
    headless: bool,
    no_start: bool,
    as_json: bool,
) -> None:
    """Create a complete root Workspace."""
    try:
        result = Workspaces.from_environment().create(
            CreateWorkspace(
                repository=repository,
                branch=branch,
                path=path,
                base=base,
                config=config,
                request_id=request_id,
                headless=headless,
                start=not no_start,
            )
        )
    except WorkspaceError as error:
        raise click.ClickException(_human(str(error))) from error

    root_json = bool(context.find_root().obj.get("json"))
    if as_json or root_json:
        _echo_json(
            {
                "schema_version": 2,
                "created": result.created,
                "workspace": _aggregate_schema(result.workspace),
                "operation": _operation_schema(result.operation),
            }
        )
        return
    action = "Created" if result.created else "Already created"
    click.echo(f"{action} Workspace {result.workspace.definition.id}")
    click.echo(f"State: {result.workspace.state}")
    click.echo(f"Path: {_human(result.workspace.path)}")


@main.command(hidden=True)
@click.option("--json", "as_json", is_flag=True, help="Emit versioned JSON.")
@click.argument(
    "path",
    default=".",
    type=COMMAND_PATH,
)
@click.pass_context
def adopt(context: click.Context, path: Path, as_json: bool) -> None:
    """Adopt an existing Git worktree without changing it."""
    try:
        result = Workspaces.from_environment().adopt(path)
    except WorkspaceError as error:
        raise click.ClickException(_human(str(error))) from error

    workspace = result.workspace
    if as_json or bool(context.find_root().obj.get("json")):
        _echo_json(
            {
                "schema_version": 1,
                "created": result.created,
                "workspace": _workspace_schema(workspace),
            }
        )
        return
    action = "Adopted" if result.created else "Already adopted"
    click.echo(f"{action} Workspace {workspace.binding.id}")
    _echo_workspace(workspace)


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit versioned JSON.")
@click.argument(
    "path",
    default=".",
    type=COMMAND_PATH,
)
@click.pass_context
def info(context: click.Context, path: Path, as_json: bool) -> None:
    """Inspect the Workspace bound to a Git worktree."""
    try:
        workspace = Workspaces.from_environment().inspect(path)
    except WorkspaceError as error:
        raise click.ClickException(_human(str(error))) from error

    if as_json or bool(context.find_root().obj.get("json")):
        _echo_json({"schema_version": 1, "workspace": _workspace_schema(workspace)})
        return
    click.echo(f"Workspace {workspace.binding.id}")
    _echo_workspace(workspace)


@main.command(name="list")
@click.option("--json", "as_json", is_flag=True, help="Emit versioned JSON.")
@click.option(
    "--ndjson",
    "as_ndjson",
    is_flag=True,
    help="Emit one versioned JSON object per Workspace.",
)
@click.pass_context
def list_workspaces(context: click.Context, as_json: bool, as_ndjson: bool) -> None:
    """List registered Workspaces."""
    root_json = bool(context.find_root().obj.get("json"))
    if (as_json or root_json) and as_ndjson:
        raise click.UsageError("Choose only one of --json or --ndjson")
    try:
        workspaces = Workspaces.from_environment().list()
    except WorkspaceError as error:
        raise click.ClickException(_human(str(error))) from error

    if as_json or root_json:
        _echo_json(
            {
                "schema_version": 1,
                "workspaces": [
                    _workspace_schema(workspace) for workspace in workspaces
                ],
            }
        )
        return
    if as_ndjson:
        for workspace in workspaces:
            _echo_json({"schema_version": 1, "workspace": _workspace_schema(workspace)})
        return
    if not workspaces:
        click.echo("No Workspaces adopted.")
        return
    click.echo("Workspace ID\tBranch\tPath")
    for workspace in workspaces:
        binding = workspace.binding
        facts = workspace.current_git_facts
        branch = facts.branch if facts.branch is not None else "(detached)"
        click.echo(f"{binding.id}\t{_human(branch)}\t{_human(facts.path)}")


def _echo_json(payload: dict[str, object]) -> None:
    click.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _echo_workspace(workspace: Workspace) -> None:
    facts = workspace.current_git_facts
    branch = facts.branch if facts.branch is not None else "(detached)"
    click.echo(f"Path: {_human(facts.path)}")
    click.echo(f"Branch: {_human(branch)}")
    head = facts.head if facts.head is not None else "(unborn)"
    click.echo(f"HEAD: {head}")


def _workspace_schema(workspace: Workspace) -> dict[str, object]:
    binding = workspace.binding
    facts = workspace.current_git_facts
    return {
        "id": binding.id,
        "repository_id": binding.repository_id,
        "repository_common_dir": binding.repository_common_dir,
        "git_common_dir_generation": binding.git_common_dir_generation,
        "git_dir": binding.git_dir,
        "git_dir_generation": binding.git_dir_generation,
        "path": facts.path,
        "branch": facts.branch,
        "head": facts.head,
        "adopted_head": binding.adopted_head,
        "created_at": binding.created_at,
        "last_observed_at": facts.observed_at,
    }


def _aggregate_schema(workspace: WorkspaceAggregate) -> dict[str, object]:
    definition = workspace.definition
    return {
        "definition": {
            "id": definition.id,
            "parent_id": definition.parent_id,
            "repository_id": definition.repository_id,
            "created_from_sha": definition.created_from_sha,
            "configuration": {
                "bytes_base64": base64.b64encode(definition.configuration).decode(
                    "ascii"
                ),
                "value": definition.configuration_value,
                "digest": definition.configuration_digest,
            },
            "resources": [
                {
                    "name": resource.name,
                    "kind": resource.kind,
                    "adapter_id": resource.adapter_id,
                    "adapter_api_major": resource.adapter_api_major,
                    "configuration": resource.configuration,
                    "external_reference": resource.external_reference,
                    "locator": resource.locator,
                    "ownership_token": resource.ownership_token,
                    "provisioning_status": resource.provisioning_status,
                }
                for resource in definition.resources
            ],
        },
        "state": workspace.state,
        "version": workspace.version,
        "path": workspace.path,
        "branch": workspace.branch,
    }


def _operation_schema(operation: Operation) -> dict[str, object]:
    return {
        "id": operation.id,
        "kind": operation.kind,
        "status": operation.status,
    }


def _human(value: str) -> str:
    rendered: list[str] = []
    for character in value:
        code_point = ord(character)
        if code_point < 0x20 or 0x7F <= code_point <= 0x9F:
            rendered.append(f"\\x{code_point:02x}")
        elif not character.isprintable():
            if code_point <= 0xFFFF:
                rendered.append(f"\\u{code_point:04x}")
            else:
                rendered.append(f"\\U{code_point:08x}")
        else:
            rendered.append(character)
    return "".join(rendered)
