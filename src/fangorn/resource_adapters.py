from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol, cast

from fangorn.configuration import ConsentStore
from fangorn.git import GitError, GitQuiescenceError
from fangorn.git_worktree import (
    _run_supervised_git,
    create_worktree,
    delete_owned_worktree,
    observe_lifecycle_worktree,
)

ObservationStatus = Literal["absent", "stopped", "ready", "degraded", "unknown"]
Continuation = Literal["safe", "unsafe", "unknown"]


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Probe contains duplicate JSON fields")
        value[key] = item
    return value


class ResourceDefinition(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def kind(self) -> str: ...
    @property
    def adapter_id(self) -> str: ...
    @property
    def adapter_api_major(self) -> int: ...
    @property
    def configuration(self) -> Mapping[str, object]: ...
    @property
    def external_reference(self) -> str | None: ...
    @property
    def locator(self) -> str: ...
    @property
    def ownership_token(self) -> str: ...


@dataclass(frozen=True)
class AdapterDescriptor:
    id: str
    api_major: int
    kinds: frozenset[str]
    capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True)
class GitWorktreeContext:
    repository: Path
    common_dir: Path
    common_generation: str
    commit: str
    branch: str


@dataclass(frozen=True)
class AdapterContext:
    workspace_id: str
    operation_id: str | None
    worktree: Path
    configuration_digest: str
    consent: ConsentStore
    scripts: Mapping[str, Path]
    liveness_fd: int | None = None
    force: bool = False
    git: GitWorktreeContext | None = None


@dataclass(frozen=True)
class AdapterObservation:
    status: ObservationStatus
    locator: str
    ownership_token: str | None
    error: str | None = None


@dataclass(frozen=True)
class AdapterResult:
    success: bool
    error: str | None = None
    continuation: Continuation = "unknown"


class ResourceAdapter(Protocol):
    @property
    def descriptor(self) -> AdapterDescriptor: ...
    def inspect(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterObservation: ...
    def create(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult: ...
    def start(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult: ...
    def stop(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult: ...
    def delete(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult: ...


class CommandAdapter:
    descriptor = AdapterDescriptor("fangorn.command", 1, frozenset({"service"}))

    def inspect(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterObservation:
        try:
            stdout = self._run("inspect", definition, context)
            value = json.loads(stdout, object_pairs_hook=_unique_json_object)
            if not isinstance(value, dict) or value.keys() != {
                "schema_version",
                "status",
                "locator",
                "ownership_token",
            }:
                raise ValueError(
                    "Probe requires exactly schema_version, status, locator, "
                    "ownership_token"
                )
            if type(value["schema_version"]) is not int or value["schema_version"] != 1:
                raise ValueError("Probe requires schema_version = 1")
            status = value["status"]
            if status not in ("absent", "stopped", "ready", "degraded", "unknown"):
                raise ValueError("Probe status is invalid")
            if value["locator"] != definition.locator:
                raise ValueError("Probe locator does not match Resource")
            owner = value["ownership_token"]
            if status == "absent":
                if owner is not None:
                    raise ValueError("Absent probe must have null ownership_token")
            elif owner != definition.ownership_token:
                raise ValueError("Probe ownership_token does not match Resource")
            return AdapterObservation(status, definition.locator, owner)
        except GitQuiescenceError:
            raise
        except (ValueError, OSError, GitError, RecursionError) as error:
            return AdapterObservation("unknown", definition.locator, None, str(error))

    def create(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult:
        if "create" not in definition.configuration:
            return AdapterResult(True, continuation="safe")
        return self._mutate("create", definition, context)

    def start(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult:
        return self._mutate("start", definition, context)

    def stop(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult:
        return self._mutate("stop", definition, context)

    def delete(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult:
        return self._mutate("delete", definition, context)

    def _mutate(
        self, action: str, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult:
        observed = self.inspect(definition, context)
        if observed.status == "unknown":
            return AdapterResult(
                False, observed.error or "Resource observation unknown", "unsafe"
            )
        if (
            (action in ("stop", "delete") and observed.status == "absent")
            or (action == "start" and observed.status == "ready")
            or (action == "stop" and observed.status == "stopped")
            or (action == "create" and observed.status != "absent")
        ):
            return AdapterResult(True, continuation="safe")
        try:
            self._run(action, definition, context)
            return AdapterResult(True, continuation="safe")
        except GitQuiescenceError:
            raise
        except (ValueError, OSError, GitError) as error:
            return AdapterResult(False, str(error), "unknown")

    def _run(
        self, action: str, definition: ResourceDefinition, context: AdapterContext
    ) -> bytes:
        context.consent.require(context.configuration_digest)
        argv = list(cast(Iterable[str], definition.configuration[action]))
        argv = [str(context.scripts.get(arg, arg)) for arg in argv]
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C",
            "LC_ALL": "C",
        }
        for name in cast(
            Iterable[str], definition.configuration.get("environment", ())
        ):
            if name not in os.environ:
                raise ValueError(f"Environment reference is unavailable: {name}")
            environment[name] = os.environ[name]
        environment.update(
            {
                "FANGORN_WORKSPACE_ID": context.workspace_id,
                "FANGORN_RESOURCE_NAME": definition.name,
                "FANGORN_RESOURCE_LOCATOR": definition.locator,
                "FANGORN_OWNERSHIP_TOKEN": definition.ownership_token,
            }
        )
        cwd = context.worktree / str(definition.configuration.get("cwd", "."))
        descriptor = os.open(cwd, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        owned_liveness = os.pipe() if context.liveness_fd is None else None
        liveness = owned_liveness[0] if owned_liveness else context.liveness_fd
        assert liveness is not None  # noqa: S101 -- guaranteed by construction
        try:
            result = _run_supervised_git(
                argv,
                environment,
                liveness_fd=liveness,
                finish_on_parent_exit=False,
                extra_fds=(descriptor,),
                working_directory_fd=descriptor,
                timeout_seconds=int(
                    cast(int, definition.configuration.get("timeout", 60))
                ),
                capture_limit=1024 * 1024,
            )
        finally:
            os.close(descriptor)
            if owned_liveness:
                for fd in owned_liveness:
                    os.close(fd)
        if result.returncode:
            raise ValueError(
                f"Service {action} failed with exit status {result.returncode}"
            )
        return result.stdout


class GitWorktreeAdapter:
    descriptor = AdapterDescriptor(
        "fangorn.git-worktree",
        1,
        frozenset({"worktree"}),
        frozenset({"dirty_worktree"}),
    )

    def inspect(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterObservation:
        try:
            git = self._git_context(definition, context)
            observed = observe_lifecycle_worktree(
                Path(definition.locator),
                ownership_token=definition.ownership_token,
                common_dir=git.common_dir,
                common_generation=git.common_generation,
                liveness_fd=context.liveness_fd,
            )
            return AdapterObservation(
                "absent" if observed is None else "ready",
                definition.locator,
                None if observed is None else definition.ownership_token,
            )
        except GitQuiescenceError:
            raise
        except (ValueError, OSError, GitError) as error:
            return AdapterObservation("unknown", definition.locator, None, str(error))

    def create(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult:
        return self._mutate("create", definition, context)

    def start(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult:
        return self._mutate("start", definition, context)

    def stop(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult:
        return self._mutate("stop", definition, context)

    def delete(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> AdapterResult:
        return self._mutate("delete", definition, context)

    def _mutate(
        self,
        action: Literal["create", "start", "stop", "delete"],
        definition: ResourceDefinition,
        context: AdapterContext,
    ) -> AdapterResult:
        try:
            git = self._git_context(definition, context)
            if context.liveness_fd is None:
                raise ValueError("Git Worktree mutations require a liveness descriptor")
        except ValueError as error:
            return AdapterResult(False, str(error), "unsafe")
        try:
            if action == "create":
                create_worktree(
                    git.repository,
                    target=Path(definition.locator),
                    branch=git.branch,
                    commit=git.commit,
                    ownership_token=definition.ownership_token,
                    reconcile=True,
                    expected_repository_common_dir=git.common_dir,
                    expected_repository_generation=git.common_generation,
                    liveness_fd=context.liveness_fd,
                )
            elif action == "delete":
                delete_owned_worktree(
                    Path(definition.locator),
                    ownership_token=definition.ownership_token,
                    common_dir=git.common_dir,
                    common_generation=git.common_generation,
                    force=context.force,
                    liveness_fd=context.liveness_fd,
                )
            else:
                observed = self.inspect(definition, context)
                if observed.status == "unknown":
                    return AdapterResult(False, observed.error, "unsafe")
                if action == "start" and observed.status == "absent":
                    return AdapterResult(False, "Worktree Resource is absent", "unsafe")
            return AdapterResult(True, continuation="safe")
        except GitQuiescenceError:
            raise
        except (ValueError, OSError, GitError) as error:
            return AdapterResult(False, str(error), "unknown")

    def _git_context(
        self, definition: ResourceDefinition, context: AdapterContext
    ) -> GitWorktreeContext:
        if not isinstance(context.git, GitWorktreeContext):
            raise ValueError("GitWorktreeContext is required")
        if (
            definition.kind != "worktree"
            or definition.adapter_id != self.descriptor.id
            or definition.adapter_api_major != self.descriptor.api_major
            or Path(definition.locator) != context.worktree
        ):
            raise ValueError("Git Worktree definition does not match its context")
        return context.git


def discover_adapters() -> Mapping[str, ResourceAdapter]:
    adapters: dict[str, ResourceAdapter] = {}
    candidates: list[ResourceAdapter] = [GitWorktreeAdapter(), CommandAdapter()]
    for entry in entry_points(group="fangorn.resource_adapters"):
        try:
            loaded = entry.load()
            candidates.append(loaded() if callable(loaded) else loaded)
        except Exception as error:
            raise ValueError(
                f"Cannot load Resource adapter entry point {entry.name}"
            ) from error
    for adapter in candidates:
        descriptor = getattr(adapter, "descriptor", None)
        if (
            not isinstance(descriptor, AdapterDescriptor)
            or not isinstance(descriptor.id, str)
            or not descriptor.id
            or "." not in descriptor.id
        ):
            raise ValueError("Resource adapter descriptor is invalid")
        if type(descriptor.api_major) is not int or descriptor.api_major != 1:
            raise ValueError(
                f"Incompatible Resource adapter API major: {descriptor.id}"
            )
        if (
            not isinstance(descriptor.kinds, frozenset)
            or not descriptor.kinds
            or not descriptor.kinds
            <= {
                "worktree",
                "service",
                "terminal",
            }
        ):
            raise ValueError(f"Resource adapter kinds are invalid: {descriptor.id}")
        if not isinstance(descriptor.capabilities, frozenset) or any(
            not isinstance(capability, str) or not capability
            for capability in descriptor.capabilities
        ):
            raise ValueError(
                f"Resource adapter capabilities are invalid: {descriptor.id}"
            )
        if descriptor.id in adapters:
            raise ValueError(f"Duplicate Resource adapter ID: {descriptor.id}")
        if any(
            not callable(getattr(adapter, action, None))
            for action in ("create", "inspect", "start", "stop", "delete")
        ):
            raise ValueError(
                f"Resource adapter lacks required operations: {descriptor.id}"
            )
        adapters[descriptor.id] = adapter
    return MappingProxyType(adapters)


def validate_resources(
    resources: Iterable[ResourceDefinition], adapters: Mapping[str, ResourceAdapter]
) -> None:
    for resource in resources:
        adapter = adapters.get(resource.adapter_id)
        if adapter is None:
            raise ValueError(
                f"Resource adapter is not installed: {resource.adapter_id}"
            )
        if adapter.descriptor.api_major != resource.adapter_api_major:
            raise ValueError(
                f"Incompatible Resource adapter API major: {resource.adapter_id}"
            )
        if resource.kind not in adapter.descriptor.kinds:
            raise ValueError(
                f"Resource adapter does not support kind {resource.kind}: "
                f"{resource.adapter_id}"
            )
