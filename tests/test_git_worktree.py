from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import pytest
from git_helpers import git, initialize_repository

import fangorn.git_worktree as git_worktree_adapter
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
from fangorn.registry import ProcessIdentity, Registry, RegistryError
from fangorn.workspaces import Workspaces


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
    ("source", "normalized"),
    [
        ("https://[2001:db8::1]/repo.git", "https://[2001:db8::1]/repo.git"),
        ("ssh://[2001:db8::1]:2222/repo.git", "ssh://[2001:db8::1]:2222/repo.git"),
    ],
)
def test_repository_source_preserves_ipv6_authority(
    source: str, normalized: str
) -> None:
    assert normalize_repository_source(source).normalized == normalized


def test_local_non_bare_repository_may_end_in_dot_git(tmp_path: Path) -> None:
    source = tmp_path / "project.git"
    repository(source)

    assert normalize_repository_source(str(source)).path == source.resolve()


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
    assert read_configuration(source, commit, None) is None
    with pytest.raises(GitError, match="unknown-ref"):
        resolve_commit(source, "unknown-ref")

    symlink = tmp_path / "linked.toml"
    symlink.symlink_to(explicit)
    with pytest.raises(GitError, match="non-symlink"):
        read_configuration(source, commit, symlink)
    with pytest.raises(GitError, match="Configuration is unavailable"):
        read_configuration(source, commit, tmp_path / "missing.toml")


def test_explicit_configuration_is_read_from_validated_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    explicit = tmp_path / "explicit.toml"
    replacement = tmp_path / "replacement.toml"
    explicit.write_bytes(b"schema_version = 1\n")
    replacement.write_bytes(b"schema_version = 2\n")
    real_open = os.open
    swapped = False

    def swap_after_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int
    ) -> int:
        nonlocal swapped
        descriptor = real_open(path, flags)
        if path == explicit:
            swapped = True
            explicit.unlink()
            explicit.symlink_to(replacement)
        return descriptor

    monkeypatch.setattr(os, "open", swap_after_open)

    assert read_configuration(source, commit, explicit) == b"schema_version = 1\n"
    assert swapped


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


def test_interrupted_refresh_is_replayed_without_completion_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_repository = tmp_path / "source"
    repository(source_repository)
    cache = tmp_path / "cache" / "repository.git"
    source = normalize_repository_source(source_repository.as_uri())
    materialize_cache(source, cache, preparation_id="initial")
    from fangorn import git_worktree

    original = git_worktree._run_git_process
    fetches = 0

    class Interrupted(BaseException):
        pass

    def interrupt_after_fetch(*args: object, **kwargs: object) -> object:
        nonlocal fetches
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        if "fetch" in args:
            fetches += 1
            if fetches == 1:
                raise Interrupted
        return result

    monkeypatch.setattr(git_worktree, "_run_git_process", interrupt_after_fetch)
    with pytest.raises(Interrupted):
        materialize_cache(source, cache, preparation_id="retry")
    materialize_cache(source, cache, preparation_id="retry")

    assert fetches == 2


@pytest.mark.parametrize(
    ("finish_on_parent_exit", "hard_death", "expected"),
    [
        (False, False, "terminated"),
        (True, False, "completed"),
        (False, True, "terminated"),
        (True, True, "terminated"),
    ],
)
def test_interrupted_owner_waits_for_git_tree_before_releasing_lease(
    tmp_path: Path, finish_on_parent_exit: bool, hard_death: bool, expected: str
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    started = tmp_path / "started"
    stopped = tmp_path / "stopped"
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        "trap 'sleep 0.5; printf terminated > \"$FANGORN_STOPPED\"; exit 0' TERM\n"
        'printf started > "$FANGORN_STARTED"\n'
        'if [ "$FANGORN_DESCENDANT" = 1 ]; then '
        "(trap '' TERM; while :; do sleep 0.05; done) & fi\n"
        'if [ "$FANGORN_FINISH" = 1 ]; then sleep 0.5; '
        'printf completed > "$FANGORN_STOPPED"; exit 0; fi\n'
        "while :; do sleep 0.05; done\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    database = tmp_path / "state" / "registry.sqlite3"
    script = """
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from fangorn.git_worktree import _run_git_process
from fangorn.registry import Registry
from fangorn.workspaces import Workspaces
registry = Registry(Path(sys.argv[1]))
intent, _ = registry.begin_create_intent(request_key="interrupt", request_id=None,
    request_json="{}", target_path=str(Path(sys.argv[1]).parent / "target"),
    workspace_id="workspace", operation_id="operation", prepare_cache=True)
w = Workspaces(registry)
owner = w._invocation_process_identity()
epoch = registry.acquire_lease(scope_kind="repository", scope_key="source",
    operation_id=intent.operation_id, owner=owner, owner_status=w._owner_status)
print(json.dumps(asdict(owner)), flush=True)
try:
    _run_git_process(Path.cwd(), "fetch", liveness_fd=w._invocation_descriptor(owner),
        finish_on_parent_exit=sys.argv[2] == "finish")
finally:
    registry.release_lease(scope_kind="repository", scope_key="source",
        operation_id=intent.operation_id, lease_epoch=epoch)
    w._finish_invocation(owner)
"""
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    environment["FANGORN_STARTED"] = str(started)
    environment["FANGORN_STOPPED"] = str(stopped)
    environment["FANGORN_FINISH"] = "1" if finish_on_parent_exit else "0"
    environment["FANGORN_DESCENDANT"] = (
        "1" if not finish_on_parent_exit and not hard_death else "0"
    )
    child = subprocess.Popen(  # noqa: S603 -- fixed interpreter and test script
        [
            sys.executable,
            "-c",
            script,
            str(database),
            "finish" if finish_on_parent_exit else "cancel",
        ],
        stdout=subprocess.PIPE,
        text=True,
        env=environment,
    )
    assert child.stdout is not None
    owner = ProcessIdentity(**json.loads(child.stdout.readline()))
    deadline = time.monotonic() + 5
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started.exists()

    child.send_signal(signal.SIGKILL if hard_death else signal.SIGINT)
    registry = Registry(database)
    checker = Workspaces(registry)
    with pytest.raises(RegistryError, match="mutation is busy"):
        registry.acquire_lease(
            scope_kind="repository",
            scope_key="source",
            operation_id="operation",
            owner=ProcessIdentity("new", "boot", os.getpid(), "start"),
            owner_status=checker._owner_status,
        )
    child.wait(timeout=5)
    deadline = time.monotonic() + 5
    while not stopped.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stopped.read_text(encoding="utf-8") == expected
    deadline = time.monotonic() + 5
    while checker._owner_status(owner) != "dead" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert checker._owner_status(owner) == "dead"
    if hard_death:
        registry.acquire_lease(
            scope_kind="repository",
            scope_key="source",
            operation_id="operation",
            owner=ProcessIdentity("new", "boot", os.getpid(), "start"),
            owner_status=checker._owner_status,
        )


def test_process_group_probe_falls_back_when_proc_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda _path: False)

    assert git_worktree_adapter._process_group_running(os.getpgrp()) is True
    assert git_worktree_adapter._process_group_running(2**30) is False


def test_successful_git_drains_term_ignoring_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    group = tmp_path / "group"
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        'printf "%s" "$$" > "$FANGORN_GROUP"\n'
        "(trap '' TERM; while :; do sleep 0.05; done) >/dev/null 2>&1 &\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FANGORN_GROUP", str(group))
    liveness, writer = os.pipe()
    try:
        result = git_worktree_adapter._run_git_process(
            tmp_path, "fetch", liveness_fd=liveness
        )
    finally:
        os.close(liveness)
        os.close(writer)

    assert result.returncode == 0
    assert not git_worktree_adapter._process_group_running(
        int(group.read_text(encoding="ascii"))
    )


def test_git_cleanup_blocks_repeated_interrupts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    started = tmp_path / "started"
    stopped = tmp_path / "stopped"
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        "trap 'printf stopped > \"$FANGORN_STOPPED\"; exit 0' TERM\n"
        'printf started > "$FANGORN_STARTED"\n'
        "while :; do sleep 0.05; done\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    script = """
import os
from pathlib import Path
from fangorn.git_worktree import _run_git_process
read_fd, write_fd = os.pipe()
try:
    _run_git_process(Path.cwd(), "fetch", liveness_fd=read_fd)
except KeyboardInterrupt:
    print("interrupted", flush=True)
finally:
    os.close(read_fd)
    os.close(write_fd)
"""
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
    environment["FANGORN_STARTED"] = str(started)
    environment["FANGORN_STOPPED"] = str(stopped)
    child = subprocess.Popen(  # noqa: S603 -- fixed interpreter and test script
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
        env=environment,
    )
    deadline = time.monotonic() + 5
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started.exists()
    child.send_signal(signal.SIGINT)
    child.send_signal(signal.SIGINT)

    stdout, _ = child.communicate(timeout=5)

    assert child.returncode == 0
    assert stdout.strip() == "interrupted"
    assert stopped.read_text(encoding="utf-8") == "stopped"


def test_supervised_git_captures_output_without_reader_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\nhead -c 131072 /dev/zero\nhead -c 131072 /dev/zero >&2\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    liveness, writer = os.pipe()
    try:
        result = git_worktree_adapter._run_git_process(
            tmp_path, "fetch", liveness_fd=liveness, finish_on_parent_exit=True
        )
    finally:
        os.close(liveness)
        os.close(writer)

    assert result.returncode == 0
    assert len(result.stdout) == 131072
    assert len(result.stderr) == 131072


def test_worktree_create_rejects_replaced_target_parent_before_git_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    commit = repository(source)
    parent = tmp_path / "worktrees"
    parent.mkdir(mode=0o700)
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o700)
    original_parent = tmp_path / "original-parent"
    original = git_worktree_adapter._run_git_process
    swapped = False

    def swap_after_probe(*args: object, **kwargs: object) -> object:
        nonlocal swapped
        result = original(*args, **kwargs)  # type: ignore[arg-type]
        if "show-ref" in args and not swapped:
            swapped = True
            parent.rename(original_parent)
            parent.symlink_to(replacement, target_is_directory=True)
        return result

    monkeypatch.setattr(git_worktree_adapter, "_run_git_process", swap_after_probe)

    with pytest.raises(GitError, match="parent changed"):
        create_worktree(
            source,
            target=parent / "topic",
            branch="topic",
            commit=commit,
            ownership_token="a" * 64,
            reconcile=False,
        )

    assert not any(replacement.iterdir())


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


def test_clone_cache_fsyncs_publication_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_repository = tmp_path / "source"
    repository(source_repository)
    cache = tmp_path / "cache" / "repository.git"
    original = os.fsync
    synced_modes: list[int] = []

    def record_fsync(descriptor: int) -> None:
        synced_modes.append(os.fstat(descriptor).st_mode)
        original(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)
    materialize_cache(normalize_repository_source(source_repository.as_uri()), cache)

    assert any(stat.S_ISDIR(mode) for mode in synced_modes)


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


def test_worktree_creation_disables_repository_hooks(tmp_path: Path) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    invoked = tmp_path / "hook-invoked"
    hook = source / ".git" / "hooks" / "post-checkout"
    hook.parent.mkdir()
    hook.write_text(f"#!/bin/sh\nprintf invoked > {invoked}\n", encoding="utf-8")
    hook.chmod(0o755)

    create_worktree(
        source,
        target=tmp_path / "target",
        branch="topic",
        commit=commit,
        ownership_token="d" * 64,
        reconcile=False,
    )

    assert not invoked.exists()


def test_worktree_creation_rejects_executable_filters(tmp_path: Path) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    invoked = tmp_path / "filter-invoked"
    (source / ".gitattributes").write_text("*.payload filter=evil\n", encoding="utf-8")
    (source / "content.payload").write_text("content\n", encoding="utf-8")
    git(source, "add", ".gitattributes", "content.payload")
    git(source, "commit", "-m", "add filtered content")
    git(source, "config", "filter.evil.smudge", f"touch {invoked}")

    with pytest.raises(GitError, match="executable checkout configuration"):
        create_worktree(
            source,
            target=tmp_path / "target",
            branch="topic",
            commit=commit,
            ownership_token="e" * 64,
            reconcile=False,
        )

    assert not invoked.exists()
    assert not (tmp_path / "target").exists()


def test_worktree_adapter_rejects_markerless_matching_staging(tmp_path: Path) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    target = tmp_path / "target"
    token = "f" * 64
    staging = target.parent / f".fangorn-{token}"
    git(source, "worktree", "add", "-b", "topic", str(staging), commit)

    with pytest.raises(GitError, match="ownership receipt"):
        create_worktree(
            source,
            target=target,
            branch="topic",
            commit=commit,
            ownership_token=token,
            reconcile=True,
        )

    assert staging.exists()
    assert not target.exists()


def test_worktree_receipt_is_atomically_published_and_directory_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    target = tmp_path / "target"
    token = "1" * 64
    receipt = target.parent / f".fangorn-{token}.intent"
    original_write = os.write
    interrupted = False

    def interrupt_write(descriptor: int, value: bytes) -> int:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            original_write(descriptor, value[:3])
            raise OSError("interrupted receipt write")
        return original_write(descriptor, value)

    monkeypatch.setattr(os, "write", interrupt_write)
    with pytest.raises(GitError, match="receipt is unavailable"):
        create_worktree(
            source,
            target=target,
            branch="topic",
            commit=commit,
            ownership_token=token,
            reconcile=False,
        )
    assert not receipt.exists()
    assert not (target.parent / f".fangorn-{token}").exists()

    monkeypatch.setattr(os, "write", original_write)
    original_fsync = os.fsync
    synced_modes: list[int] = []

    def record_fsync(descriptor: int) -> None:
        synced_modes.append(os.fstat(descriptor).st_mode)
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)
    created = create_worktree(
        source,
        target=target,
        branch="topic",
        commit=commit,
        ownership_token=token,
        reconcile=True,
    )

    assert created.git_dir_generation == token
    assert any(stat.S_ISREG(mode) for mode in synced_modes)
    assert sum(stat.S_ISDIR(mode) for mode in synced_modes) >= 2


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
