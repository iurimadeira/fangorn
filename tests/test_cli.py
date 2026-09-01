from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from git_helpers import git, initialize_repository

import fangorn.git as git_adapter
from fangorn.git import GitError, observe_worktree
from fangorn.registry import Registry, RegistryError

GENERATION_MARKER_NAME = "fangorn-worktree-generation"
REPOSITORY_GENERATION_MARKER_NAME = "fangorn-repository-generation"


def fangorn_executable() -> Path:
    executable = Path(sys.executable).with_name("fangorn")
    assert executable.is_file(), "fangorn console script is not installed"
    return executable


def run_fangorn(
    state_home: Path,
    *arguments: str,
    environment_overrides: Mapping[str, str | None] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["XDG_STATE_HOME"] = str(state_home)
    environment["HOME"] = str(state_home.parent / "home")
    environment.pop("XDG_CONFIG_HOME", None)
    if environment_overrides is not None:
        for name, value in environment_overrides.items():
            if value is None:
                environment.pop(name, None)
            else:
                environment[name] = value
    return subprocess.run(
        [fangorn_executable(), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        cwd=cwd,
    )


def create_repository(path: Path) -> str:
    initialize_repository(path)
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


def test_unborn_worktree_is_adoptable_and_keeps_nullable_head(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    initialize_repository(repository)
    state_home = tmp_path / "state"

    adopted = run_fangorn(state_home, "adopt", "--json", str(repository))
    inspected = run_fangorn(state_home, "info", "--json", str(repository))
    listed = run_fangorn(state_home, "list", "--json")
    human = run_fangorn(state_home, "info", str(repository))

    for result in (adopted, inspected, listed, human):
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
    adopted_workspace = cast(
        dict[str, object],
        cast(dict[str, object], json.loads(adopted.stdout))["workspace"],
    )
    inspected_workspace = cast(
        dict[str, object],
        cast(dict[str, object], json.loads(inspected.stdout))["workspace"],
    )
    listed_workspace = cast(
        dict[str, object],
        cast(
            list[object],
            cast(dict[str, object], json.loads(listed.stdout))["workspaces"],
        )[0],
    )
    for workspace in (adopted_workspace, inspected_workspace, listed_workspace):
        assert workspace["branch"] == "main"
        assert workspace["head"] is None
        assert workspace["adopted_head"] is None
    assert "Branch: main\n" in human.stdout
    assert "HEAD: (unborn)\n" in human.stdout

    connection = sqlite3.connect(state_home / "fangorn" / "registry.sqlite3")
    try:
        assert connection.execute(
            "SELECT head, adopted_head FROM workspaces"
        ).fetchone() == (None, None)
        columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(workspaces)")
        }
        assert columns["head"][3] == 0
        assert columns["adopted_head"][3] == 0
    finally:
        connection.close()


def test_cli_reports_unavailable_home_without_a_traceback(tmp_path: Path) -> None:
    result = run_fangorn(
        tmp_path / "unused-state",
        "list",
        "--json",
        environment_overrides={"XDG_STATE_HOME": None, "HOME": None},
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "HOME is unset" in result.stderr
    assert result.stderr.count("\n") == 1
    assert "Traceback" not in result.stderr


def test_git_older_than_231_fails_preflight_before_marker_creation(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    wrapper_directory = tmp_path / "bin"
    wrapper_directory.mkdir()
    wrapper = wrapper_directory / "git"
    real_git = subprocess.run(
        ["sh", "-c", "command -v git"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    wrapper.write_text(
        """#!/bin/sh
case "$*" in
    *" --version")
        printf 'git version 2.30.9\n'
        exit 0
        ;;
esac
exec "$FANGORN_TEST_REAL_GIT" "$@"
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    result = run_fangorn(
        tmp_path / "state",
        "adopt",
        "--json",
        str(repository),
        environment_overrides={
            "PATH": f"{wrapper_directory}{os.pathsep}{os.environ['PATH']}",
            "FANGORN_TEST_REAL_GIT": real_git,
        },
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Git 2.31 or newer is required; found 2.30.9" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (repository / ".git" / GENERATION_MARKER_NAME).exists()
    assert not (repository / ".git" / REPOSITORY_GENERATION_MARKER_NAME).exists()


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
    generation = cast(str, first_workspace["git_dir_generation"])
    repository_generation = cast(str, first_workspace["git_common_dir_generation"])
    marker = repository / ".git" / GENERATION_MARKER_NAME
    repository_marker = repository / ".git" / REPOSITORY_GENERATION_MARKER_NAME
    assert re.fullmatch(r"[0-9a-f]{64}", generation)
    assert re.fullmatch(r"[0-9a-f]{64}", repository_generation)
    assert repository_generation != generation
    assert marker.read_text(encoding="ascii") == f"{generation}\n"
    assert repository_marker.read_text(encoding="ascii") == (
        f"{repository_generation}\n"
    )
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert stat.S_IMODE(repository_marker.stat().st_mode) == 0o600
    assert second_workspace["git_dir_generation"] == generation
    assert second_workspace["git_common_dir_generation"] == repository_generation
    assert (state_home / "fangorn" / "registry.sqlite3").is_file()
    assert git(repository, "status", "--porcelain") == ""
    assert git(repository, "rev-parse", "HEAD") == adopted_head


def test_adopt_retries_after_generation_marker_creation_without_registry_write(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    observation = observe_worktree(repository, create_generation=True)
    marker = observation.git_dir / GENERATION_MARKER_NAME
    repository_marker = (
        observation.repository_common_dir / REPOSITORY_GENERATION_MARKER_NAME
    )

    result = run_fangorn(tmp_path / "state", "adopt", "--json", str(repository))

    assert result.returncode == 0, result.stderr
    workspace = cast(
        dict[str, object],
        cast(dict[str, object], json.loads(result.stdout))["workspace"],
    )
    assert workspace["git_dir_generation"] == observation.git_dir_generation
    assert marker.read_text(encoding="ascii") == (f"{observation.git_dir_generation}\n")
    assert repository_marker.read_text(encoding="ascii") == (
        f"{observation.git_common_dir_generation}\n"
    )


def test_concurrent_adoption_converges_on_one_generation_and_workspace(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    state_home = tmp_path / "state"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(run_fangorn, state_home, "adopt", "--json", str(repository))
            for _ in range(2)
        ]
        results = [future.result() for future in futures]

    assert all(result.returncode == 0 for result in results), [
        result.stderr for result in results
    ]
    payloads = [
        cast(dict[str, object], json.loads(result.stdout)) for result in results
    ]
    workspaces = [cast(dict[str, object], payload["workspace"]) for payload in payloads]
    assert {payload["created"] for payload in payloads} == {False, True}
    assert len({workspace["id"] for workspace in workspaces}) == 1
    assert len({workspace["git_dir_generation"] for workspace in workspaces}) == 1
    assert (
        len({workspace["git_common_dir_generation"] for workspace in workspaces}) == 1
    )
    generation = cast(str, workspaces[0]["git_dir_generation"])
    marker = repository / ".git" / GENERATION_MARKER_NAME
    assert marker.read_text(encoding="ascii") == f"{generation}\n"


def test_markerless_observation_retries_when_equivalent_adoption_wins(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    registry = Registry(tmp_path / "state" / "fangorn" / "registry.sqlite3")
    markerless = observe_worktree(
        repository,
        reserve_observation=registry.reserve_observation,
    )
    winner = observe_worktree(
        repository,
        create_generation=True,
        reserve_observation=registry.reserve_observation,
    )
    adopted, created = registry.adopt(winner)

    assert created is True
    assert registry.marker_creation_requirements(markerless) is None

    retried = observe_worktree(
        repository,
        reserve_observation=registry.reserve_observation,
    )
    assert registry.marker_creation_requirements(retried) == (False, False)
    equivalent, created = registry.adopt(retried)

    assert created is False
    assert equivalent.id == adopted.id
    assert equivalent.git_dir_generation == adopted.git_dir_generation
    assert equivalent.git_common_dir_generation == adopted.git_common_dir_generation


def test_linked_worktree_uses_its_canonical_git_directory_generation_marker(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    linked = tmp_path / "linked"
    git(repository, "worktree", "add", "-b", "topic", str(linked))

    result = run_fangorn(tmp_path / "state", "adopt", "--json", str(linked))

    assert result.returncode == 0, result.stderr
    workspace = cast(
        dict[str, object],
        cast(dict[str, object], json.loads(result.stdout))["workspace"],
    )
    marker = Path(cast(str, workspace["git_dir"])) / GENERATION_MARKER_NAME
    repository_marker = (
        Path(cast(str, workspace["repository_common_dir"]))
        / REPOSITORY_GENERATION_MARKER_NAME
    )
    assert marker.parent != repository / ".git"
    assert marker.read_text(encoding="ascii") == (
        f"{workspace['git_dir_generation']}\n"
    )
    assert repository_marker.read_text(encoding="ascii") == (
        f"{workspace['git_common_dir_generation']}\n"
    )


def test_marker_publication_does_not_require_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)

    def reject_hard_link(*_arguments: object, **_options: object) -> None:
        raise AssertionError("marker publication attempted a hard link")

    monkeypatch.setattr(os, "link", reject_hard_link)

    observation = observe_worktree(repository, create_generation=True)

    assert observation.git_dir_generation is not None
    assert observation.git_common_dir_generation is not None


def test_marker_publication_and_concurrent_winner_fsync_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    fsynced_modes: list[int] = []
    real_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        fsynced_modes.append(os.fstat(descriptor).st_mode)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)

    observation = observe_worktree(repository, create_generation=True)
    assert sum(stat.S_ISDIR(mode) for mode in fsynced_modes) >= 2
    fsynced_modes.clear()

    winner = git_adapter._create_generation_marker(observation.git_dir)

    assert winner == observation.git_dir_generation
    assert any(stat.S_ISDIR(mode) for mode in fsynced_modes)


def test_marker_cleanup_does_not_mask_a_publication_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    git_dir = repository / ".git"
    real_unlink = Path.unlink

    def fail_replace(*_arguments: object, **_options: object) -> None:
        raise OSError("publication failed")

    def fail_pending_cleanup(path: Path, *, missing_ok: bool = False) -> None:
        if path.name == f".{GENERATION_MARKER_NAME}.pending":
            raise OSError("cleanup failed")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(os, "replace", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_pending_cleanup)

    with pytest.raises(GitError, match="publication failed"):
        git_adapter._create_generation_marker(git_dir)


def test_marker_cleanup_failure_without_a_primary_error_is_a_git_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    git_dir = repository / ".git"
    pending = git_dir / f".{GENERATION_MARKER_NAME}.pending"
    pending.write_text("incomplete", encoding="ascii")
    real_unlink = Path.unlink

    def fail_pending_cleanup(path: Path, *, missing_ok: bool = False) -> None:
        if path == pending:
            raise OSError("cleanup failed")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_pending_cleanup)

    with pytest.raises(GitError, match="cleanup failed"):
        git_adapter._create_generation_marker(git_dir)


def test_marker_lock_succeeds_after_bounded_contention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    monotonic_values = iter((10.0, 10.01, 10.02))
    sleeps: list[float] = []

    def contend_then_succeed(_descriptor: int, operation: int) -> None:
        nonlocal attempts
        assert operation & fcntl.LOCK_NB
        attempts += 1
        if attempts < 3:
            raise BlockingIOError

    monkeypatch.setattr("fangorn.git.fcntl.flock", contend_then_succeed)
    monkeypatch.setattr("fangorn.git.time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("fangorn.git.time.sleep", sleeps.append)

    git_adapter._acquire_marker_lock(123)

    assert attempts == 3
    assert sleeps == [
        git_adapter.MARKER_LOCK_RETRY_SECONDS,
        git_adapter.MARKER_LOCK_RETRY_SECONDS,
    ]


def test_marker_lock_contention_times_out_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monotonic_values = iter((0.0, git_adapter.MARKER_LOCK_TIMEOUT_SECONDS + 0.001))

    def always_contended(_descriptor: int, _operation: int) -> None:
        raise BlockingIOError

    monkeypatch.setattr("fangorn.git.fcntl.flock", always_contended)
    monkeypatch.setattr("fangorn.git.time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(
        "fangorn.git.time.sleep",
        lambda _duration: pytest.fail("timeout path must not sleep"),
    )

    with pytest.raises(GitError, match="Timed out waiting for Fangorn marker lock"):
        git_adapter._acquire_marker_lock(123)


def test_timed_out_contender_leaves_owner_pending_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    git_dir = repository / ".git"
    pending = git_dir / f".{GENERATION_MARKER_NAME}.pending"
    marker = git_dir / GENERATION_MARKER_NAME
    owner_generation = "a" * 64
    pending.write_text(f"{owner_generation}\n", encoding="ascii")

    def time_out(_descriptor: int) -> None:
        raise GitError("Timed out waiting for Fangorn marker lock; retry the command")

    monkeypatch.setattr(git_adapter, "_acquire_marker_lock", time_out)

    with pytest.raises(GitError, match="Timed out waiting for Fangorn marker lock"):
        git_adapter._create_generation_marker(git_dir)

    assert pending.read_text(encoding="ascii") == f"{owner_generation}\n"
    os.replace(pending, marker)
    assert git_adapter._read_generation_marker(git_dir) == owner_generation


def test_adopt_target_wins_over_inherited_repository_git_environment(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target_head = create_repository(target)
    contaminating_repository = tmp_path / "contaminating"
    create_repository(contaminating_repository)

    result = run_fangorn(
        tmp_path / "state",
        "adopt",
        "--json",
        str(target),
        environment_overrides={
            "GIT_DIR": str(contaminating_repository / ".git"),
            "GIT_WORK_TREE": str(contaminating_repository),
        },
    )

    assert result.returncode == 0, result.stderr
    payload = cast(dict[str, object], json.loads(result.stdout))
    workspace = cast(dict[str, object], payload["workspace"])
    assert workspace["path"] == str(target.resolve())
    assert workspace["git_dir"] == str((target / ".git").resolve())
    assert workspace["head"] == target_head


def test_adopt_nested_path_ignores_inherited_git_discovery_environment(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    nested = repository / "nested" / "directory"
    nested.mkdir(parents=True)

    result = run_fangorn(
        tmp_path / "state",
        "adopt",
        "--json",
        str(nested),
        environment_overrides={
            "GIT_CEILING_DIRECTORIES": str(repository),
            "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    payload = cast(dict[str, object], json.loads(result.stdout))
    workspace = cast(dict[str, object], payload["workspace"])
    assert workspace["path"] == str(repository.resolve())


def test_each_observation_attempt_is_timestamped_before_its_first_git_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    wrapper_directory = tmp_path / "bin"
    wrapper_directory.mkdir()
    wrapper = wrapper_directory / "git"
    events = tmp_path / "events"
    marker = tmp_path / "branch-changed"
    real_git = subprocess.run(
        ["sh", "-c", "command -v git"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    wrapper.write_text(
        """#!/bin/sh
printf 'git\n' >> "$FANGORN_TEST_EVENTS"
if [ "$*" = "-C $FANGORN_TEST_REPOSITORY rev-parse --verify --quiet HEAD" ] \
    && [ ! -e "$FANGORN_TEST_MARKER" ]; then
    "$FANGORN_TEST_REAL_GIT" "$@"
    : > "$FANGORN_TEST_MARKER"
    "$FANGORN_TEST_REAL_GIT" -C "$FANGORN_TEST_REPOSITORY" branch -m observed-later
    exit 0
fi
exec "$FANGORN_TEST_REAL_GIT" "$@"
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{wrapper_directory}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FANGORN_TEST_EVENTS", str(events))
    monkeypatch.setenv("FANGORN_TEST_MARKER", str(marker))
    monkeypatch.setenv("FANGORN_TEST_REAL_GIT", real_git)
    monkeypatch.setenv("FANGORN_TEST_REPOSITORY", str(repository))

    timestamp_index = 0

    def timestamp() -> str:
        nonlocal timestamp_index
        timestamp_index += 1
        with events.open("a", encoding="utf-8") as stream:
            stream.write("timestamp\n")
        return f"2026-01-01T00:00:00.00000{timestamp_index}Z"

    monkeypatch.setattr(git_adapter, "_timestamp", timestamp)

    observation = observe_worktree(repository)

    recorded_events = events.read_text(encoding="utf-8").splitlines()
    assert observation.branch == "observed-later"
    assert recorded_events[0] == "timestamp"
    assert recorded_events.count("timestamp") == 2
    for index, event in enumerate(recorded_events):
        if event == "timestamp":
            assert recorded_events[index + 1] == "git"


def test_observation_reserves_token_between_snapshots_and_retries_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    old = git_adapter._capture_snapshot(repository)
    new = replace(old, branch="newer-facts", head="f" * 40)
    snapshots = [old, new, new, new]
    tokens = iter((11, 12))
    events: list[str] = []

    def capture_snapshot(
        _path: Path, *, create_generation: bool = False
    ) -> git_adapter._Snapshot:
        del create_generation
        snapshot = snapshots.pop(0)
        events.append(f"capture:{snapshot.branch}")
        return snapshot

    def reserve_observation() -> int:
        token = next(tokens)
        events.append(f"reserve:{token}")
        return token

    monkeypatch.setattr(git_adapter, "_capture_snapshot", capture_snapshot)

    observation = observe_worktree(
        repository,
        reserve_observation=reserve_observation,
    )

    assert events == [
        "capture:main",
        "reserve:11",
        "capture:newer-facts",
        "capture:newer-facts",
        "reserve:12",
        "capture:newer-facts",
    ]
    assert observation.observation_token == 12
    assert observation.branch == "newer-facts"
    assert observation.head == "f" * 40


def test_adopt_rejects_non_utf8_git_output_without_a_traceback(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    wrapper_directory = tmp_path / "bin"
    wrapper_directory.mkdir()
    wrapper = wrapper_directory / "git"
    real_git = subprocess.run(
        ["sh", "-c", "command -v git"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    wrapper.write_text(
        """#!/bin/sh
case "$*" in
    *"rev-parse --show-toplevel")
        printf 'repository-\\377\\n'
        exit 0
        ;;
esac
exec "$FANGORN_TEST_REAL_GIT" "$@"
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    result = run_fangorn(
        tmp_path / "state",
        "adopt",
        "--json",
        str(repository),
        environment_overrides={
            "PATH": f"{wrapper_directory}{os.pathsep}{os.environ['PATH']}",
            "FANGORN_TEST_REAL_GIT": real_git,
        },
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Git output is not valid UTF-8" in result.stderr
    assert "Traceback" not in result.stderr


def test_adopt_ignores_inherited_git_config_source_overrides(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    target_head = create_repository(repository)
    contaminating_worktree = tmp_path / "contaminating-worktree"
    create_repository(contaminating_worktree)
    config = tmp_path / "contaminating.gitconfig"
    config.write_text(
        f"[core]\n\tworktree = {contaminating_worktree}\n",
        encoding="utf-8",
    )
    wrapper_directory = tmp_path / "bin"
    wrapper_directory.mkdir()
    wrapper = wrapper_directory / "git"
    real_git = subprocess.run(
        ["sh", "-c", "command -v git"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    wrapper.write_text(
        """#!/bin/sh
if [ -n "$GIT_CONFIG_GLOBAL" ] \
    || [ -n "$GIT_CONFIG_SYSTEM" ] \
    || [ -n "$GIT_CONFIG_NOSYSTEM" ]; then
    echo "Git config-source overrides reached child" >&2
    exit 2
fi
exec "$FANGORN_TEST_REAL_GIT" "$@"
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    result = run_fangorn(
        tmp_path / "state",
        "adopt",
        "--json",
        str(repository),
        environment_overrides={
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_SYSTEM": str(config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": f"{wrapper_directory}{os.pathsep}{os.environ['PATH']}",
            "FANGORN_TEST_REAL_GIT": real_git,
        },
    )

    assert result.returncode == 0, result.stderr
    workspace = cast(
        dict[str, object],
        cast(dict[str, object], json.loads(result.stdout))["workspace"],
    )
    assert workspace["path"] == str(repository.resolve())
    assert workspace["head"] == target_head


def test_git_fixture_ignores_inherited_commit_signing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "signing.gitconfig"
    config.write_text("[commit]\n\tgpgSign = true\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))

    repository = tmp_path / "repository"
    head = create_repository(repository)

    assert git(repository, "rev-parse", "HEAD") == head


def test_observation_reports_the_latest_failure_after_an_earlier_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    snapshot = git_adapter._capture_snapshot(repository)
    changed = replace(snapshot, branch="changed-during-observation")
    outcomes: list[git_adapter._Snapshot | GitError] = [
        snapshot,
        changed,
        GitError("middle Git failure"),
        GitError("final Git failure"),
    ]

    def capture_snapshot(
        _path: Path, *, create_generation: bool = False
    ) -> git_adapter._Snapshot:
        del create_generation
        outcome = outcomes.pop(0)
        if isinstance(outcome, GitError):
            raise outcome
        return outcome

    monkeypatch.setattr(git_adapter, "_capture_snapshot", capture_snapshot)

    with pytest.raises(GitError, match="final Git failure"):
        observe_worktree(repository)


def test_adopt_preserves_whitespace_in_git_reported_paths(tmp_path: Path) -> None:
    repository = tmp_path / "  repository  "
    create_repository(repository)

    result = run_fangorn(tmp_path / "state", "adopt", "--json", str(repository))

    assert result.returncode == 0, result.stderr
    payload = cast(dict[str, object], json.loads(result.stdout))
    workspace = cast(dict[str, object], payload["workspace"])
    assert workspace["path"] == str(repository.resolve())
    assert workspace["git_dir"] == str((repository / ".git").resolve())


def test_adopt_revalidates_a_deterministic_concurrent_branch_change(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    wrapper_directory = tmp_path / "bin"
    wrapper_directory.mkdir()
    wrapper = wrapper_directory / "git"
    marker = tmp_path / "branch-changed"
    real_git = subprocess.run(
        ["sh", "-c", "command -v git"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    wrapper.write_text(
        """#!/bin/sh
if [ "$*" = "-C $FANGORN_TEST_REPOSITORY rev-parse --verify --quiet HEAD" ] \
    && [ ! -e "$FANGORN_TEST_MARKER" ]; then
    "$FANGORN_TEST_REAL_GIT" "$@"
    : > "$FANGORN_TEST_MARKER"
    "$FANGORN_TEST_REAL_GIT" -C "$FANGORN_TEST_REPOSITORY" branch -m observed-later
    exit 0
fi
exec "$FANGORN_TEST_REAL_GIT" "$@"
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    result = run_fangorn(
        tmp_path / "state",
        "adopt",
        "--json",
        str(repository),
        environment_overrides={
            "PATH": f"{wrapper_directory}{os.pathsep}{os.environ['PATH']}",
            "FANGORN_TEST_MARKER": str(marker),
            "FANGORN_TEST_REAL_GIT": real_git,
            "FANGORN_TEST_REPOSITORY": str(repository),
        },
    )

    assert result.returncode == 0, result.stderr
    payload = cast(dict[str, object], json.loads(result.stdout))
    workspace = cast(dict[str, object], payload["workspace"])
    assert marker.is_file()
    assert workspace["branch"] == "observed-later"
    assert git(repository, "branch", "--show-current") == "observed-later"


def test_adopt_reports_symbolic_ref_and_subprocess_os_failures(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    wrapper_directory = tmp_path / "wrapper-bin"
    wrapper_directory.mkdir()
    wrapper = wrapper_directory / "git"
    real_git = subprocess.run(
        ["sh", "-c", "command -v git"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    wrapper.write_text(
        """#!/bin/sh
case "$*" in
    *"symbolic-ref --quiet --short HEAD")
        echo "forced symbolic-ref failure" >&2
        exit 2
        ;;
esac
exec "$FANGORN_TEST_REAL_GIT" "$@"
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    symbolic_ref_failure = run_fangorn(
        tmp_path / "state-symbolic",
        "adopt",
        str(repository),
        environment_overrides={
            "PATH": f"{wrapper_directory}{os.pathsep}{os.environ['PATH']}",
            "FANGORN_TEST_REAL_GIT": real_git,
        },
    )

    assert symbolic_ref_failure.returncode != 0
    assert symbolic_ref_failure.stdout == ""
    assert "forced symbolic-ref failure" in symbolic_ref_failure.stderr

    blocked_directory = tmp_path / "blocked-bin"
    blocked_directory.mkdir()
    blocked_git = blocked_directory / "git"
    blocked_git.write_text("not executable\n", encoding="utf-8")
    blocked_git.chmod(0o644)
    os_failure = run_fangorn(
        tmp_path / "state-os",
        "adopt",
        str(repository),
        environment_overrides={"PATH": str(blocked_directory)},
    )

    assert os_failure.returncode != 0
    assert os_failure.stdout == ""
    assert "Cannot run Git: Permission denied" in os_failure.stderr


def test_adopt_represents_only_detached_head_as_no_branch(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    git(repository, "checkout", "--detach")

    result = run_fangorn(tmp_path / "state", "adopt", "--json", str(repository))

    assert result.returncode == 0, result.stderr
    payload = cast(dict[str, object], json.loads(result.stdout))
    workspace = cast(dict[str, object], payload["workspace"])
    assert workspace["branch"] is None


def test_adopt_distinguishes_missing_paths_from_resolution_failures(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing"
    missing_result = run_fangorn(tmp_path / "state-missing", "adopt", str(missing))
    assert missing_result.returncode != 0
    assert "Path does not exist" in missing_result.stderr

    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    loop_result = run_fangorn(tmp_path / "state-loop", "adopt", str(loop))
    assert loop_result.returncode != 0
    assert loop_result.stdout == ""
    assert "Cannot resolve path" in loop_result.stderr


@pytest.mark.parametrize("command", ["adopt", "info"])
def test_command_path_validation_stays_inside_sanitized_error_boundary(
    tmp_path: Path,
    command: str,
) -> None:
    unsafe = tmp_path / "not-directory\nansi\x1b[31m"
    unsafe.write_text("not a directory\n", encoding="utf-8")

    result = run_fangorn(
        tmp_path / "state",
        command,
        "--json",
        str(unsafe),
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Path is not a directory" in result.stderr
    assert "\\x0a" in result.stderr
    assert "\\x1b" in result.stderr
    assert "\x1b" not in result.stderr
    assert result.stderr.count("\n") == 1
    assert "Traceback" not in result.stderr


def test_git_reported_symlink_loop_is_translated_to_git_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reported = tmp_path / "reported-loop"
    real_resolve = Path.resolve

    def resolve(path: Path, *, strict: bool = False) -> Path:
        if path == reported:
            raise RuntimeError("synthetic symlink loop")
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)

    with pytest.raises(GitError, match="invalid Git common directory") as raised:
        git_adapter._required_path(str(reported), "Git common directory")

    assert isinstance(raised.value.__cause__, RuntimeError)


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


def test_registry_rejects_a_changed_worktree_generation(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    registry = Registry(tmp_path / "state" / "fangorn" / "registry.sqlite3")
    observation = observe_worktree(
        repository,
        create_generation=True,
        reserve_observation=registry.reserve_observation,
    )
    adopted, created = registry.adopt(observation)
    replacement = "f" * 64 if observation.git_dir_generation != "f" * 64 else "e" * 64
    recreated = replace(
        observation,
        git_dir_generation=replacement,
    )

    assert created is True
    for operation in (registry.get_by_worktree, registry.adopt):
        with pytest.raises(RegistryError, match=r"generation marker changed.*drifted"):
            operation(recreated)
    listed = registry.list_workspaces()
    assert len(listed) == 1
    assert listed[0].git_dir_generation == adopted.git_dir_generation
    assert listed[0].last_observed_at == adopted.last_observed_at
    assert listed[0].branch == adopted.branch


@pytest.mark.parametrize(
    "marker_state",
    ["missing", "malformed", "changed", "symlink", "directory"],
)
def test_registered_worktree_generation_marker_drift_fails_closed(
    tmp_path: Path,
    marker_state: str,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    state_home = tmp_path / "state"
    adopted = run_fangorn(state_home, "adopt", "--json", str(repository))
    assert adopted.returncode == 0, adopted.stderr
    workspace = cast(
        dict[str, object],
        cast(dict[str, object], json.loads(adopted.stdout))["workspace"],
    )
    marker = Path(cast(str, workspace["git_dir"])) / GENERATION_MARKER_NAME
    generation = cast(str, workspace["git_dir_generation"])

    marker.unlink()
    if marker_state == "malformed":
        marker.write_text("not-a-generation\n", encoding="ascii")
    elif marker_state == "changed":
        replacement = "f" * 64 if generation != "f" * 64 else "e" * 64
        marker.write_text(f"{replacement}\n", encoding="ascii")
        marker.chmod(0o600)
    elif marker_state == "symlink":
        target = tmp_path / "generation-target"
        target.write_text(f"{generation}\n", encoding="ascii")
        marker.symlink_to(target)
    elif marker_state == "directory":
        marker.mkdir()

    inspected = run_fangorn(state_home, "info", "--json", str(repository))
    readopted = run_fangorn(state_home, "adopt", "--json", str(repository))

    for result in (inspected, readopted):
        assert result.returncode != 0
        assert result.stdout == ""
        assert "generation marker" in result.stderr
        assert "identity" in result.stderr
        assert "Traceback" not in result.stderr
    if marker_state == "missing":
        assert not marker.exists()


def test_same_git_directory_path_with_a_replacement_generation_is_rejected(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    state_home = tmp_path / "state"
    adopted = run_fangorn(state_home, "adopt", "--json", str(repository))
    assert adopted.returncode == 0, adopted.stderr
    workspace = cast(
        dict[str, object],
        cast(dict[str, object], json.loads(adopted.stdout))["workspace"],
    )
    old_generation = cast(str, workspace["git_dir_generation"])
    git_dir = repository / ".git"
    old_git_dir = repository / ".git-first-generation"
    git_dir.rename(old_git_dir)
    shutil.copytree(
        old_git_dir,
        git_dir,
        ignore=shutil.ignore_patterns(GENERATION_MARKER_NAME),
    )
    replacement = "f" * 64 if old_generation != "f" * 64 else "e" * 64
    marker = git_dir / GENERATION_MARKER_NAME
    marker.write_text(f"{replacement}\n", encoding="ascii")
    marker.chmod(0o600)

    result = run_fangorn(state_home, "adopt", "--json", str(repository))

    assert result.returncode != 0
    assert result.stdout == ""
    assert "generation marker changed" in result.stderr
    assert "identity" in result.stderr
    listed = run_fangorn(state_home, "list", "--json")
    listed_workspace = cast(
        dict[str, object],
        cast(
            list[object],
            cast(dict[str, object], json.loads(listed.stdout))["workspaces"],
        )[0],
    )
    assert listed_workspace["git_dir_generation"] == old_generation


@pytest.mark.parametrize(
    "marker_state",
    ["missing", "malformed", "changed", "symlink", "directory"],
)
def test_registered_repository_generation_marker_drift_fails_closed(
    tmp_path: Path,
    marker_state: str,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    state_home = tmp_path / "state"
    adopted = run_fangorn(state_home, "adopt", "--json", str(repository))
    assert adopted.returncode == 0, adopted.stderr
    workspace = cast(
        dict[str, object],
        cast(dict[str, object], json.loads(adopted.stdout))["workspace"],
    )
    marker = (
        Path(cast(str, workspace["repository_common_dir"]))
        / REPOSITORY_GENERATION_MARKER_NAME
    )
    generation = cast(str, workspace["git_common_dir_generation"])

    marker.unlink()
    if marker_state == "malformed":
        marker.write_text("not-a-generation\n", encoding="ascii")
    elif marker_state == "changed":
        replacement = "f" * 64 if generation != "f" * 64 else "e" * 64
        marker.write_text(f"{replacement}\n", encoding="ascii")
        marker.chmod(0o600)
    elif marker_state == "symlink":
        target = tmp_path / "repository-generation-target"
        target.write_text(f"{generation}\n", encoding="ascii")
        marker.symlink_to(target)
    elif marker_state == "directory":
        marker.mkdir()

    inspected = run_fangorn(state_home, "info", "--json", str(repository))
    readopted = run_fangorn(state_home, "adopt", "--json", str(repository))

    for result in (inspected, readopted):
        assert result.returncode != 0
        assert result.stdout == ""
        assert "repository generation marker" in result.stderr
        assert "identity" in result.stderr
        assert "Traceback" not in result.stderr
    if marker_state == "missing":
        assert not marker.exists()


def test_replacement_repository_cannot_reuse_identity_for_a_new_linked_worktree(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    state_home = tmp_path / "state"
    adopted = run_fangorn(state_home, "adopt", "--json", str(repository))
    assert adopted.returncode == 0, adopted.stderr
    adopted_workspace = cast(
        dict[str, object],
        cast(dict[str, object], json.loads(adopted.stdout))["workspace"],
    )
    repository_id = adopted_workspace["repository_id"]
    git_dir = repository / ".git"
    old_git_dir = repository / ".git-original-repository"
    git_dir.rename(old_git_dir)
    shutil.copytree(
        old_git_dir,
        git_dir,
        ignore=shutil.ignore_patterns(
            GENERATION_MARKER_NAME,
            REPOSITORY_GENERATION_MARKER_NAME,
        ),
    )
    linked = tmp_path / "replacement-linked"
    git(repository, "worktree", "add", "-b", "replacement-linked", str(linked))

    result = run_fangorn(state_home, "adopt", "--json", str(linked))

    assert result.returncode != 0
    assert result.stdout == ""
    assert "repository generation marker is missing" in result.stderr
    assert "identity" in result.stderr
    assert not (git_dir / REPOSITORY_GENERATION_MARKER_NAME).exists()
    linked_git_dir = Path(
        git(linked, "rev-parse", "--path-format=absolute", "--git-dir")
    )
    assert not (linked_git_dir / GENERATION_MARKER_NAME).exists()
    listed = run_fangorn(state_home, "list", "--json")
    workspaces = cast(
        list[dict[str, object]],
        cast(dict[str, object], json.loads(listed.stdout))["workspaces"],
    )
    assert len(workspaces) == 1
    assert workspaces[0]["repository_id"] == repository_id


def test_causal_observation_token_wins_despite_reversed_write_and_clock_rollback(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    registry = Registry(tmp_path / "state" / "fangorn" / "registry.sqlite3")
    older = replace(
        observe_worktree(
            repository,
            create_generation=True,
            reserve_observation=registry.reserve_observation,
        ),
        observed_at="2030-01-01T00:00:00.000001Z",
    )
    older_token = cast(int, older.observation_token)
    git(repository, "branch", "-m", "newer-branch")
    (repository / "newer.txt").write_text("newer\n", encoding="utf-8")
    git(repository, "add", "newer.txt")
    git(repository, "commit", "-m", "Newer observation")
    newer = replace(
        observe_worktree(
            repository,
            reserve_observation=registry.reserve_observation,
        ),
        observed_at="2020-01-01T00:00:00.000001Z",
    )
    newer_token = cast(int, newer.observation_token)

    refreshed, was_created = registry.adopt(newer)
    reversed_write = registry.get_by_worktree(older)

    assert was_created is True
    assert older_token < newer_token
    assert refreshed.branch == "newer-branch"
    assert refreshed.last_observed_at == newer.observed_at
    assert reversed_write.branch == "newer-branch"
    assert reversed_write.head == newer.head
    assert reversed_write.path == str(newer.path)
    assert reversed_write.last_observed_at == newer.observed_at
    connection = sqlite3.connect(registry.path)
    try:
        assert connection.execute(
            "SELECT last_observation_token FROM workspaces"
        ).fetchone() == (newer_token,)
        assert connection.execute(
            "SELECT current_token FROM observation_clock WHERE singleton = 1"
        ).fetchone() == (newer_token,)
    finally:
        connection.close()


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


def test_human_output_escapes_terminal_controls_but_json_stays_exact(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    state_home = tmp_path / "state"
    adopted = run_fangorn(state_home, "adopt", "--json", str(repository))
    assert adopted.returncode == 0, adopted.stderr
    database = state_home / "fangorn" / "registry.sqlite3"
    unsafe_path = (
        "/workspace/line\nansi\x1b[31m/tab\t/del\x7f/c1\x85/"
        "bidi\u202e/line\u2028/paragraph\u2029/tag\U000e0001"
    )
    unsafe_branch = "topic\r\n\x01\x1b]0;title\x07\x9f/isolate\u2066"
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE workspaces SET path = ?, branch = ?", (unsafe_path, unsafe_branch)
    )
    connection.commit()
    connection.close()

    human = run_fangorn(state_home, "list")
    machine = run_fangorn(state_home, "list", "--json")

    assert human.returncode == 0, human.stderr
    assert machine.returncode == 0, machine.stderr
    assert (
        "topic\\x0d\\x0a\\x01\\x1b]0;title\\x07\\x9f/isolate\\u2066\t"
        "/workspace/line\\x0aansi\\x1b[31m/tab\\x09/del\\x7f/c1\\x85/"
        "bidi\\u202e/line\\u2028/paragraph\\u2029/tag\\U000e0001\n" in human.stdout
    )
    for control in (
        "\r",
        "\x01",
        "\x1b",
        "\x07",
        "\x7f",
        "\x85",
        "\u202e",
        "\u2028",
        "\u2029",
        "\u2066",
        "\U000e0001",
    ):
        assert control not in human.stdout
    payload = cast(dict[str, object], json.loads(machine.stdout))
    workspace = cast(dict[str, object], cast(list[object], payload["workspaces"])[0])
    assert workspace["path"] == unsafe_path
    assert workspace["branch"] == unsafe_branch


def test_cli_errors_escape_terminal_controls_for_every_command(tmp_path: Path) -> None:
    unsafe_suffix = "line\nansi\x1b[31m"
    missing = tmp_path / f"missing-{unsafe_suffix}"
    adopt_error = run_fangorn(tmp_path / "adopt-state", "adopt", "--json", str(missing))

    repository = tmp_path / f"repository-{unsafe_suffix}"
    create_repository(repository)
    info_error = run_fangorn(tmp_path / "info-state", "info", "--json", str(repository))

    list_state = tmp_path / f"state-{unsafe_suffix}"
    list_state.mkdir()
    (list_state / "fangorn").write_text("not a directory\n", encoding="utf-8")
    list_error = run_fangorn(list_state, "list", "--json")

    for result in (adopt_error, info_error, list_error):
        assert result.returncode != 0
        assert result.stdout == ""
        assert "\\x0a" in result.stderr
        assert "\\x1b" in result.stderr
        assert "\x1b" not in result.stderr
        assert result.stderr.count("\n") == 1
        assert "Traceback" not in result.stderr


def test_relative_xdg_state_home_uses_home_fallback_across_working_directories(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    first_cwd = tmp_path / "first-cwd"
    second_cwd = tmp_path / "second-cwd"
    first_cwd.mkdir()
    second_cwd.mkdir()
    home = tmp_path / "home"
    environment = {"XDG_STATE_HOME": "relative-state", "HOME": str(home)}

    adopted = run_fangorn(
        tmp_path / "unused-state",
        "adopt",
        "--json",
        str(repository),
        environment_overrides=environment,
        cwd=first_cwd,
    )
    inspected = run_fangorn(
        tmp_path / "unused-state",
        "info",
        "--json",
        str(repository),
        environment_overrides=environment,
        cwd=second_cwd,
    )

    assert adopted.returncode == 0, adopted.stderr
    assert inspected.returncode == 0, inspected.stderr
    assert (home / ".local" / "state" / "fangorn" / "registry.sqlite3").is_file()
    assert not (first_cwd / "relative-state").exists()
    assert not (second_cwd / "relative-state").exists()


def test_registry_migration_enforces_immutable_binding_and_foreign_keys(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    create_repository(repository)
    state_home = tmp_path / "state"
    adopted = run_fangorn(state_home, "adopt", "--json", str(repository))
    assert adopted.returncode == 0, adopted.stderr
    other_repository = tmp_path / "other-repository"
    create_repository(other_repository)
    other_adopted = run_fangorn(state_home, "adopt", "--json", str(other_repository))
    assert other_adopted.returncode == 0, other_adopted.stderr
    database = state_home / "fangorn" / "registry.sqlite3"

    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,)]
        repository_columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(repositories)")
        }
        workspace_columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(workspaces)")
        }
        assert repository_columns["id"][3] == 1
        assert repository_columns["created_observation_token"][3] == 1
        assert workspace_columns["id"][3] == 1
        assert workspace_columns["last_observation_token"][3] == 1
        assert connection.execute(
            "SELECT singleton, current_token FROM observation_clock"
        ).fetchone() == (1, 4)
        repositories = connection.execute(
            "SELECT id, git_common_dir FROM repositories ORDER BY git_common_dir"
        ).fetchall()
        assert len(repositories) == 2
        workspace = connection.execute(
            """
            SELECT id, repository_id, git_dir, git_dir_generation, path, head
            FROM workspaces
            WHERE path = ?
            """,
            (str(repository.resolve()),),
        ).fetchone()
        assert workspace is not None
        workspace_id, repository_id, git_dir, _generation, path, head = workspace
        repository_row = connection.execute(
            """
            SELECT git_common_dir_generation
            FROM repositories
            WHERE id = ?
            """,
            (repository_id,),
        ).fetchone()
        assert repository_row is not None
        assert re.fullmatch(r"[0-9a-f]{64}", repository_row[0])
        other_repository_id = next(
            row[0] for row in repositories if row[0] != repository_id
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO workspaces (
                    id, repository_id, git_dir, git_dir_generation,
                    path, branch, head,
                    adopted_head, created_at, last_observed_at
                ) VALUES (
                    'duplicate', ?, ?, 'different-generation', ?,
                    'main', ?, ?, 'now', 'now'
                )
                """,
                (other_repository_id, git_dir, path, head, head),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO repositories (
                    id, git_common_dir, git_common_dir_generation, created_at
                ) VALUES (
                    'invalid-repository-generation', '/git/common/invalid',
                    'not-a-generation', 'now'
                )
                """
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO workspaces (
                    id, repository_id, git_dir, git_dir_generation,
                    path, branch, head,
                    adopted_head, created_at, last_observed_at
                ) VALUES (
                    'invalid-generation', ?, '/git/invalid-generation',
                    'not-a-generation', '/worktree/invalid-generation',
                    'main', 'head', 'head', 'now', 'now'
                )
                """,
                (repository_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO workspaces (
                    id, repository_id, git_dir, git_dir_generation,
                    path, branch, head,
                    adopted_head, created_at, last_observed_at
                ) VALUES (
                    'orphan', 'missing', '/git/orphan', '1:2',
                    '/worktree/orphan', NULL, 'head', 'head', 'now', 'now'
                )
                """
            )
        connection.rollback()
        workspace_updates = (
            ("id", "changed-id"),
            ("repository_id", other_repository_id),
            ("git_dir", "/different/git-dir"),
            ("git_dir_generation", "different-generation"),
        )
        for column, value in workspace_updates:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(
                    f"UPDATE workspaces SET {column} = ? WHERE id = ?",
                    (value, workspace_id),
                )
            connection.rollback()
        repository_common_dir = next(
            row[1] for row in repositories if row[0] == repository_id
        )
        for column, value in (
            ("id", "changed-repository-id"),
            ("git_common_dir", f"{repository_common_dir}-changed"),
            ("git_common_dir_generation", "f" * 64),
            ("created_observation_token", 999),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(
                    f"UPDATE repositories SET {column} = ? WHERE id = ?",
                    (value, repository_id),
                )
            connection.rollback()
        unique_indexes = []
        for index in connection.execute("PRAGMA index_list(workspaces)").fetchall():
            if index[2] != 1:
                continue
            columns = [
                row[2]
                for row in connection.execute(
                    f"PRAGMA index_info('{index[1]}')"
                ).fetchall()
            ]
            unique_indexes.append(columns)
        assert ["repository_id", "git_dir"] not in unique_indexes
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


def test_registry_repairs_private_state_and_database_permissions(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "state"
    state_directory = state_home / "fangorn"
    state_directory.mkdir(parents=True, mode=0o777)
    state_directory.chmod(0o777)
    database = state_directory / "registry.sqlite3"
    database.touch(mode=0o666)
    database.chmod(0o666)

    result = run_fangorn(state_home, "list", "--json")

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(state_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_registry_filesystem_failures_are_concise_cli_errors(tmp_path: Path) -> None:
    blocked_state = tmp_path / "blocked-state"
    blocked_state.mkdir()
    (blocked_state / "fangorn").write_text("not a directory\n", encoding="utf-8")

    directory_failure = run_fangorn(blocked_state, "list", "--json")

    assert directory_failure.returncode != 0
    assert directory_failure.stdout == ""
    assert "Registry state directory unavailable" in directory_failure.stderr
    assert "Traceback" not in directory_failure.stderr

    database_state = tmp_path / "database-state"
    database_path = database_state / "fangorn" / "registry.sqlite3"
    database_path.mkdir(parents=True)
    database_failure = run_fangorn(database_state, "list", "--json")
    assert database_failure.returncode != 0
    assert database_failure.stdout == ""
    assert "Registry database unavailable" in database_failure.stderr
    assert "Traceback" not in database_failure.stderr

    symlink_state = tmp_path / "symlink-state"
    symlink_state.mkdir()
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    (symlink_state / "fangorn").symlink_to(redirected, target_is_directory=True)
    symlink_failure = run_fangorn(symlink_state, "list", "--json")
    assert symlink_failure.returncode != 0
    assert symlink_failure.stdout == ""
    assert "Registry state directory unavailable" in symlink_failure.stderr
    assert "symlink" in symlink_failure.stderr
    assert "Traceback" not in symlink_failure.stderr
