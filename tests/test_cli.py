from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest


def fangorn_executable() -> Path:
    executable = Path(sys.executable).with_name("fangorn")
    assert executable.is_file(), "fangorn console script is not installed"
    return executable


def run_fangorn(state_home: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["XDG_STATE_HOME"] = str(state_home)
    environment["HOME"] = str(state_home.parent / "home")
    environment.pop("XDG_CONFIG_HOME", None)
    return subprocess.run(
        [fangorn_executable(), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", repository, *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def create_repository(path: Path) -> str:
    path.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main", path],
        check=True,
        capture_output=True,
        text=True,
    )
    git(path, "config", "user.name", "Fangorn Test")
    git(path, "config", "user.email", "fangorn@example.invalid")
    (path / "README.md").write_text("temporary repository\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "Initial commit")
    return git(path, "rev-parse", "HEAD")


def test_help_exposes_bootstrap_commands() -> None:
    result = subprocess.run(
        [fangorn_executable(), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Worktree-native workspace families" in result.stdout
    assert "adopt" in result.stdout
    assert "info" in result.stdout
    assert "list" in result.stdout


def test_adopt_json_binds_and_reuses_git_identity(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    adopted_head = create_repository(repository)
    state_home = tmp_path / "state"

    first = run_fangorn(state_home, "adopt", "--json", str(repository))
    second = run_fangorn(state_home, "adopt", "--json", str(repository))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stderr == ""
    assert second.stderr == ""
    first_payload = cast(dict[str, object], json.loads(first.stdout))
    second_payload = cast(dict[str, object], json.loads(second.stdout))
    assert first_payload["schema_version"] == 1
    assert first_payload["created"] is True
    assert second_payload["created"] is False
    first_workspace = cast(dict[str, object], first_payload["workspace"])
    second_workspace = cast(dict[str, object], second_payload["workspace"])
    assert second_workspace["id"] == first_workspace["id"]
    assert second_workspace["repository_id"] == first_workspace["repository_id"]
    assert second_workspace["created_at"] == first_workspace["created_at"]
    assert second_workspace["adopted_head"] == first_workspace["adopted_head"]
    UUID(cast(str, first_workspace["id"]))
    UUID(cast(str, first_workspace["repository_id"]))
    assert first_workspace["repository_common_dir"] == str(
        (repository / ".git").resolve()
    )
    assert first_workspace["git_dir"] == str((repository / ".git").resolve())
    assert first_workspace["path"] == str(repository.resolve())
    assert first_workspace["branch"] == "main"
    assert first_workspace["head"] == adopted_head
    assert first_workspace["adopted_head"] == adopted_head
    assert (state_home / "fangorn" / "registry.sqlite3").is_file()
    assert git(repository, "status", "--porcelain") == ""
    assert git(repository, "rev-parse", "HEAD") == adopted_head


def test_info_resolves_and_reconciles_only_an_adopted_worktree(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    state_home = tmp_path / "state"
    adopted = run_fangorn(state_home, "adopt", "--json", str(repository))
    adopted_payload = cast(dict[str, object], json.loads(adopted.stdout))
    adopted_workspace = cast(dict[str, object], adopted_payload["workspace"])
    workspace_id = adopted_workspace["id"]
    nested_path = repository / "nested" / "directory"
    nested_path.mkdir(parents=True)
    git(repository, "branch", "-m", "topic")

    machine = run_fangorn(state_home, "info", "--json", str(nested_path))
    human = run_fangorn(state_home, "info", str(repository))

    assert machine.returncode == 0, machine.stderr
    assert machine.stderr == ""
    machine_payload = cast(dict[str, object], json.loads(machine.stdout))
    assert machine_payload["schema_version"] == 1
    workspace = cast(dict[str, object], machine_payload["workspace"])
    assert workspace["id"] == workspace_id
    assert workspace["path"] == str(repository.resolve())
    assert workspace["branch"] == "topic"
    assert human.returncode == 0, human.stderr
    assert human.stdout.startswith(f"Workspace {workspace_id}\n")
    assert f"Path: {repository.resolve()}\n" in human.stdout
    assert "Branch: topic\n" in human.stdout

    other_repository = tmp_path / "other"
    create_repository(other_repository)
    unregistered = run_fangorn(state_home, "info", "--json", str(other_repository))
    assert unregistered.returncode != 0
    assert unregistered.stdout == ""
    assert "Worktree is not adopted" in unregistered.stderr


def test_list_emits_deterministic_human_json_and_ndjson(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    linked = tmp_path / "linked"
    git(repository, "worktree", "add", "-b", "topic", str(linked))
    state_home = tmp_path / "state"
    main_adopt = run_fangorn(state_home, "adopt", "--json", str(repository))
    linked_adopt = run_fangorn(state_home, "adopt", "--json", str(linked))
    main_workspace = cast(
        dict[str, object],
        cast(dict[str, object], json.loads(main_adopt.stdout))["workspace"],
    )
    linked_workspace = cast(
        dict[str, object],
        cast(dict[str, object], json.loads(linked_adopt.stdout))["workspace"],
    )

    machine = run_fangorn(state_home, "list", "--json")
    stream = run_fangorn(state_home, "list", "--ndjson")
    human = run_fangorn(state_home, "list")

    assert machine.returncode == 0, machine.stderr
    assert stream.returncode == 0, stream.stderr
    assert human.returncode == 0, human.stderr
    assert machine.stderr == stream.stderr == human.stderr == ""
    machine_payload = cast(dict[str, object], json.loads(machine.stdout))
    assert machine_payload["schema_version"] == 1
    workspaces = cast(list[dict[str, object]], machine_payload["workspaces"])
    assert [workspace["path"] for workspace in workspaces] == sorted(
        [str(repository.resolve()), str(linked.resolve())]
    )
    assert main_workspace["id"] != linked_workspace["id"]
    assert main_workspace["repository_id"] == linked_workspace["repository_id"]
    stream_payloads = [
        cast(dict[str, object], json.loads(line)) for line in stream.stdout.splitlines()
    ]
    assert [payload["schema_version"] for payload in stream_payloads] == [1, 1]
    assert [
        cast(dict[str, object], payload["workspace"])["id"]
        for payload in stream_payloads
    ] == [workspace["id"] for workspace in workspaces]
    assert human.stdout.startswith("Workspace ID\tBranch\tPath\n")
    for workspace in workspaces:
        assert (
            f"{workspace['id']}\t{workspace['branch']}\t{workspace['path']}\n"
            in human.stdout
        )


def test_registry_migration_enforces_immutable_binding_and_foreign_keys(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    state_home = tmp_path / "state"
    adopted = run_fangorn(state_home, "adopt", "--json", str(repository))
    assert adopted.returncode == 0, adopted.stderr
    database = state_home / "fangorn" / "registry.sqlite3"

    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,)]
        workspace = connection.execute(
            """
            SELECT repository_id, git_dir, path, head
            FROM workspaces
            """
        ).fetchone()
        assert workspace is not None
        repository_id, git_dir, path, head = workspace
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO workspaces (
                    id, repository_id, git_dir, path, branch, head,
                    adopted_head, created_at, last_observed_at
                ) VALUES ('duplicate', ?, ?, ?, 'main', ?, ?, 'now', 'now')
                """,
                (repository_id, git_dir, path, head, head),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO workspaces (
                    id, repository_id, git_dir, path, branch, head,
                    adopted_head, created_at, last_observed_at
                ) VALUES (
                    'orphan', 'missing', '/git/orphan', '/worktree/orphan',
                    NULL, 'head', 'head', 'now', 'now'
                )
                """
            )
    finally:
        connection.close()


def test_failed_migration_rolls_back_its_schema_changes(tmp_path: Path) -> None:
    state_home = tmp_path / "state"
    database = state_home / "fangorn" / "registry.sqlite3"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE repositories (broken TEXT)")
    connection.commit()
    connection.close()

    result = run_fangorn(state_home, "list", "--json")

    assert result.returncode != 0
    assert result.stdout == ""
    connection = sqlite3.connect(database)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert "schema_migrations" not in tables


def test_registry_contention_fails_after_a_bounded_wait(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    state_home = tmp_path / "state"
    adopted = run_fangorn(state_home, "adopt", "--json", str(repository))
    assert adopted.returncode == 0, adopted.stderr
    database = state_home / "fangorn" / "registry.sqlite3"
    connection = sqlite3.connect(database, isolation_level=None)
    connection.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        result = run_fangorn(state_home, "list", "--json")
    finally:
        elapsed = time.monotonic() - started
        connection.rollback()
        connection.close()

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Registry remained busy for 2 seconds" in result.stderr
    assert 1.5 <= elapsed < 6
