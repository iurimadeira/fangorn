from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fangorn.git import GitError, observe_worktree
from fangorn.registry import Registry, RegistryError
from fangorn.registry import WorkspaceRecord as WorkspaceRecord

ADOPTION_ATTEMPTS = 3


class WorkspaceError(RuntimeError):
    """Workspace lifecycle operation failed."""


@dataclass(frozen=True)
class AdoptionResult:
    workspace: WorkspaceRecord
    created: bool


class Workspaces:
    """Public application facade for Workspace lifecycle operations."""

    def __init__(self, registry: Registry) -> None:
        self._registry = registry

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
                workspace, created = self._registry.adopt(observation)
                return AdoptionResult(workspace=workspace, created=created)
            raise RegistryError(
                "Concurrent equivalent adoption did not settle; retry the command"
            )
        except (GitError, RegistryError) as error:
            raise WorkspaceError(str(error)) from error

    def list(self) -> list[WorkspaceRecord]:
        try:
            return self._registry.list_workspaces()
        except RegistryError as error:
            raise WorkspaceError(str(error)) from error

    def inspect(self, path: Path) -> WorkspaceRecord:
        try:
            observation = observe_worktree(path)
            return self._registry.inspect_worktree(observation)
        except (GitError, RegistryError) as error:
            raise WorkspaceError(str(error)) from error
