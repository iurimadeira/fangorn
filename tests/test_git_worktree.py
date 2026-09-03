from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

import pytest
from git_helpers import git, initialize_repository

from fangorn.git import GitError, establish_worktree_generation
from fangorn.git_worktree import (
    RepositorySource,
    create_worktree,
    inspect_owned_worktree,
    materialize_cache,
    normalize_repository_source,
    read_configuration,
    resolve_commit,
)
from fangorn.registry import ProcessIdentity


def repository(path: Path) -> str:
    initialize_repository(path)
    (path / "README.md").write_text("root\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "root")
    return git(path, "rev-parse", "HEAD")


def test_repository_source_normalizes_urls_and_local_identity(tmp_path: Path) -> None:
    local = tmp_path / "local"
    repository(local)

    local_source = normalize_repository_source(str(local))
    url_source = normalize_repository_source("HTTPS://Example.COM/acme//widgets.git/")

    assert local_source.path == local.resolve()
    assert local_source.normalized == str((local / ".git").resolve())
    assert local_source.name == "local"
    assert url_source.normalized == "https://example.com/acme/widgets.git"
    assert url_source.clone_url == url_source.normalized
    assert url_source.name == "widgets"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("ftp://example.com/repository.git", "Unsupported repository URL scheme"),
        ("https://user@example.com/repository.git", "must not contain credentials"),
        ("https://example.com/repository.git?token=value", "query or fragment"),
    ],
)
def test_repository_source_rejects_unsafe_urls(source: str, message: str) -> None:
    with pytest.raises(GitError, match=message):
        normalize_repository_source(source)


def test_repository_source_rejects_missing_local_path(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="Repository path is unavailable"):
        normalize_repository_source(str(tmp_path / "missing"))


def test_commit_and_configuration_reads_are_immutable(tmp_path: Path) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    explicit = tmp_path / "explicit.toml"
    explicit.write_text("schema_version = 1\n", encoding="utf-8")

    assert resolve_commit(source, None) == commit
    assert read_configuration(source, commit, explicit) == b"schema_version = 1\n"
    assert read_configuration(source, commit, None) == b""
    with pytest.raises(GitError, match="unknown-ref"):
        resolve_commit(source, "unknown-ref")

    symlink = tmp_path / "linked.toml"
    symlink.symlink_to(explicit)
    with pytest.raises(GitError, match="non-symlink"):
        read_configuration(source, commit, symlink)
    with pytest.raises(GitError, match="Configuration is unavailable"):
        read_configuration(source, commit, tmp_path / "missing.toml")


def test_materialize_local_source_requires_and_returns_path(tmp_path: Path) -> None:
    source = RepositorySource("local", tmp_path, None, "local")

    assert materialize_cache(source, tmp_path / "unused") == tmp_path
    with pytest.raises(GitError, match="Local repository path is unavailable"):
        materialize_cache(
            RepositorySource("local", None, None, "local"), tmp_path / "unused"
        )


def test_clone_cache_reuses_only_matching_bare_repository(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    repository(first)
    repository(second)
    cache = tmp_path / "cache" / "repository.git"
    source = normalize_repository_source(first.as_uri())

    assert materialize_cache(source, cache) == cache
    assert materialize_cache(source, cache) == cache

    git(cache, "remote", "set-url", "origin", second.as_uri())
    with pytest.raises(GitError, match="belongs to another source"):
        materialize_cache(source, cache)

    invalid_cache = tmp_path / "cache" / "not-bare.git"
    invalid_cache.mkdir()
    with pytest.raises(GitError, match="not a bare repository"):
        materialize_cache(source, invalid_cache)


def test_clone_cache_removes_only_proven_dead_private_clone(tmp_path: Path) -> None:
    source_repository = tmp_path / "source"
    repository(source_repository)
    cache = tmp_path / "cache" / "repository.git"
    abandoned = cache.parent / "clone-dead-private"
    abandoned.mkdir(parents=True)
    dead = ProcessIdentity("dead", "boot", 1001, "start")
    (abandoned / "owner.json").write_text(
        json.dumps(
            {
                "process_instance_id": dead.process_instance_id,
                "boot_identity": dead.boot_identity,
                "pid": dead.pid,
                "process_start_identity": dead.process_start_identity,
            }
        ),
        encoding="utf-8",
    )
    live = ProcessIdentity("live", "boot", 1002, "start")

    materialize_cache(
        normalize_repository_source(source_repository.as_uri()),
        cache,
        owner=live,
        owner_status=lambda owner: "dead" if owner == dead else "live",
    )

    assert not abandoned.exists()


def test_clone_cache_cleans_invocation_when_owner_metadata_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_repository = tmp_path / "source"
    repository(source_repository)
    cache = tmp_path / "cache" / "repository.git"
    original = Path.write_text

    def fail_owner(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if path.name == "owner.json":
            raise OSError("metadata unavailable")
        return original(path, data, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, "write_text", fail_owner)
    with pytest.raises(OSError, match="metadata unavailable"):
        materialize_cache(
            normalize_repository_source(source_repository.as_uri()),
            cache,
            owner=ProcessIdentity("live", "boot", 1002, "start"),
            owner_status=lambda _owner: "live",
        )

    assert list(cache.parent.glob("clone-*")) == []


def test_git_adapter_forces_stable_diagnostics_locale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, str] = {}

    def run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured.update(cast(dict[str, str], kwargs["env"]))
        return subprocess.CompletedProcess([], 0, b"a" * 40, b"")

    monkeypatch.setattr(subprocess, "run", run)
    resolve_commit(tmp_path, None)

    assert captured["LC_ALL"] == "C"
    assert captured["LANG"] == "C"


def test_worktree_adapter_reconciles_only_its_owned_definition(tmp_path: Path) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    target = tmp_path / "target"
    token = "a" * 64
    created = create_worktree(
        source,
        target=target,
        branch="topic",
        commit=commit,
        ownership_token=token,
        reconcile=False,
    )

    assert created.git_dir_generation == token
    assert (
        create_worktree(
            source,
            target=target,
            branch="topic",
            commit=commit,
            ownership_token=token,
            reconcile=True,
        ).git_dir_generation
        == token
    )
    with pytest.raises(GitError, match="already exists"):
        create_worktree(
            source,
            target=target,
            branch="topic",
            commit=commit,
            ownership_token=token,
            reconcile=False,
        )
    with pytest.raises(GitError, match="interrupted create"):
        create_worktree(
            source,
            target=target,
            branch="topic",
            commit="0" * 40,
            ownership_token=token,
            reconcile=True,
        )
    with pytest.raises(GitError, match="already exists"):
        create_worktree(
            source,
            target=tmp_path / "other-target",
            branch="main",
            commit=commit,
            ownership_token="c" * 64,
            reconcile=False,
        )
    with pytest.raises(GitError, match="ownership token"):
        inspect_owned_worktree(
            target,
            expected_commit=commit,
            expected_branch="topic",
            ownership_token="b" * 64,
        )
    with pytest.raises(GitError, match="does not match"):
        inspect_owned_worktree(
            target,
            expected_commit="0" * 40,
            expected_branch="topic",
            ownership_token=token,
        )


def test_worktree_recovery_never_claims_markerless_final_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    commit = repository(source)
    unrelated = tmp_path / "unrelated"
    git(tmp_path, "clone", str(source), str(unrelated))
    git(unrelated, "checkout", "-b", "topic")

    with pytest.raises(GitError, match="not owned"):
        create_worktree(
            source,
            target=unrelated,
            branch="topic",
            commit=commit,
            ownership_token="d" * 64,
            reconcile=True,
        )


def test_worktree_recovery_publishes_staged_target_after_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fangorn.git_worktree as adapter

    source = tmp_path / "source"
    commit = repository(source)
    target = tmp_path / "target"
    token = "e" * 64
    interrupted = False

    def interrupt_once(directory: Path, ownership_token: str) -> str:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise OSError("interrupted before ownership marker")
        return establish_worktree_generation(directory, ownership_token)

    monkeypatch.setattr(adapter, "establish_worktree_generation", interrupt_once)
    with pytest.raises(OSError, match="before ownership marker"):
        create_worktree(
            source,
            target=target,
            branch="topic",
            commit=commit,
            ownership_token=token,
            reconcile=False,
        )
    assert not target.exists()

    recovered = create_worktree(
        source,
        target=target,
        branch="topic",
        commit=commit,
        ownership_token=token,
        reconcile=True,
    )

    assert recovered.path == target.resolve()
    assert recovered.git_dir_generation == token
    with pytest.raises(GitError, match="is absent"):
        inspect_owned_worktree(
            tmp_path / "absent",
            expected_commit=commit,
            expected_branch="topic",
            ownership_token=token,
        )
