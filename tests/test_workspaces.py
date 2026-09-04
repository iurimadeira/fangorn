from __future__ import annotations

import sqlite3
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from git_helpers import git, initialize_repository

from fangorn.registry import Registry
from fangorn.workspaces import (
    AdoptionResult,
    Binding,
    CurrentGitFacts,
    Workspace,
    WorkspaceError,
    Workspaces,
)


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
    assert isinstance(result.workspace, Workspace)
    assert isinstance(result.workspace.binding, Binding)
    assert isinstance(result.workspace.current_git_facts, CurrentGitFacts)
    assert result.workspace.binding.id
    assert result.workspace.current_git_facts.path == str(repository.resolve())
    assert result.workspace.current_git_facts.branch == "main"
    assert result.workspace.current_git_facts.head == head
    assert not hasattr(result.workspace, "last_observation_token")


def test_workspaces_rejects_symbolic_head_outside_local_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    git(repository, "tag", "release")
    git(repository, "symbolic-ref", "HEAD", "refs/tags/release")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    with pytest.raises(WorkspaceError, match="HEAD does not reference a local branch"):
        Workspaces.from_environment().adopt(repository)


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

    assert inspected.binding == adopted.binding
    assert inspected.current_git_facts.path == str(repository.resolve())
    assert inspected.current_git_facts.branch == "topic"
    assert (
        inspected.current_git_facts.observed_at != adopted.current_git_facts.observed_at
    )
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
        "INSERT INTO schema_migrations (version, applied_at) VALUES (3, 'future')"
    )
    connection.commit()
    migrations_before = connection.execute(
        "SELECT version, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()
    connection.close()

    with pytest.raises(WorkspaceError, match="newer than this Fangorn version: 3"):
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


def test_workspaces_list_uses_one_snapshot_for_schema_and_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    workspaces = Workspaces.from_environment()
    adopted = workspaces.adopt(repository).workspace
    database = state_home / "fangorn" / "registry.sqlite3"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
    finally:
        connection.close()

    schema_checked = Event()
    writer_finished = Event()
    original_check = Registry._require_supported_schema

    def pause_after_schema_check(
        registry: Registry, read_connection: sqlite3.Connection
    ) -> None:
        original_check(registry, read_connection)
        schema_checked.set()
        assert writer_finished.wait(2)

    def delete_workspace_after_schema_check() -> None:
        assert schema_checked.wait(2)
        writer = sqlite3.connect(database)
        try:
            writer.execute("DELETE FROM workspaces")
            writer.commit()
        finally:
            writer.close()
            writer_finished.set()

    monkeypatch.setattr(Registry, "_require_supported_schema", pause_after_schema_check)
    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(delete_workspace_after_schema_check)
        listed = workspaces.list()
        writer.result()

    assert listed == [adopted]


def test_workspaces_list_waits_for_concurrent_first_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    database = state_home / "fangorn" / "registry.sqlite3"
    state_home.mkdir(mode=0o700)
    database.parent.mkdir(mode=0o700)
    database.touch(mode=0o600)

    with ThreadPoolExecutor(max_workers=1) as executor:
        reader = executor.submit(Workspaces.from_environment().list)
        time.sleep(0.05)
        adopted = Workspaces.from_environment().adopt(repository).workspace
        listed = reader.result()

    assert listed in ([], [adopted])
