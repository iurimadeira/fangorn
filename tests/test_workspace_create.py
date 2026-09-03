from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from git_helpers import git, initialize_repository

from fangorn.registry import Registry
from fangorn.workspaces import CreateWorkspace, WorkspaceError, Workspaces


def create_repository(path: Path) -> str:
    initialize_repository(path)
    (path / "README.md").write_text("root\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "root")
    return git(path, "rev-parse", "HEAD")


def facade(tmp_path: Path) -> Workspaces:
    return Workspaces(
        Registry(tmp_path / "state" / "registry.sqlite3"),
        data_home=tmp_path / "data",
        cache_home=tmp_path / "cache",
    )


@pytest.mark.parametrize(
    ("start", "expected_state"),
    [(True, "ready"), (False, "stopped")],
)
def test_create_root_headless_local_workspace(
    tmp_path: Path, start: bool, expected_state: str
) -> None:
    repository = tmp_path / "repository"
    created_from_sha = create_repository(repository)
    target = tmp_path / "worktrees" / expected_state

    result = facade(tmp_path).create(
        CreateWorkspace(
            repository=str(repository),
            branch=f"topic-{expected_state}",
            path=target,
            headless=True,
            start=start,
        )
    )

    assert result.created is True
    assert result.workspace.definition.created_from_sha == created_from_sha
    assert result.workspace.definition.parent_id is None
    assert result.workspace.state == expected_state
    assert result.workspace.path == str(target.resolve())
    assert result.workspace.branch == f"topic-{expected_state}"
    assert [
        (resource.name, resource.kind, resource.adapter_id)
        for resource in result.workspace.definition.resources
    ] == [("worktree", "worktree", "fangorn.git-worktree")]
    assert result.workspace.definition.resources[0].provisioning_status == "created"
    assert result.operation.status == "completed"
    assert git(target, "rev-parse", "HEAD") == created_from_sha


def test_equivalent_retry_reuses_resolved_values_and_completed_operation(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    created_from_sha = create_repository(repository)
    target = tmp_path / "worktrees" / "topic"
    workspaces = facade(tmp_path)
    request = CreateWorkspace(
        repository=str(repository),
        branch="topic",
        path=target,
        request_id="retry-1",
        headless=True,
    )

    first = workspaces.create(request)
    (repository / "later.txt").write_text("later\n", encoding="utf-8")
    git(repository, "add", "later.txt")
    git(repository, "commit", "-m", "later")
    retried = workspaces.create(request)

    assert retried.created is False
    assert retried.workspace.definition.id == first.workspace.definition.id
    assert retried.workspace.definition.created_from_sha == created_from_sha
    assert retried.workspace.path == first.workspace.path
    assert retried.operation.id == first.operation.id
    assert retried.operation.status == "completed"


def test_reused_request_id_with_divergent_definition_conflicts_before_effects(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    workspaces = facade(tmp_path)
    first_target = tmp_path / "worktrees" / "first"
    second_target = tmp_path / "worktrees" / "second"
    workspaces.create(
        CreateWorkspace(
            repository=str(repository),
            branch="first",
            path=first_target,
            request_id="same-key",
            headless=True,
        )
    )

    with pytest.raises(WorkspaceError, match=r"idempotency key.*different request"):
        workspaces.create(
            CreateWorkspace(
                repository=str(repository),
                branch="second",
                path=second_target,
                request_id="same-key",
                headless=True,
            )
        )

    assert not second_target.exists()


def test_created_workspace_remains_visible_through_schema_1_reads(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    target = tmp_path / "worktrees" / "topic"
    workspaces = facade(tmp_path)

    created = workspaces.create(
        CreateWorkspace(
            repository=str(repository),
            branch="topic",
            path=target,
            headless=True,
        )
    ).workspace
    legacy = workspaces.inspect(target)

    assert legacy.binding.id == created.definition.id
    assert legacy.current_git_facts.path == created.path
    with sqlite3.connect(tmp_path / "state" / "registry.sqlite3") as connection:
        assert connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone() == (2,)
