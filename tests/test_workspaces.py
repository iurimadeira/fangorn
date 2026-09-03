from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest
from git_helpers import git, initialize_repository

from fangorn.workspaces import AdoptionResult, WorkspaceError, Workspaces


def create_repository(path: Path) -> str:
    initialize_repository(path)
    (path / "README.md").write_text("temporary repository\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "Initial commit")
    return git(path, "rev-parse", "HEAD")


def snapshot_tree(path: Path) -> dict[str, tuple[int, int, bytes | None]]:
    return {
        str(entry.relative_to(path)): (
            entry.lstat().st_mode,
            entry.lstat().st_mtime_ns,
            entry.read_bytes() if stat.S_ISREG(entry.lstat().st_mode) else None,
        )
        for entry in sorted(path.rglob("*"))
    }


def test_workspaces_adopts_through_public_python_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    head = create_repository(repository)
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    result = Workspaces.from_environment().adopt(repository)

    assert isinstance(result, AdoptionResult)
    assert result.created is True
    assert result.workspace.path == str(repository.resolve())
    assert result.workspace.branch == "main"
    assert result.workspace.head == head


def test_workspaces_list_does_not_initialize_missing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    workspaces = Workspaces.from_environment().list()

    assert workspaces == []
    assert not state_home.exists()


def test_workspaces_inspect_returns_current_facts_without_mutating_state_or_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    workspaces = Workspaces.from_environment()
    adopted = workspaces.adopt(repository).workspace
    git(repository, "branch", "-m", "topic")
    nested = repository / "nested"
    nested.mkdir()
    state_before = snapshot_tree(state_home)
    git_before = snapshot_tree(repository / ".git")

    inspected = workspaces.inspect(nested)

    assert inspected.id == adopted.id
    assert inspected.path == str(repository.resolve())
    assert inspected.branch == "topic"
    assert inspected.last_observed_at != adopted.last_observed_at
    assert snapshot_tree(state_home) == state_before
    assert snapshot_tree(repository / ".git") == git_before


def test_workspaces_inspect_does_not_initialize_missing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    with pytest.raises(WorkspaceError, match="Worktree is not adopted"):
        Workspaces.from_environment().inspect(repository)

    assert not state_home.exists()
    assert not (repository / ".git" / "fangorn-repository-generation").exists()
    assert not (repository / ".git" / "fangorn-worktree-generation").exists()


def test_workspaces_reads_reject_newer_registry_schema_without_migrating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    workspaces = Workspaces.from_environment()
    workspaces.adopt(repository)
    database = state_home / "fangorn" / "registry.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (2, 'future')"
    )
    connection.commit()
    migrations_before = connection.execute(
        "SELECT version, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()
    connection.close()

    with pytest.raises(WorkspaceError, match="newer than this Fangorn version: 2"):
        workspaces.list()

    connection = sqlite3.connect(database)
    try:
        assert (
            connection.execute(
                "SELECT version, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
            == migrations_before
        )
    finally:
        connection.close()
