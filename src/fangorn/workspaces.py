from __future__ import annotations

import ctypes
import fcntl
import hashlib
import json
import os
import re
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock, Thread
from types import MappingProxyType
from typing import Literal, cast
from uuid import uuid4

from fangorn._lifecycle import Observation, finish_create, plan_create
from fangorn._lifecycle import Resource as LifecycleResource
from fangorn._permissions import (
    descriptor_has_writable_acl as _darwin_acl_allows_write,
)
from fangorn.git import (
    GitError,
    GitQuiescenceError,
    observe_worktree,
    repository_generation,
)
from fangorn.git_worktree import (
    RepositorySource,
    create_worktree,
    inspect_owned_worktree,
    materialize_cache,
    normalize_repository_source,
    read_configuration,
    resolve_commit,
    validate_branch_name,
    validate_repository_for_object_reads,
    validate_target_path,
)
from fangorn.registry import (
    CreateAlreadyCompleted,
    CreateIntentRecord,
    ProcessIdentity,
    Registry,
    RegistryError,
    _open_registry_state_directory,
)
from fangorn.registry import WorkspaceRecord as _WorkspaceRecord

ADOPTION_ATTEMPTS = 3
_ACTIVE_INVOCATIONS: dict[str, tuple[int, Path]] = {}
_ACTIVE_INVOCATIONS_LOCK = Lock()


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = (
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    )


def _defer_invocation_marker_cleanup(descriptor: int, marker: Path) -> None:
    ready_read, ready_write = os.pipe()
    blocked = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    try:
        cleaner = subprocess.Popen(  # noqa: S603 -- fixed cleaner argv
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("_invocation_cleaner.py")),
                str(descriptor),
                str(marker),
                str(ready_write),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={
                name: value
                for name, value in os.environ.items()
                if name not in {"COVERAGE_PROCESS_CONFIG", "COVERAGE_PROCESS_START"}
            },
            pass_fds=(descriptor, ready_write),
            process_group=0,
        )
    except BaseException:
        os.close(ready_read)
        raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        os.close(ready_write)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(ready_read, selectors.EVENT_READ)
            ready = os.read(ready_read, 3) if selector.select(5) else b""
    finally:
        os.close(ready_read)
    if ready != b"r\n":
        with suppress(ProcessLookupError):
            os.killpg(cleaner.pid, signal.SIGKILL)
        cleaner.wait(timeout=2)
        raise WorkspaceError("Cannot establish Workspace marker cleaner")
    with suppress(RuntimeError):
        Thread(target=cleaner.wait, daemon=True).start()


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
    configuration: Mapping[str, object]
    external_reference: str | None
    locator: str
    ownership_token: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "configuration", _freeze_mapping(self.configuration))


@dataclass(frozen=True)
class ResourceState:
    name: str
    provisioning_status: Literal["uncreated", "created"]


@dataclass(frozen=True)
class WorkspaceDefinition:
    id: str
    parent_id: str | None
    repository_id: str
    created_from_sha: str
    configuration: bytes
    configuration_value: Mapping[str, object]
    configuration_digest: str
    resources: tuple[ResourceDefinition, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "configuration_value", _freeze_mapping(self.configuration_value)
        )


@dataclass(frozen=True)
class WorkspaceAggregate:
    definition: WorkspaceDefinition
    resource_states: tuple[ResourceState, ...]
    state: str
    version: int
    path: str
    branch: str | None


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
        self._data_home = data_home
        self._cache_home = cache_home
        self._process_identity = process_identity
        self._invocation_root = registry.path.parent / "invocations"

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
        intent: CreateIntentRecord | None = None
        lease_epoch: int | None = None
        owner: ProcessIdentity | None = None
        try:
            if not request.headless:
                raise WorkspaceError(
                    "Only headless Workspace creation is available in this release"
                )
            if not request.repository:
                raise WorkspaceError("A root Workspace requires a repository source")
            validate_branch_name(request.branch)
            source = normalize_repository_source(request.repository)
            data_home = self._data_home
            if request.path is None and data_home is None:
                data_home = _xdg_home("XDG_DATA_HOME", ".local/share")
            target = _target_path(request, source, data_home)
            config_identity = (
                str(_configuration_identity(request.config))
                if request.config is not None
                else None
            )
            request_value = {
                "base": request.base,
                "branch": request.branch,
                "config": config_identity,
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
                return self._completed_create(intent.workspace_id)
            owner = self._invocation_process_identity()
            try:
                lease_epoch = self._registry.acquire_lease(
                    scope_kind="workspace",
                    scope_key=intent.workspace_id,
                    operation_id=intent.operation_id,
                    owner=owner,
                    owner_status=self._owner_status,
                )
            except CreateAlreadyCompleted:
                return self._completed_create(intent.workspace_id, created=created)

            config = (
                Path(config_identity)
                if intent.resolved_json is None and config_identity is not None
                else None
            )
            repository = self._prepare_repository(
                source,
                intent,
                owner,
                refresh_default_head=request.base
                in {None, "HEAD", "origin/HEAD", "refs/remotes/origin/HEAD"},
            )
            validate_repository_for_object_reads(
                repository,
                liveness_fd=self._invocation_descriptor(owner),
            )
            if intent.resolved_json is None:
                commit = intent.resolved_sha
                if commit is None:
                    commit = self._registry.persist_resolved_sha(
                        intent.operation_id,
                        resolve_commit(
                            repository,
                            request.base,
                            remote=source.clone_url is not None,
                            liveness_fd=self._invocation_descriptor(owner),
                        ),
                        workspace_id=intent.workspace_id,
                        lease_epoch=lease_epoch,
                    )
                loaded_configuration = read_configuration(
                    repository,
                    commit,
                    config,
                    liveness_fd=self._invocation_descriptor(owner),
                )
                configuration_value = _configuration_value(loaded_configuration)
                configuration = loaded_configuration or b""
                resource_token = secrets.token_hex(32)
                common_dir = (
                    source.normalized
                    if source.clone_url is None
                    else str(repository.resolve())
                )
                repository_common_dir = Path(common_dir)
                expected_repository_generation = repository_generation(
                    repository_common_dir, create=True
                )
                if expected_repository_generation is None:
                    raise WorkspaceError("Repository generation marker is unavailable")
                repository_id = self._registry.repository_id_for_common_dir(common_dir)
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
                    "repository_common_dir": str(repository_common_dir),
                    "repository_generation": expected_repository_generation,
                    "repository_id": repository_id,
                }
                resolved = self._registry.enrich_create_intent(
                    intent.operation_id,
                    workspace_id=intent.workspace_id,
                    lease_epoch=lease_epoch,
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
            self._registry.record_workspace_definition(
                workspace_id=intent.workspace_id,
                operation_id=intent.operation_id,
                lease_epoch=lease_epoch,
                definition=_create_definition(intent, target, resolved),
            )

            lifecycle = plan_create(
                (LifecycleResource("worktree", "worktree"),), start=request.start
            )
            position = 1 if source.clone_url is not None else 0
            ownership_token = str(resolved["ownership_token"])
            commit = str(resolved["created_from_sha"])
            repository_common_value = resolved.get("repository_common_dir")
            repository_generation_value = resolved.get("repository_generation")
            if (
                not isinstance(repository_common_value, str)
                or not Path(repository_common_value).is_absolute()
                or not isinstance(repository_generation_value, str)
                or not repository_generation_value
            ):
                raise WorkspaceError(
                    "Stored Workspace Repository identity is malformed"
                )
            expected_repository_common_dir = Path(repository_common_value)
            expected_repository_generation = repository_generation_value
            observation = None
            for step in lifecycle.steps:
                previous = self._registry.start_operation_step(
                    intent.operation_id,
                    position=position,
                    scope_kind="workspace",
                    scope_key=intent.workspace_id,
                    lease_epoch=lease_epoch,
                    enter_state=step.enter_state,
                    reconcile_completed=step.action == "create",
                )
                if step.action == "create":
                    observation = create_worktree(
                        repository,
                        target=target,
                        branch=request.branch,
                        commit=commit,
                        ownership_token=ownership_token,
                        reconcile=previous != "pending",
                        expected_repository_common_dir=(expected_repository_common_dir),
                        expected_repository_generation=expected_repository_generation,
                        liveness_fd=self._invocation_descriptor(owner),
                    )
                elif previous != "completed":
                    observation = inspect_owned_worktree(
                        target,
                        expected_commit=commit,
                        expected_branch=request.branch,
                        ownership_token=ownership_token,
                        expected_repository_common_dir=(expected_repository_common_dir),
                        expected_repository_generation=expected_repository_generation,
                        liveness_fd=self._invocation_descriptor(owner),
                    )
                if step.action == "create" or previous != "completed":
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
                expected_repository_common_dir=expected_repository_common_dir,
                expected_repository_generation=expected_repository_generation,
                liveness_fd=self._invocation_descriptor(owner),
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
                expected_repository_common_dir=str(expected_repository_common_dir),
                expected_repository_generation=expected_repository_generation,
                created_from_sha=commit,
                configuration=configuration,
                configuration_json=json.dumps(
                    stored_configuration_value,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                configuration_digest=str(resolved["configuration_digest"]),
                repository_id=str(resolved["repository_id"]),
                state=state,
                lease_epoch=lease_epoch,
            )
            lease_epoch = None
            aggregate, operation = self._load_completed(intent.workspace_id)
            return CreateWorkspaceResult(aggregate, operation, created=created)
        except BaseException as error:
            if (
                intent is not None
                and lease_epoch is not None
                and not isinstance(error, GitQuiescenceError)
            ):
                try:
                    self._registry.fail_create_operation(
                        operation_id=intent.operation_id,
                        workspace_id=intent.workspace_id,
                        lease_epoch=lease_epoch,
                        error=str(error),
                    )
                except RegistryError as persistence_error:
                    raise WorkspaceError(
                        f"{error}; failed to persist create failure: "
                        f"{persistence_error}"
                    ) from error
            if isinstance(error, WorkspaceError):
                raise
            if isinstance(
                error,
                (
                    GitError,
                    RegistryError,
                    OSError,
                    ValueError,
                    tomllib.TOMLDecodeError,
                ),
            ):
                raise WorkspaceError(str(error)) from error
            raise
        finally:
            if owner is not None:
                try:
                    self._finish_invocation(owner)
                except WorkspaceError as cleanup_error:
                    active = sys.exception()
                    if active is not None and active is not cleanup_error:
                        raise WorkspaceError(f"{active}; {cleanup_error}") from active
                    raise

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
        self,
        source: RepositorySource,
        intent: CreateIntentRecord,
        owner: ProcessIdentity,
        *,
        refresh_default_head: bool,
    ) -> Path:
        if source.clone_url is None:
            if source.path is None:
                raise WorkspaceError("Local repository path is unavailable")
            return source.path
        operation_id = intent.operation_id
        cache_home = self._cache_home or _xdg_home("XDG_CACHE_HOME", ".cache")
        digest = hashlib.sha256(source.normalized.encode()).hexdigest()
        registry_namespace = hashlib.sha256(
            str(self._registry.path.resolve()).encode()
        ).hexdigest()
        cache_path = (
            cache_home
            / "fangorn"
            / "repositories"
            / registry_namespace
            / f"{digest}.git"
        )
        epoch = self._registry.acquire_lease(
            scope_kind="repository",
            scope_key=source.normalized,
            operation_id=operation_id,
            owner=owner,
            owner_status=self._owner_status,
        )
        primary_error: BaseException | None = None
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
            repository = materialize_cache(
                source,
                selected,
                owner=owner,
                owner_status=self._owner_status,
                refresh=previous != "completed",
                refresh_default_head=refresh_default_head,
                liveness_fd=self._invocation_descriptor(owner),
                preparation_id=operation_id,
                namespace_root=cache_home / "fangorn",
            )
            generation = repository_generation(repository, create=entry is None)
            if generation is None:
                raise WorkspaceError("Repository cache generation marker is missing")
            if entry is not None and entry[1] != generation:
                raise WorkspaceError("Repository cache identity changed")
            if previous != "completed" or entry is None:
                self._registry.finish_cache_preparation(
                    source.normalized,
                    path=str(repository),
                    repository_generation=generation,
                    operation_id=operation_id,
                    lease_epoch=epoch,
                )
            return repository
        except BaseException as error:
            primary_error = error
            raise
        finally:
            if not isinstance(primary_error, GitQuiescenceError):
                try:
                    self._registry.release_lease(
                        scope_kind="repository",
                        scope_key=source.normalized,
                        operation_id=operation_id,
                        lease_epoch=epoch,
                    )
                except RegistryError as release_error:
                    if primary_error is not None:
                        raise WorkspaceError(
                            f"{primary_error}; failed to release Repository lease: "
                            f"{release_error}"
                        ) from primary_error
                    raise

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
            resource_states=tuple(
                ResourceState(
                    name=str(resource["name"]),
                    provisioning_status=str(resource["provisioning_status"]),  # type: ignore[arg-type]
                )
                for resource in resource_rows
            ),
            state=str(row["lifecycle_state"]),
            version=int(row["aggregate_version"]),
            path=str(row["path"]),
            branch=str(row["branch"]) if row["branch"] is not None else None,
        )
        return aggregate, Operation(
            id=str(operation_row["id"]),
            kind=str(operation_row["kind"]),
            status=str(operation_row["status"]),
        )

    def _completed_create(
        self, workspace_id: str, *, created: bool = False
    ) -> CreateWorkspaceResult:
        aggregate, operation = self._load_completed(workspace_id)
        observation = inspect_owned_worktree(
            Path(aggregate.path),
            expected_commit=None,
            expected_branch=None,
            ownership_token=aggregate.definition.resources[0].ownership_token,
        )
        current = self._registry.inspect_worktree(observation)
        aggregate = replace(aggregate, path=current.path, branch=current.branch)
        return CreateWorkspaceResult(aggregate, operation, created=created)

    def _invocation_process_identity(self) -> ProcessIdentity:
        identity = self._process_identity or _current_process_identity()
        try:
            state_descriptor = _open_registry_state_directory(
                self._invocation_root.parent, create=True
            )
            if state_descriptor is None:
                raise WorkspaceError("Cannot create Workspace invocation marker")
            os.close(state_descriptor)
            self._invocation_root.mkdir(mode=0o700, exist_ok=True)
        except (OSError, RegistryError) as error:
            raise WorkspaceError("Cannot create Workspace invocation marker") from error
        if self._invocation_root.is_symlink() or not self._invocation_root.is_dir():
            raise WorkspaceError("Workspace invocation marker directory is unsafe")
        try:
            root_descriptor = os.open(
                self._invocation_root,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
        except OSError as error:
            raise WorkspaceError(
                "Workspace invocation marker directory is unsafe"
            ) from error
        try:
            os.fchmod(root_descriptor, 0o700)
            if not _safe_invocation_descriptor(root_descriptor, directory=True):
                raise WorkspaceError("Workspace invocation marker directory is unsafe")
            process_instance_id = str(uuid4())
            marker = self._invocation_root / process_instance_id
            flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
            descriptor: int | None = None
            try:
                descriptor = os.open(
                    process_instance_id, flags, 0o600, dir_fd=root_descriptor
                )
                os.fchmod(descriptor, 0o600)
                if not _safe_invocation_descriptor(descriptor, directory=False):
                    raise OSError("Workspace invocation marker ACL is unsafe")
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if descriptor is not None:
                    os.close(descriptor)
                with suppress(OSError):
                    os.unlink(process_instance_id, dir_fd=root_descriptor)
                raise WorkspaceError(
                    "Cannot establish Workspace invocation marker"
                ) from error
        finally:
            os.close(root_descriptor)
        invocation = ProcessIdentity(
            process_instance_id=process_instance_id,
            boot_identity=identity.boot_identity,
            pid=identity.pid,
            process_start_identity=identity.process_start_identity,
        )
        with _ACTIVE_INVOCATIONS_LOCK:
            _ACTIVE_INVOCATIONS[invocation.process_instance_id] = (descriptor, marker)
        return invocation

    def _finish_invocation(self, owner: ProcessIdentity) -> None:
        with _ACTIVE_INVOCATIONS_LOCK:
            held = _ACTIVE_INVOCATIONS.pop(owner.process_instance_id, None)
        if held is None:
            return
        descriptor, marker = held
        errors: list[OSError] = []
        try:
            os.close(descriptor)
        except OSError as error:
            errors.append(error)
        try:
            cleanup = os.open(marker, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            if errors:
                raise WorkspaceError(
                    "Cannot close Workspace invocation marker"
                ) from errors[0]
            return
        except OSError as error:
            raise WorkspaceError("Cannot clean Workspace invocation marker") from error
        try:
            try:
                if not _safe_invocation_descriptor(cleanup, directory=False):
                    raise WorkspaceError("Cannot clean Workspace invocation marker")
                fcntl.flock(cleanup, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if errors:
                    raise WorkspaceError(
                        "Cannot close Workspace invocation marker"
                    ) from errors[0]
                try:
                    _defer_invocation_marker_cleanup(cleanup, marker)
                except (OSError, subprocess.SubprocessError) as error:
                    raise WorkspaceError(
                        "Cannot defer Workspace invocation marker cleanup"
                    ) from error
                return
            marker.unlink()
        except OSError as error:
            errors.append(error)
        finally:
            if cleanup >= 0:
                try:
                    os.close(cleanup)
                except OSError as error:
                    errors.append(error)
        if errors:
            raise WorkspaceError(
                "Cannot clean Workspace invocation marker"
            ) from errors[0]

    @staticmethod
    def _invocation_descriptor(owner: ProcessIdentity) -> int:
        with _ACTIVE_INVOCATIONS_LOCK:
            held = _ACTIVE_INVOCATIONS.get(owner.process_instance_id)
        if held is None:
            raise WorkspaceError("Workspace invocation marker is unavailable")
        return held[0]

    def _owner_status(self, owner: ProcessIdentity) -> str:
        try:
            boot_identity = _boot_identity()
        except WorkspaceError:
            return "inconclusive"
        if owner.boot_identity != boot_identity:
            return "dead"
        status = _process_owner_status(owner, boot_identity=boot_identity)
        if status != "dead":
            return status
        try:
            root_descriptor = os.open(
                self._invocation_root,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
        except FileNotFoundError:
            return "dead"
        except OSError:
            return "inconclusive"
        try:
            if not _safe_invocation_descriptor(root_descriptor, directory=True):
                return "inconclusive"
            try:
                descriptor = os.open(
                    owner.process_instance_id,
                    os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=root_descriptor,
                )
            except FileNotFoundError:
                return "dead"
            except OSError:
                return "inconclusive"
            try:
                if not _safe_invocation_descriptor(descriptor, directory=False):
                    return "inconclusive"
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return "live" if status == "live" else "inconclusive"
                opened = os.fstat(descriptor)
                try:
                    current = os.stat(
                        owner.process_instance_id,
                        dir_fd=root_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return "dead"
                if (
                    not stat.S_ISREG(current.st_mode)
                    or opened.st_dev != current.st_dev
                    or opened.st_ino != current.st_ino
                ):
                    return "inconclusive"
                try:
                    if os.pread(descriptor, 1, 0):
                        return "inconclusive"
                except OSError:
                    return "inconclusive"
                os.unlink(owner.process_instance_id, dir_fd=root_descriptor)
                return "dead"
            finally:
                os.close(descriptor)
        finally:
            os.close(root_descriptor)


def _safe_invocation_descriptor(descriptor: int, *, directory: bool) -> bool:
    try:
        metadata = os.fstat(descriptor)
        expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
        return (
            expected_kind(metadata.st_mode)
            and metadata.st_uid == os.geteuid()
            and not _darwin_acl_allows_write(descriptor)
        )
    except OSError:
        return False


def _create_definition(
    intent: CreateIntentRecord, target: Path, resolved: dict[str, object]
) -> dict[str, object]:
    return {
        "id": intent.workspace_id,
        "parent_id": None,
        "repository_id": str(resolved["repository_id"]),
        "created_from_sha": str(resolved["created_from_sha"]),
        "configuration": str(resolved["configuration"]),
        "configuration_value": resolved["configuration_value"],
        "configuration_digest": str(resolved["configuration_digest"]),
        "resources": [
            {
                "name": "worktree",
                "kind": "worktree",
                "adapter_id": "fangorn.git-worktree",
                "adapter_api_major": 1,
                "configuration": {},
                "external_reference": None,
                "locator": str(target),
                "ownership_token": str(resolved["ownership_token"]),
            }
        ],
    }


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
    request: CreateWorkspace, source: RepositorySource, data_home: Path | None
) -> Path:
    try:
        if request.path is not None:
            return _canonical_target(request.path.expanduser())
        if data_home is None:
            raise WorkspaceError("Workspace data home is unavailable")
    except (OSError, RuntimeError) as error:
        raise WorkspaceError("Workspace target path cannot be canonicalized") from error
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
    try:
        return _canonical_target(
            data_home
            / "fangorn"
            / "worktrees"
            / source.name
            / f"{safe_branch}-{suffix}"
        )
    except (OSError, RuntimeError) as error:
        raise WorkspaceError("Workspace target path cannot be canonicalized") from error


def _canonical_target(path: Path) -> Path:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            current.resolve(strict=True)
    resolved = absolute.resolve(strict=False)
    validate_target_path(resolved)
    return resolved


def _configuration_identity(path: Path) -> Path:
    try:
        return path.expanduser().absolute()
    except (OSError, RuntimeError) as error:
        raise WorkspaceError("Configuration path cannot be canonicalized") from error


def re_sub_path(value: str) -> str:
    rendered = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in value
    )
    return rendered.strip(".-") or "workspace"


def _configuration_value(content: bytes | None) -> dict[str, object]:
    if content is None:
        return {"schema_version": 1}
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorkspaceError("fangorn.toml must be valid UTF-8") from error
    value = tomllib.loads(decoded)
    unknown = value.keys() - {"schema_version", "services"}
    if unknown:
        raise WorkspaceError(
            f"fangorn.toml contains unsupported top-level key: {min(unknown)}"
        )
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise WorkspaceError("fangorn.toml requires schema_version = 1")
    services = value.get("services")
    if services is not None and not isinstance(services, dict):
        raise WorkspaceError("fangorn.toml services must be a table")
    if services:
        raise WorkspaceError("Service Resources are not available in this release")
    return value


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    if any(not isinstance(key, str) for key in value):
        raise TypeError("configuration keys must be strings")
    return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("configuration contains an unsupported value")


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
        value = boot_id.read_text(encoding="ascii").strip()
        if value:
            return value
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.boottime"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WorkspaceError("Cannot establish host boot identity") from error
    match = re.search(r"sec\s*=\s*(\d+).*usec\s*=\s*(\d+)", result.stdout)
    if result.returncode == 0 and match:
        return f"darwin:{match[1]}:{match[2]}"
    raise WorkspaceError("Cannot establish host boot identity")


def _process_start_identity(pid: int) -> str:
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        fields = stat_path.read_text(encoding="ascii").rsplit(")", 1)[1].split()
        return fields[19]
    except (OSError, IndexError):
        if sys.platform == "darwin":
            return _darwin_process_start_identity(pid)
        raise WorkspaceError("Cannot establish process start identity") from None


def _darwin_process_start_identity(pid: int) -> str:
    info = _darwin_process_info(pid)
    return f"darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec:06d}"


def _darwin_process_info(pid: int) -> _ProcBsdInfo:
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    except OSError as error:
        raise WorkspaceError("Cannot establish process start identity") from error
    proc_pidinfo = library.proc_pidinfo
    proc_pidinfo.argtypes = (
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    )
    proc_pidinfo.restype = ctypes.c_int
    info = _ProcBsdInfo()
    size = ctypes.sizeof(info)
    result = proc_pidinfo(pid, 3, 0, ctypes.byref(info), size)
    if (
        result != size
        or info.pbi_pid != pid
        or info.pbi_start_tvsec <= 0
        or not 0 <= info.pbi_start_tvusec < 1_000_000
    ):
        raise WorkspaceError("Cannot establish process start identity")
    return info


def _process_is_zombie(pid: int) -> bool:
    try:
        state = (
            Path(f"/proc/{pid}/stat")
            .read_text(encoding="ascii")
            .rsplit(")", 1)[1]
            .split()[0]
        )
        return state == "Z"
    except (OSError, IndexError):
        if sys.platform == "darwin":
            return _darwin_process_info(pid).pbi_status == 5
        raise WorkspaceError("Cannot establish process state") from None


def _process_owner_status(
    owner: ProcessIdentity, *, boot_identity: str | None = None
) -> str:
    if boot_identity is None:
        try:
            boot_identity = _boot_identity()
        except WorkspaceError:
            return "inconclusive"
    if owner.boot_identity != boot_identity:
        return "dead"
    try:
        os.kill(owner.pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "inconclusive"
    try:
        if _process_is_zombie(owner.pid):
            return "dead"
    except WorkspaceError:
        return "inconclusive"
    try:
        current_start = _process_start_identity(owner.pid)
    except WorkspaceError:
        return "inconclusive"
    if current_start != owner.process_start_identity:
        return "dead"
    return "live"
