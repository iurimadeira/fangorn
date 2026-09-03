from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from fangorn._lifecycle import Observation, finish_create, plan_create
from fangorn._lifecycle import Resource as LifecycleResource
from fangorn.git import GitError, observe_worktree
from fangorn.git_worktree import (
    RepositorySource,
    create_worktree,
    inspect_owned_worktree,
    materialize_cache,
    normalize_repository_source,
    read_configuration,
    resolve_commit,
)
from fangorn.registry import (
    CreateIntentRecord,
    ProcessIdentity,
    Registry,
    RegistryError,
)
from fangorn.registry import WorkspaceRecord as _WorkspaceRecord

ADOPTION_ATTEMPTS = 3


class WorkspaceError(RuntimeError):
    """Workspace lifecycle operation failed."""


@dataclass(frozen=True)
class Binding:
    id: str
    repository_id: str
    repository_common_dir: str
    git_common_dir_generation: str
    git_dir: str
    git_dir_generation: str
    adopted_head: str | None
    created_at: str


@dataclass(frozen=True)
class CurrentGitFacts:
    path: str
    branch: str | None
    head: str | None
    observed_at: str


@dataclass(frozen=True)
class Workspace:
    binding: Binding
    current_git_facts: CurrentGitFacts


@dataclass(frozen=True)
class AdoptionResult:
    workspace: Workspace
    created: bool


@dataclass(frozen=True)
class CreateWorkspace:
    repository: str
    branch: str
    path: Path | None = None
    base: str | None = None
    config: Path | None = None
    request_id: str | None = None
    headless: bool = True
    start: bool = True


@dataclass(frozen=True)
class ResourceDefinition:
    name: str
    kind: Literal["worktree", "service", "terminal"]
    adapter_id: str
    adapter_api_major: int
    configuration: dict[str, object]
    external_reference: str | None
    locator: str
    ownership_token: str
    provisioning_status: Literal["uncreated", "created"]


@dataclass(frozen=True)
class WorkspaceDefinition:
    id: str
    parent_id: str | None
    repository_id: str
    created_from_sha: str
    configuration: bytes
    configuration_value: dict[str, object]
    configuration_digest: str
    resources: tuple[ResourceDefinition, ...]


@dataclass(frozen=True)
class WorkspaceAggregate:
    definition: WorkspaceDefinition
    state: str
    version: int
    path: str
    branch: str


@dataclass(frozen=True)
class Operation:
    id: str
    kind: str
    status: str


@dataclass(frozen=True)
class CreateWorkspaceResult:
    workspace: WorkspaceAggregate
    operation: Operation
    created: bool


class Workspaces:
    """Public application facade for Workspace lifecycle operations."""

    def __init__(
        self,
        registry: Registry,
        *,
        data_home: Path | None = None,
        cache_home: Path | None = None,
        process_identity: ProcessIdentity | None = None,
    ) -> None:
        self._registry = registry
        self._data_home = data_home or _xdg_home("XDG_DATA_HOME", ".local/share")
        self._cache_home = cache_home or _xdg_home("XDG_CACHE_HOME", ".cache")
        self._process_identity = process_identity or _current_process_identity()

    @classmethod
    def from_environment(cls) -> Workspaces:
        try:
            return cls(Registry.from_environment())
        except RegistryError as error:
            raise WorkspaceError(str(error)) from error

    def adopt(self, path: Path) -> AdoptionResult:
        try:
            markerless_reobserved = False
            for _ in range(ADOPTION_ATTEMPTS):
                observation = observe_worktree(
                    path,
                    reserve_observation=self._registry.reserve_observation,
                )
                requirements = self._registry.marker_creation_requirements(
                    observation,
                    markerless_reobserved=markerless_reobserved,
                )
                if requirements is None:
                    markerless_reobserved = True
                    continue
                create_repository_generation, create_worktree_generation = requirements
                if create_repository_generation or create_worktree_generation:
                    observation = observe_worktree(
                        path,
                        create_repository_generation=create_repository_generation,
                        create_worktree_generation=create_worktree_generation,
                        reserve_observation=self._registry.reserve_observation,
                    )
                record, created = self._registry.adopt(observation)
                return AdoptionResult(workspace=_workspace(record), created=created)
            raise RegistryError(
                "Concurrent equivalent adoption did not settle; retry the command"
            )
        except (GitError, RegistryError) as error:
            raise WorkspaceError(str(error)) from error

    def create(self, request: CreateWorkspace) -> CreateWorkspaceResult:
        try:
            if not request.headless:
                raise WorkspaceError(
                    "Only headless Workspace creation is available in this release"
                )
            if not request.repository:
                raise WorkspaceError("A root Workspace requires a repository source")
            _validate_branch(request.branch)
            source = normalize_repository_source(request.repository)
            target = _target_path(request, source, self._data_home)
            request_value = {
                "base": request.base,
                "branch": request.branch,
                "config": str(request.config.resolve()) if request.config else None,
                "headless": request.headless,
                "no_start": not request.start,
                "parent_id": None,
                "request_id": request.request_id,
                "source": source.normalized,
                "target_path": str(target),
            }
            request_json = json.dumps(
                request_value, sort_keys=True, separators=(",", ":")
            )
            request_key = hashlib.sha256(request_json.encode()).hexdigest()
            intent, created = self._registry.begin_create_intent(
                request_key=request_key,
                request_id=request.request_id,
                request_json=request_json,
                target_path=str(target),
                workspace_id=str(uuid4()),
                operation_id=str(uuid4()),
                prepare_cache=source.clone_url is not None,
            )
            if intent.status == "completed":
                aggregate, operation = self._load_completed(intent.workspace_id)
                resource = aggregate.definition.resources[0]
                inspect_owned_worktree(
                    Path(aggregate.path),
                    expected_commit=aggregate.definition.created_from_sha,
                    expected_branch=aggregate.branch,
                    ownership_token=resource.ownership_token,
                )
                return CreateWorkspaceResult(aggregate, operation, created=False)

            repository = self._prepare_repository(source, intent)
            if intent.resolved_json is None:
                commit = resolve_commit(repository, request.base)
                configuration = read_configuration(repository, commit, request.config)
                configuration_value = _configuration_value(configuration)
                resource_token = secrets.token_hex(32)
                lifecycle = plan_create(
                    (LifecycleResource("worktree", "worktree"),),
                    start=request.start,
                )
                resolved: dict[str, object] = {
                    "configuration": configuration.hex(),
                    "configuration_digest": hashlib.sha256(configuration).hexdigest(),
                    "configuration_value": configuration_value,
                    "created_from_sha": commit,
                    "ownership_token": resource_token,
                }
                resolved = self._registry.enrich_create_intent(
                    intent.operation_id,
                    resolved=resolved,
                    steps=tuple(
                        (step.action, step.resource_name) for step in lifecycle.steps
                    ),
                )
            else:
                loaded = json.loads(intent.resolved_json)
                if not isinstance(loaded, dict):
                    raise WorkspaceError("Stored Workspace create intent is malformed")
                resolved = cast(dict[str, object], loaded)

            lease_epoch = self._registry.acquire_lease(
                scope_kind="workspace",
                scope_key=intent.workspace_id,
                operation_id=intent.operation_id,
                owner=self._process_identity,
                owner_status=_owner_status,
            )
            lifecycle = plan_create(
                (LifecycleResource("worktree", "worktree"),), start=request.start
            )
            position = 1 if source.clone_url is not None else 0
            ownership_token = str(resolved["ownership_token"])
            commit = str(resolved["created_from_sha"])
            observation = None
            for step in lifecycle.steps:
                previous = self._registry.start_operation_step(
                    intent.operation_id,
                    position=position,
                    scope_kind="workspace",
                    scope_key=intent.workspace_id,
                    lease_epoch=lease_epoch,
                )
                if previous != "completed":
                    if step.action == "create":
                        observation = create_worktree(
                            repository,
                            target=target,
                            branch=request.branch,
                            commit=commit,
                            ownership_token=ownership_token,
                            reconcile=previous in {"running", "unknown"},
                        )
                    else:
                        observation = inspect_owned_worktree(
                            target,
                            expected_commit=commit,
                            expected_branch=request.branch,
                            ownership_token=ownership_token,
                        )
                    self._registry.finish_operation_step(
                        intent.operation_id,
                        position=position,
                        scope_kind="workspace",
                        scope_key=intent.workspace_id,
                        lease_epoch=lease_epoch,
                        result={"observation": "ready"},
                    )
                position += 1
            observation = inspect_owned_worktree(
                target,
                expected_commit=commit,
                expected_branch=request.branch,
                ownership_token=ownership_token,
            )
            state = finish_create(
                (LifecycleResource("worktree", "worktree"),),
                {"worktree": Observation("ready")},
                start=request.start,
            )
            configuration = bytes.fromhex(str(resolved["configuration"]))
            stored_configuration_value = resolved["configuration_value"]
            if not isinstance(stored_configuration_value, dict):
                raise WorkspaceError("Stored Workspace configuration is malformed")
            self._registry.complete_workspace_create(
                intent=intent,
                observation=observation,
                created_from_sha=commit,
                configuration=configuration,
                configuration_json=json.dumps(
                    stored_configuration_value,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                configuration_digest=str(resolved["configuration_digest"]),
                state=state,
                lease_epoch=lease_epoch,
            )
            aggregate, operation = self._load_completed(intent.workspace_id)
            return CreateWorkspaceResult(aggregate, operation, created=created)
        except (
            GitError,
            RegistryError,
            OSError,
            ValueError,
            tomllib.TOMLDecodeError,
        ) as error:
            raise WorkspaceError(str(error)) from error

    def list(self) -> list[Workspace]:
        try:
            return [_workspace(record) for record in self._registry.list_workspaces()]
        except RegistryError as error:
            raise WorkspaceError(str(error)) from error

    def inspect(self, path: Path) -> Workspace:
        try:
            observation = observe_worktree(path)
            return _workspace(self._registry.inspect_worktree(observation))
        except (GitError, RegistryError) as error:
            raise WorkspaceError(str(error)) from error

    def _prepare_repository(
        self, source: RepositorySource, intent: CreateIntentRecord
    ) -> Path:
        if source.clone_url is None:
            if source.path is None:
                raise WorkspaceError("Local repository path is unavailable")
            return source.path
        operation_id = intent.operation_id
        digest = hashlib.sha256(source.normalized.encode()).hexdigest()
        cache_path = self._cache_home / "fangorn" / "repositories" / f"{digest}.git"
        epoch = self._registry.acquire_lease(
            scope_kind="repository",
            scope_key=source.normalized,
            operation_id=operation_id,
            owner=self._process_identity,
            owner_status=_owner_status,
        )
        try:
            previous = self._registry.start_operation_step(
                operation_id,
                position=0,
                scope_kind="repository",
                scope_key=source.normalized,
                lease_epoch=epoch,
            )
            entry = self._registry.cache_entry(source.normalized)
            selected = Path(entry[0]) if entry is not None else cache_path
            repository = materialize_cache(source, selected)
            if previous != "completed" or entry is None:
                self._registry.save_cache_entry(
                    source.normalized,
                    path=str(repository),
                    repository_generation=entry[1] if entry is not None else None,
                )
                self._registry.finish_operation_step(
                    operation_id,
                    position=0,
                    scope_kind="repository",
                    scope_key=source.normalized,
                    lease_epoch=epoch,
                    result={"path": str(repository)},
                )
            return repository
        finally:
            self._registry.release_lease(
                scope_kind="repository",
                scope_key=source.normalized,
                operation_id=operation_id,
                lease_epoch=epoch,
            )

    def _load_completed(
        self, workspace_id: str
    ) -> tuple[WorkspaceAggregate, Operation]:
        row, resource_rows, operation_row = self._registry.load_created_workspace(
            workspace_id
        )
        resources = tuple(
            ResourceDefinition(
                name=str(resource["name"]),
                kind=str(resource["kind"]),  # type: ignore[arg-type]
                adapter_id=str(resource["adapter_id"]),
                adapter_api_major=int(resource["adapter_api_major"]),
                configuration=json.loads(str(resource["configuration_json"])),
                external_reference=(
                    str(resource["external_reference"])
                    if resource["external_reference"] is not None
                    else None
                ),
                locator=str(resource["locator"]),
                ownership_token=str(resource["ownership_token"]),
                provisioning_status=str(resource["provisioning_status"]),  # type: ignore[arg-type]
            )
            for resource in resource_rows
        )
        aggregate = WorkspaceAggregate(
            definition=WorkspaceDefinition(
                id=str(row["id"]),
                parent_id=str(row["parent_id"]) if row["parent_id"] else None,
                repository_id=str(row["repository_id"]),
                created_from_sha=str(row["created_from_sha"]),
                configuration=bytes(row["configuration"]),
                configuration_value=json.loads(str(row["configuration_json"])),
                configuration_digest=str(row["configuration_digest"]),
                resources=resources,
            ),
            state=str(row["lifecycle_state"]),
            version=int(row["aggregate_version"]),
            path=str(row["path"]),
            branch=str(row["branch"]),
        )
        return aggregate, Operation(
            id=str(operation_row["id"]),
            kind=str(operation_row["kind"]),
            status=str(operation_row["status"]),
        )


def _workspace(record: _WorkspaceRecord) -> Workspace:
    return Workspace(
        binding=Binding(
            id=record.id,
            repository_id=record.repository_id,
            repository_common_dir=record.repository_common_dir,
            git_common_dir_generation=record.git_common_dir_generation,
            git_dir=record.git_dir,
            git_dir_generation=record.git_dir_generation,
            adopted_head=record.adopted_head,
            created_at=record.created_at,
        ),
        current_git_facts=CurrentGitFacts(
            path=record.path,
            branch=record.branch,
            head=record.head,
            observed_at=record.last_observed_at,
        ),
    )


def _xdg_home(variable: str, fallback: str) -> Path:
    value = os.environ.get(variable)
    if value and Path(value).is_absolute():
        return Path(value)
    home = os.environ.get("HOME")
    if not home or not Path(home).is_absolute():
        raise WorkspaceError(f"{variable} is unset and HOME is unavailable")
    return Path(home) / fallback


def _target_path(
    request: CreateWorkspace, source: RepositorySource, data_home: Path
) -> Path:
    if request.path is not None:
        return request.path.expanduser().absolute()
    material = json.dumps(
        {
            "base": request.base,
            "branch": request.branch,
            "request_id": request.request_id,
            "source": source.normalized,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    suffix = hashlib.sha256(material.encode()).hexdigest()[:12]
    safe_branch = re_sub_path(request.branch)
    return (
        data_home / "fangorn" / "worktrees" / source.name / f"{safe_branch}-{suffix}"
    ).absolute()


def re_sub_path(value: str) -> str:
    rendered = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in value
    )
    return rendered.strip(".-") or "workspace"


def _validate_branch(branch: str) -> None:
    if (
        not branch
        or branch.startswith("-")
        or any(character in branch for character in ("\x00", "\n", "\r"))
    ):
        raise WorkspaceError("Workspace branch is invalid")


def _configuration_value(content: bytes) -> dict[str, object]:
    if not content:
        return {"schema_version": 1}
    value = tomllib.loads(content.decode("utf-8"))
    if value.get("schema_version") != 1:
        raise WorkspaceError("fangorn.toml requires schema_version = 1")
    if value.get("services"):
        raise WorkspaceError("Service Resources are not available in this release")
    return value


def _current_process_identity() -> ProcessIdentity:
    return ProcessIdentity(
        process_instance_id=str(uuid4()),
        boot_identity=_boot_identity(),
        pid=os.getpid(),
        process_start_identity=_process_start_identity(os.getpid()),
    )


def _boot_identity() -> str:
    boot_id = Path("/proc/sys/kernel/random/boot_id")
    try:
        return boot_id.read_text(encoding="ascii").strip()
    except OSError:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.boottime"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return f"{platform.node()}:unknown-boot"


def _process_start_identity(pid: int) -> str:
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        fields = stat_path.read_text(encoding="ascii").rsplit(")", 1)[1].split()
        return fields[19]
    except (OSError, IndexError):
        result = subprocess.run(  # noqa: S603
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise WorkspaceError("Cannot establish process start identity") from None
        return result.stdout.strip()


def _owner_status(owner: ProcessIdentity) -> str:
    if owner.boot_identity != _boot_identity():
        return "dead"
    try:
        os.kill(owner.pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "inconclusive"
    try:
        current_start = _process_start_identity(owner.pid)
    except WorkspaceError:
        return "inconclusive"
    return "live" if current_start == owner.process_start_identity else "dead"
