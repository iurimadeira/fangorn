from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fangorn.git import GitError, observe_worktree
from fangorn.registry import Registry, RegistryError
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
                record, created = self._registry.adopt(observation)
                return AdoptionResult(workspace=_workspace(record), created=created)
            raise RegistryError(
                "Concurrent equivalent adoption did not settle; retry the command"
            )
        except (GitError, RegistryError) as error:
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
