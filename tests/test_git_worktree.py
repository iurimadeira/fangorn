from __future__ import annotations

import ctypes
import errno
import json
import os
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import pytest
from git_helpers import git, initialize_repository

import fangorn._git_anchor as git_anchor
import fangorn._git_guardian as git_guardian
import fangorn._git_supervisor as git_supervisor
import fangorn._permissions as permissions_adapter
import fangorn.git as git_adapter
import fangorn.git_worktree as git_worktree_adapter
import fangorn.workspaces as workspaces_adapter
from fangorn.git import (
    GitError,
    GitQuiescenceError,
    WorktreeObservation,
    establish_worktree_generation,
    observe_worktree,
    repository_generation,
)
from fangorn.git_worktree import (
    RepositorySource,
    create_worktree,
    inspect_owned_worktree,
    materialize_cache,
    normalize_repository_source,
    read_configuration,
    resolve_commit,
    validate_repository_for_object_reads,
)
from fangorn.registry import ProcessIdentity, Registry, RegistryError
from fangorn.workspaces import Workspaces


class _StubSelector:
    def __init__(self, readiness: Iterator[bool]) -> None:
        self._readiness = readiness
        self._keys: dict[int, selectors.SelectorKey] = {}

    def register(self, descriptor: int, events: int) -> selectors.SelectorKey:
        key = selectors.SelectorKey(descriptor, descriptor, events, None)
        self._keys[descriptor] = key
        return key

    def unregister(self, descriptor: int) -> selectors.SelectorKey:
        return self._keys.pop(descriptor)

    def select(
        self, timeout: float | None = None
    ) -> list[tuple[selectors.SelectorKey, int]]:
        del timeout
        if not next(self._readiness, False):
            return []
        key = next(iter(self._keys.values()))
        return [(key, selectors.EVENT_READ)]

    def close(self) -> None:
        self._keys.clear()

    def __enter__(self) -> _StubSelector:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()


def _stub_selector(*readiness: bool) -> Callable[[], _StubSelector]:
    states = iter(readiness)
    return lambda: _StubSelector(states)


def repository(path: Path) -> str:
    previous_umask = os.umask(0o077)
    try:
        initialize_repository(path)
        (path / "README.md").write_text("root\n", encoding="utf-8")
        git(path, "add", "README.md")
        git(path, "commit", "-m", "root")
        return git(path, "rev-parse", "HEAD")
    finally:
        os.umask(previous_umask)


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


@pytest.mark.parametrize(
    ("name", "value", "rejected"),
    [
        ("extensions.partialClone", "origin", True),
        ("remote.origin.partialCloneFilter", "blob:none", True),
        ("remote.origin.promisor", "true", True),
        ("remote.origin.promisor", "false", False),
        ("remote.origin.promisor", "invalid", True),
        ("core.sshCommand", "/bin/false", False),
    ],
)
def test_repository_object_reads_reject_promisor_semantics(
    tmp_path: Path, name: str, value: str, rejected: bool
) -> None:
    source = tmp_path / "repository"
    repository(source)
    git(source, "config", name, value)

    if rejected:
        with pytest.raises(GitError, match="promisor"):
            validate_repository_for_object_reads(source)
    else:
        validate_repository_for_object_reads(source)


def test_replace_refs_cannot_change_configuration_or_checkout(tmp_path: Path) -> None:
    source = tmp_path / "repository"
    original = repository(source)
    (source / "README.md").write_text("replacement\n", encoding="utf-8")
    (source / "fangorn.toml").write_text("schema_version = 2\n", encoding="utf-8")
    git(source, "add", "README.md", "fangorn.toml")
    tree = git(source, "write-tree")
    replacement = git(source, "commit-tree", tree, "-m", "replacement")
    git(source, "reset", "--hard", original)
    git(source, "replace", original, replacement)

    assert read_configuration(source, original, None) is None
    target = tmp_path / "target"
    created = create_worktree(
        source,
        target=target,
        branch="topic",
        commit=original,
        ownership_token="a" * 64,
        reconcile=False,
    )

    assert created.head == original
    assert (target / "README.md").read_text(encoding="utf-8") == "root\n"
    assert not (target / "fangorn.toml").exists()


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
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path in {explicit, explicit.name}:
            swapped = True
            explicit.unlink()
            explicit.symlink_to(replacement)
        return descriptor

    monkeypatch.setattr(os, "open", swap_after_open)

    assert read_configuration(source, commit, explicit) == b"schema_version = 1\n"
    assert swapped


def test_explicit_configuration_read_stays_bound_to_opened_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    controlled = tmp_path / "controlled"
    controlled.mkdir()
    parked = tmp_path / "parked"
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    explicit = controlled / "fangorn.toml"
    explicit.write_bytes(b"schema_version = 1\n# trusted\n")
    (redirected / explicit.name).write_bytes(b"schema_version = 1\n# secret\n")
    real_open = os.open
    swapped = False

    def swap_ancestor(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and path == explicit:
            controlled.rename(parked)
            controlled.symlink_to(redirected, target_is_directory=True)
            swapped = True
            return real_open(path, flags, mode, dir_fd=dir_fd)
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if not swapped and path == controlled.name:
            controlled.rename(parked)
            controlled.symlink_to(redirected, target_is_directory=True)
            swapped = True
        return descriptor

    monkeypatch.setattr(os, "open", swap_ancestor)

    assert read_configuration(source, commit, explicit) == (
        b"schema_version = 1\n# trusted\n"
    )
    assert swapped


def test_explicit_configuration_normalizes_ancestor_symlink_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    parent = tmp_path / "parent"
    parent.mkdir()
    explicit = parent / "fangorn.toml"
    explicit.write_bytes(b"schema_version = 1\n")
    real_open = os.open

    def fail_ancestor(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == parent.name:
            raise OSError(errno.ELOOP, "synthetic macOS symlink refusal")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", fail_ancestor)

    with pytest.raises(GitError, match="Configuration is unavailable"):
        read_configuration(source, commit, explicit)


def test_explicit_configuration_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    explicit = tmp_path / "fangorn.toml"
    os.mkfifo(explicit)
    script = """
import sys
from pathlib import Path
from fangorn.git import GitError
from fangorn.git_worktree import read_configuration

try:
    read_configuration(Path.cwd(), "HEAD", Path(sys.argv[1]))
except GitError as error:
    raise SystemExit(
        0 if str(error) == "Configuration must be a regular non-symlink file" else 2
    )
raise SystemExit(3)
"""

    result = subprocess.run(  # noqa: S603 -- isolated fixed interpreter invocation
        [sys.executable, "-I", "-c", script, str(explicit)],
        check=False,
        timeout=3,
    )

    assert result.returncode == 0


def test_explicit_configuration_parent_segments_stay_bound_to_opened_ancestors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    controlled = tmp_path / "controlled"
    controlled.mkdir()
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    explicit = tmp_path / "fangorn.toml"
    explicit.write_bytes(b"schema_version = 1\n# trusted\n")
    (redirected / explicit.name).write_bytes(b"schema_version = 1\n# secret\n")
    real_open = os.open
    moved = False

    def move_opened_ancestor(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal moved
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if not moved and path == controlled.name:
            controlled.rename(redirected / controlled.name)
            moved = True
        return descriptor

    monkeypatch.setattr(os, "open", move_opened_ancestor)

    assert (
        read_configuration(source, commit, controlled / ".." / explicit.name)
        == b"schema_version = 1\n# trusted\n"
    )
    assert moved


def test_explicit_configuration_supports_search_only_ancestors(tmp_path: Path) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    parent = tmp_path / "search-only"
    parent.mkdir()
    explicit = parent / "fangorn.toml"
    explicit.write_bytes(b"schema_version = 1\n")
    parent.chmod(stat.S_IXUSR)

    try:
        assert read_configuration(source, commit, explicit) == b"schema_version = 1\n"
    finally:
        parent.chmod(stat.S_IRWXU)


def test_configuration_directory_flags_use_darwin_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    assert git_worktree_adapter._configuration_directory_flags() == (
        0x40000000 | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    )


@pytest.mark.parametrize("explicit", [False, True])
def test_configuration_reads_are_bounded(tmp_path: Path, explicit: bool) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    configuration = tmp_path / "explicit.toml" if explicit else source / "fangorn.toml"
    configuration.write_bytes(b"#" * (1024 * 1024 + 1))
    if not explicit:
        git(source, "add", "fangorn.toml")
        git(source, "commit", "-m", "add oversized configuration")
        commit = git(source, "rev-parse", "HEAD")

    with pytest.raises(GitError, match="Configuration exceeds 1 MiB"):
        read_configuration(source, commit, configuration if explicit else None)


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
    invalid_cache.mkdir(mode=0o700)
    with pytest.raises(GitError, match="not a bare repository"):
        materialize_cache(source, invalid_cache)


def test_clone_cache_permissions_do_not_depend_on_follow_symlinks_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_repository = tmp_path / "source"
    repository(source_repository)
    cache = tmp_path / "cache" / "repository.git"
    real_chmod = os.chmod

    def chmod_without_follow_symlinks(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if not follow_symlinks:
            raise NotImplementedError
        real_chmod(path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "chmod", chmod_without_follow_symlinks)

    materialize_cache(normalize_repository_source(source_repository.as_uri()), cache)

    assert stat.S_IMODE(cache.stat().st_mode) == 0o700


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


def test_repository_refresh_disables_automatic_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def run(_path: Path, *arguments: str, **_kwargs: object) -> object:
        calls.append(arguments)
        return subprocess.CompletedProcess([], 0, b"", b"")

    monkeypatch.setattr(git_worktree_adapter, "_run_git_process", run)

    git_worktree_adapter._refresh_bare_repository(
        tmp_path, update_default=False, liveness_fd=None
    )

    assert calls == [
        (
            "fetch",
            "--no-auto-maintenance",
            "--prune",
            "--prune-tags",
            "origin",
            "+refs/heads/*:refs/remotes/origin/*",
            "+refs/tags/*:refs/tags/*",
        )
    ]


@pytest.mark.parametrize(
    ("finish_on_parent_exit", "hard_death", "expected"),
    [
        (False, False, "terminated"),
        (True, False, "completed"),
        (False, True, "terminated"),
        (True, True, "completed"),
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


def test_supervisor_process_group_probe_falls_back_without_proc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda _path: False)

    assert git_supervisor._process_group_running(os.getpgrp()) is True
    assert git_supervisor._process_group_running(2**30) is False


def test_supervisor_drains_git_when_owner_dies_before_status_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Stream:
        def __init__(self, descriptor: int) -> None:
            self._descriptor = descriptor

        def fileno(self) -> int:
            return self._descriptor

    class Child:
        pid = 123
        returncode = 0
        stdout = Stream(20)
        stderr = Stream(21)

        @staticmethod
        def poll() -> int:
            return 0

    child = Child()
    drained: list[object] = []
    child_options: dict[str, object] = {}

    def drain(value: object, _process_group: int) -> bool:
        drained.append(value)
        return True

    def start_child(*_args: object, **options: object) -> Child:
        child_options.update(options)
        return child

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "supervisor",
            "10",
            "11",
            "12",
            "13",
            "14",
            "123",
            "",
            "-1",
            "cancel",
            "3600",
            str(8 * 1024 * 1024),
            "git",
        ],
    )
    monkeypatch.setattr(os, "fstat", lambda _descriptor: os.stat_result((0,) * 10))
    monkeypatch.setattr(subprocess, "Popen", start_child)
    monkeypatch.setattr(
        os, "write", lambda *args: (_ for _ in ()).throw(BrokenPipeError())
    )
    monkeypatch.setattr(os, "close", lambda _descriptor: None)
    monkeypatch.setattr(git_supervisor, "_drain", drain)
    monkeypatch.setattr(git_supervisor, "_child_running", lambda _child: False)
    monkeypatch.setattr(git_supervisor, "_finish_captures", lambda *_args: "ok")
    monkeypatch.setattr(selectors, "DefaultSelector", _stub_selector())

    assert git_supervisor.main() == 0
    assert drained == [child]
    assert child_options["umask"] == 0o077


def test_supervisor_reports_git_startup_os_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipes = [os.pipe() for _ in range(5)]
    control, status, completion, liveness, anchor = pipes
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "supervisor",
            str(control[0]),
            str(status[1]),
            str(completion[1]),
            str(liveness[0]),
            str(anchor[1]),
            "123",
            "",
            "-1",
            "cancel",
            "3600",
            str(8 * 1024 * 1024),
            "git",
        ],
    )
    monkeypatch.setattr(
        git_supervisor,
        "_supervise",
        lambda *_args: (_ for _ in ()).throw(
            FileNotFoundError(2, "No such file or directory", "git")
        ),
    )
    try:
        assert git_supervisor.main() == 127
        assert os.read(status[0], 16) == b"!2\n"
        assert os.read(completion[0], 1) == b""
    finally:
        for read, write in pipes:
            for descriptor in (read, write):
                with suppress(OSError):
                    os.close(descriptor)


def test_supervisor_does_not_reap_before_group_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Child:
        pid = 123
        waited = False

        def wait(self) -> int:
            self.waited = True
            return 0

        @staticmethod
        def poll() -> int:
            raise AssertionError("cleanup must not reap through poll")

    child = Child()
    monkeypatch.setattr(
        git_supervisor, "_wait_for_group_state", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(os, "killpg", lambda *_args: None)

    assert git_supervisor._drain(child, 123) is True  # type: ignore[arg-type]

    assert child.waited is True


def test_supervisor_permission_denial_still_requires_group_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Child:
        returncode = 0

        @staticmethod
        def wait(timeout: float | None = None) -> int:
            del timeout
            return 0

    scans = iter((True, True))
    monkeypatch.setattr(
        os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(PermissionError()),
    )
    monkeypatch.setattr(
        git_supervisor,
        "_wait_for_group_state",
        lambda *_args, **_kwargs: next(scans),
    )

    assert git_supervisor._drain(Child(), 123) is False  # type: ignore[arg-type]


def test_supervisor_reaps_child_without_releasing_anchor_group() -> None:
    anchor = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], process_group=0
    )
    child = subprocess.Popen([sys.executable, "-c", "pass"], process_group=anchor.pid)
    try:
        deadline = time.monotonic() + 2
        while git_supervisor._child_running(child):
            assert time.monotonic() < deadline
            time.sleep(0.01)

        assert child.returncode == 0
        os.killpg(anchor.pid, 0)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)
        if anchor.poll() is None:
            os.killpg(anchor.pid, signal.SIGKILL)
        anchor.wait(timeout=2)


def test_supervised_git_rejects_missing_child_handshake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailedSupervisor:
        pid = 2**30
        returncode = 1

        @staticmethod
        def wait(timeout: float | None = None) -> int:
            del timeout
            return 1

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FailedSupervisor())
    monkeypatch.setattr(
        git_worktree_adapter,
        "_retain_quiescence_guardian",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        git_worktree_adapter, "_process_group_running", lambda *_args, **_kwargs: False
    )
    write = os.write
    monkeypatch.setattr(
        os,
        "write",
        lambda descriptor, value: 1 if value == b"a" else write(descriptor, value),
    )
    liveness, writer = os.pipe()
    try:
        with pytest.raises(GitError, match="failed before child startup"):
            git_worktree_adapter._run_git_process(
                tmp_path, "show-ref", liveness_fd=liveness
            )
    finally:
        os.close(liveness)
        os.close(writer)


@pytest.mark.parametrize(
    "value",
    [b"", b"0\n", b" 1\n", b"1", b"1\nextra", b"2147483648\n"],
)
def test_supervisor_pid_rejects_noncanonical_frames(value: bytes) -> None:
    assert git_worktree_adapter._supervisor_pid(value) is None


def test_supervisor_pid_accepts_canonical_positive_pid() -> None:
    assert git_worktree_adapter._supervisor_pid(b"123\n") == 123


def test_supervisor_pid_reader_completes_short_pipe_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = iter((b"12", b"3\n", b""))
    monkeypatch.setattr(os, "read", lambda *args: next(chunks))
    monkeypatch.setattr(selectors, "DefaultSelector", _stub_selector())

    assert git_worktree_adapter._read_supervisor_pid(10) == 123


def test_supervisor_pid_reader_completes_when_frame_writer_closes() -> None:
    read, write = os.pipe()
    try:
        os.write(write, b"123\n")
        os.close(write)
        write = -1

        assert git_worktree_adapter._read_supervisor_pid(read) == 123
    finally:
        os.close(read)
        if write >= 0:
            os.close(write)


def test_supervisor_pid_reader_completes_with_frame_writer_open() -> None:
    read, write = os.pipe()
    try:
        os.write(write, b"123\n")

        assert (
            git_worktree_adapter._read_supervisor_pid(
                read, deadline=time.monotonic() + 1
            )
            == 123
        )
    finally:
        os.close(read)
        os.close(write)


def test_supervisor_pid_reader_rejects_eof_before_newline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = iter((b"123", b""))
    monkeypatch.setattr(os, "read", lambda *args: next(chunks))
    monkeypatch.setattr(selectors, "DefaultSelector", _stub_selector(True))

    assert git_worktree_adapter._read_supervisor_pid(10) is None


def test_supervisor_pid_reader_rejects_split_trailing_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = iter((b"123\n", b"e"))
    monkeypatch.setattr(os, "read", lambda *args: next(chunks))
    monkeypatch.setattr(selectors, "DefaultSelector", _stub_selector(True))

    assert git_worktree_adapter._read_supervisor_pid(10) is None


def test_supervisor_completion_reader_requires_one_complete_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = iter((b"-1", b"5\n", b""))
    monkeypatch.setattr(os, "read", lambda *args: next(chunks))
    monkeypatch.setattr(
        selectors,
        "DefaultSelector",
        _stub_selector(False, False, True),
    )
    assert git_worktree_adapter._read_supervisor_completion(10) == -15

    chunks = iter((b"256\n", b""))
    assert git_worktree_adapter._read_supervisor_completion(10) == 256

    chunks = iter((b"0\n", b"extra", b""))
    assert git_worktree_adapter._read_supervisor_completion(10) is None


def test_quiescence_errors_are_not_retried_or_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    def fail(*_args: object, **_kwargs: object) -> str:
        nonlocal attempts
        attempts += 1
        raise GitQuiescenceError("unproven")

    monkeypatch.setattr(git_adapter, "_require_supported_git", fail)
    with pytest.raises(GitQuiescenceError, match="unproven"):
        git_adapter.observe_worktree(tmp_path)
    assert attempts == 1

    attempts = 0
    monkeypatch.setattr(git_worktree_adapter, "_run_git", fail)
    with pytest.raises(GitQuiescenceError, match="unproven"):
        git_worktree_adapter.resolve_commit(tmp_path, "topic", remote=True)
    assert attempts == 1

    attempts = 0
    with pytest.raises(GitQuiescenceError, match="unproven"):
        git_worktree_adapter._verify_bare_repository(tmp_path, "source")
    assert attempts == 1


def test_parent_kills_a_supervisor_that_will_not_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HungSupervisor:
        pid = 123
        waits = 0

        def wait(self, *, timeout: float) -> int:
            self.waits += 1
            if self.waits < 3:
                raise subprocess.TimeoutExpired(["supervisor"], timeout)
            return -signal.SIGKILL

    signals: list[int] = []
    monkeypatch.setattr(os, "killpg", lambda _pid, sent: signals.append(sent))

    git_worktree_adapter._settle_process(HungSupervisor())  # type: ignore[arg-type]

    assert signals == [signal.SIGTERM, signal.SIGKILL]


def test_parent_preserves_unproven_supervisor_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HungSupervisor:
        pid = 123

        @staticmethod
        def wait(*, timeout: float) -> int:
            raise subprocess.TimeoutExpired(["supervisor"], timeout)

    monkeypatch.setattr(os, "killpg", lambda *_args: None)

    with pytest.raises(GitQuiescenceError, match="supervisor termination"):
        git_worktree_adapter._settle_process(HungSupervisor())  # type: ignore[arg-type]


def test_portable_group_probe_ignores_zombie_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda _path: False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "123 77 Z\n124 77 S\n", ""
        ),
    )
    monkeypatch.setattr(os, "getpid", lambda: 999)

    assert git_supervisor._process_group_running(77) is True

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "123 77 Z\n", ""
        ),
    )
    assert git_supervisor._process_group_running(77) is False

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "truncated\n", ""
        ),
    )
    with pytest.raises(OSError, match="parse process-group"):
        git_supervisor._process_group_running(77)


def test_supervisor_group_probe_bounds_ps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(Path, "is_dir", lambda _path: False)

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(["ps"], 0, "123 77 S\n", "")

    monkeypatch.setattr(
        subprocess,
        "run",
        run,
    )

    assert git_supervisor._process_group_running(77, timeout=0.25) is True
    observed_timeout = captured["timeout"]
    assert isinstance(observed_timeout, (int, float))
    assert 0 < observed_timeout <= 0.25


def test_process_group_proc_probes_stop_at_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda _path: True)
    monkeypatch.setattr(Path, "iterdir", lambda _path: iter((Path("/proc/123"),)))

    for module in (git_supervisor, git_worktree_adapter):
        clock = iter((0.0, 0.25))
        monkeypatch.setattr(module.time, "monotonic", lambda clock=clock: next(clock))
        with pytest.raises(subprocess.TimeoutExpired):
            module._process_group_running(77, timeout=0.25)


def test_supervisor_group_probe_ignores_watchdog_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda _path: False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "77 77 S\n", ""
        ),
    )

    assert git_supervisor._process_group_running(77, ignore_pid=77) is False


def test_group_cleanup_retries_failed_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations: Iterator[bool | subprocess.SubprocessError] = iter(
        (subprocess.SubprocessError(), False)
    )

    def observe(_process_group: int, **_kwargs: float) -> bool:
        result = next(observations)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(git_supervisor, "_process_group_running", observe)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    assert git_supervisor._wait_for_group_state(77) is False


def test_supervisor_group_cleanup_forces_kill_after_probe_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Child:
        def __init__(self) -> None:
            self.waits: list[float] = []

        def wait(self, timeout: float) -> int:
            self.waits.append(timeout)
            return -signal.SIGKILL

    states = iter((True, True))
    signals: list[int] = []
    monkeypatch.setattr(
        git_supervisor,
        "_wait_for_group_state",
        lambda *_args, **_kwargs: next(states),
    )
    monkeypatch.setattr(os, "killpg", lambda _pid, sent: signals.append(sent))

    child = Child()
    assert git_supervisor._drain(child, 77) is False  # type: ignore[arg-type]

    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert len(child.waits) == 1


def test_supervisor_rejects_completion_without_proven_group_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostics: list[str] = []
    monkeypatch.setattr(git_supervisor, "_replace_output", diagnostics.append)
    monkeypatch.setattr(
        git_supervisor,
        "_finish_captures",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must close immediately")),
    )

    assert (
        git_supervisor._completion_failure(False, {}, 1, time.monotonic())
        == git_supervisor.UNPROVEN_GROUP_TERMINATION
    )
    assert diagnostics == ["Git process-group termination could not be confirmed"]


@pytest.mark.parametrize(
    "message",
    ["Git operation exceeded one hour", "Git diagnostic output exceeded 8 MiB"],
)
def test_supervisor_limits_preserve_unproven_group_result(
    message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    diagnostics: list[str] = []
    monkeypatch.setattr(git_supervisor, "_replace_output", diagnostics.append)

    assert (
        git_supervisor._limit_result(False, message)
        == git_supervisor.UNPROVEN_GROUP_TERMINATION
    )
    assert diagnostics == ["Git process-group termination could not be confirmed"]


def test_supervisor_capture_finish_stops_at_deadline() -> None:
    reader, writer = os.pipe()
    captures = {reader: [1, 0]}
    try:
        assert (
            git_supervisor._finish_captures(captures, 1, time.monotonic()) == "timeout"
        )
        assert captures == {}
    finally:
        os.close(writer)


@pytest.mark.parametrize(
    ("payload", "limit", "expected"),
    [(b"ok", 2, "ok"), (b"toolong", 2, "exceeded")],
)
def test_supervisor_capture_finish_drains_available_output(
    payload: bytes, limit: int, expected: str
) -> None:
    reader, writer = os.pipe()
    os.write(writer, payload)
    os.close(writer)
    with tempfile.TemporaryFile() as destination:
        captures = {reader: [destination.fileno(), 0]}
        assert (
            git_supervisor._finish_captures(captures, limit, time.monotonic() + 1)
            == expected
        )
        assert captures == {}


def test_supervisor_finish_waits_for_child_before_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = object()
    running = iter((True, False))
    drained: list[object] = []
    monkeypatch.setattr(git_supervisor, "_child_running", lambda _child: next(running))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    def drain(value: object, _group: int) -> bool:
        drained.append(value)
        return True

    monkeypatch.setattr(git_supervisor, "_drain", drain)

    assert git_supervisor._finish(child, 77) is True  # type: ignore[arg-type]
    assert drained == [child]


def test_supervisor_drain_returns_after_first_absent_group_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Child:
        waited = False

        def wait(self) -> int:
            self.waited = True
            return 0

    child = Child()
    monkeypatch.setattr(os, "killpg", lambda *_args: None)
    monkeypatch.setattr(
        git_supervisor, "_wait_for_group_state", lambda *_args, **_kwargs: False
    )

    assert git_supervisor._drain(child, 77) is True  # type: ignore[arg-type]
    assert child.waited is True


def test_anchor_survives_group_termination_before_arming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_reader, control_writer = os.pipe()
    liveness_reader, liveness_writer = os.pipe()
    events: list[tuple[object, object]] = []
    os.write(control_writer, b"x")
    monkeypatch.setattr(
        sys,
        "argv",
        ["anchor", str(control_reader), str(liveness_reader), "1"],
    )
    monkeypatch.setattr(
        signal, "signal", lambda number, handler: events.append((number, handler))
    )
    monkeypatch.setattr(
        signal,
        "pthread_sigmask",
        lambda operation, signals: events.append((operation, signals)),
    )
    try:
        assert git_anchor.main() == 1
    finally:
        for descriptor in (
            control_reader,
            control_writer,
            liveness_reader,
            liveness_writer,
        ):
            os.close(descriptor)

    assert events == [
        (signal.SIGTERM, signal.SIG_IGN),
        (signal.SIG_UNBLOCK, {signal.SIGTERM}),
    ]


def test_anchor_inherits_blocked_term_until_handler_is_ready() -> None:
    control_reader, control_writer = os.pipe()
    liveness_reader, liveness_writer = os.pipe()
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    anchor: subprocess.Popen[bytes] | None = None
    try:
        anchor = subprocess.Popen(  # noqa: S603 -- fixed watchdog argv
            [
                sys.executable,
                "-I",
                str(Path(git_anchor.__file__)),
                str(control_reader),
                str(liveness_reader),
                "1",
            ],
            pass_fds=(control_reader, liveness_reader),
            process_group=0,
        )
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    try:
        os.killpg(anchor.pid, signal.SIGTERM)
        os.write(control_writer, b"a")
        time.sleep(0.05)
        assert anchor.poll() is None
    finally:
        os.close(control_writer)
        for descriptor in (control_reader, liveness_reader, liveness_writer):
            os.close(descriptor)
        with suppress(subprocess.TimeoutExpired):
            anchor.wait(timeout=2)


def test_anchor_main_terminates_group_when_control_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_read, control_write = os.pipe()
    liveness_read, liveness_write = os.pipe()
    signals: list[tuple[int, int]] = []
    os.write(control_write, b"a")
    os.close(control_write)
    monkeypatch.setattr(
        sys, "argv", ["anchor", str(control_read), str(liveness_read), "1"]
    )
    monkeypatch.setattr(signal, "signal", lambda *_args: None)
    monkeypatch.setattr(signal, "pthread_sigmask", lambda *_args: None)
    monkeypatch.setattr(os, "killpg", lambda group, sent: signals.append((group, sent)))
    try:
        assert git_anchor.main() == 1
    finally:
        os.close(control_read)
        os.close(liveness_read)
        os.close(liveness_write)

    assert signals == [(os.getpgrp(), signal.SIGKILL)]


def test_guardian_main_retries_probe_errors_with_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    liveness_read, liveness_write = os.pipe()
    ready_read, ready_write = os.pipe()
    states = iter(
        (
            OSError("probe failed"),
            None,
            None,
            None,
            None,
            None,
            None,
            ProcessLookupError(),
        )
    )
    sleeps: list[float] = []
    scans = 0

    def probe(_pid: int, _signal: int) -> None:
        state = next(states)
        if isinstance(state, BaseException):
            raise state

    def scan(_group: int) -> bool:
        nonlocal scans
        scans += 1
        return scans < 6

    monkeypatch.setattr(
        sys,
        "argv",
        ["guardian", "123", str(liveness_read), str(ready_write)],
    )
    monkeypatch.setattr(signal, "signal", lambda *_args: None)
    monkeypatch.setattr(signal, "pthread_sigmask", lambda *_args: None)
    monkeypatch.setattr(os, "kill", probe)
    monkeypatch.setattr(git_guardian, "_process_group_running", scan)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    try:
        assert git_guardian.main() == 0
        assert os.read(ready_read, 2) == b"r\n"
    finally:
        for descriptor in (
            liveness_read,
            liveness_write,
            ready_read,
            ready_write,
        ):
            with suppress(OSError):
                os.close(descriptor)

    assert sleeps == [0.01, 0.02, 0.04, 0.08, 0.16, 0.25, 0.25]
    assert max(sleeps) == 0.25
    assert scans == 6


def test_guardian_scans_group_only_after_two_cheap_leader_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    liveness_read, liveness_write = os.pipe()
    ready_read, ready_write = os.pipe()
    events: list[str] = []
    probes = iter((PermissionError(), ProcessLookupError()))
    scans = iter((True, False))

    def probe(_pid: int, sent: int) -> None:
        assert sent == 0
        events.append("probe")
        raise next(probes)

    def scan(_process_group: int) -> bool:
        events.append("scan")
        return next(scans)

    monkeypatch.setattr(
        sys,
        "argv",
        ["guardian", "123", str(liveness_read), str(ready_write)],
    )
    monkeypatch.setattr(signal, "signal", lambda *_args: None)
    monkeypatch.setattr(signal, "pthread_sigmask", lambda *_args: None)
    monkeypatch.setattr(os, "kill", probe)
    monkeypatch.setattr(git_guardian, "_process_group_running", scan)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    try:
        assert git_guardian.main() == 0
        assert os.read(ready_read, 2) == b"r\n"
    finally:
        for descriptor in (
            liveness_read,
            liveness_write,
            ready_read,
            ready_write,
        ):
            with suppress(OSError):
                os.close(descriptor)

    assert events == ["probe", "probe", "scan", "scan"]


def test_guardian_scans_zombie_like_leader_after_initial_cheap_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    liveness_read, liveness_write = os.pipe()
    ready_read, ready_write = os.pipe()
    events: list[str] = []

    def probe(_pid: int, sent: int) -> None:
        assert sent == 0
        events.append("probe")

    def scan(_process_group: int) -> bool:
        events.append("scan")
        return False

    def bounded_sleep(_seconds: float) -> None:
        if events.count("probe") >= 3:
            raise AssertionError("guardian did not scan the zombie-like leader")

    monkeypatch.setattr(
        sys,
        "argv",
        ["guardian", "123", str(liveness_read), str(ready_write)],
    )
    monkeypatch.setattr(signal, "signal", lambda *_args: None)
    monkeypatch.setattr(signal, "pthread_sigmask", lambda *_args: None)
    monkeypatch.setattr(os, "kill", probe)
    monkeypatch.setattr(git_guardian, "_process_group_running", scan)
    monkeypatch.setattr(time, "sleep", bounded_sleep)
    try:
        assert git_guardian.main() == 0
        assert os.read(ready_read, 2) == b"r\n"
    finally:
        for descriptor in (
            liveness_read,
            liveness_write,
            ready_read,
            ready_write,
        ):
            with suppress(OSError):
                os.close(descriptor)

    assert events == ["probe", "probe", "scan"]


def test_guardian_persists_unknown_quiescence_after_probe_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "invocation"
    marker.touch()
    liveness = os.open(marker, os.O_RDWR)
    ready_read, ready_write = os.pipe()
    probes = 0

    def unavailable(_pid: int, _signal: int) -> None:
        nonlocal probes
        probes += 1
        raise OSError("probe unavailable")

    monkeypatch.setattr(
        sys,
        "argv",
        ["guardian", "123", str(liveness), str(ready_write)],
    )
    monkeypatch.setattr(signal, "signal", lambda *_args: None)
    monkeypatch.setattr(signal, "pthread_sigmask", lambda *_args: None)
    monkeypatch.setattr(os, "kill", unavailable)
    monkeypatch.setattr(
        git_guardian,
        "_process_group_running",
        lambda _group: (_ for _ in ()).throw(AssertionError("must not scan")),
    )
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    try:
        assert git_guardian.main() == 1
        assert os.read(ready_read, 2) == b"r\n"
    finally:
        for descriptor in (liveness, ready_read, ready_write):
            with suppress(OSError):
                os.close(descriptor)

    assert probes == git_guardian.PROBE_FAILURE_LIMIT
    assert marker.read_bytes() == git_guardian.QUIESCENCE_UNKNOWN


def test_guardian_executable_releases_after_group_stops() -> None:
    target = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], process_group=0
    )
    liveness_read, liveness_write = os.pipe()
    ready_read, ready_write = os.pipe()
    guardian = subprocess.Popen(  # noqa: S603 -- fixed helper argv
        [
            sys.executable,
            "-I",
            str(Path(git_guardian.__file__)),
            str(target.pid),
            str(liveness_read),
            str(ready_write),
        ],
        pass_fds=(liveness_read, ready_write),
        process_group=0,
    )
    os.close(ready_write)
    try:
        assert os.read(ready_read, 2) == b"r\n"
        os.killpg(target.pid, signal.SIGKILL)
        target.wait(timeout=2)
        assert guardian.wait(timeout=2) == 0
    finally:
        for descriptor in (liveness_read, liveness_write, ready_read):
            with suppress(OSError):
                os.close(descriptor)
        for process in (target, guardian):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)


def test_supervisor_executable_reports_completed_effect() -> None:
    anchor = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], process_group=0
    )
    control_read, control_write = os.pipe()
    status_read, status_write = os.pipe()
    completion_read, completion_write = os.pipe()
    liveness_read, liveness_write = os.pipe()
    anchor_read, anchor_write = os.pipe()
    supervisor = subprocess.Popen(  # noqa: S603 -- fixed helper argv
        [
            sys.executable,
            "-I",
            str(Path(git_supervisor.__file__)),
            str(control_read),
            str(status_write),
            str(completion_write),
            str(liveness_read),
            str(anchor_write),
            str(anchor.pid),
            "",
            "-1",
            "cancel",
            "10",
            str(8 * 1024 * 1024),
            sys.executable,
            "-c",
            "pass",
        ],
        pass_fds=(
            control_read,
            status_write,
            completion_write,
            liveness_read,
            anchor_write,
        ),
        process_group=0,
    )
    for descriptor in (control_read, status_write, completion_write):
        os.close(descriptor)
    try:
        assert git_worktree_adapter._read_supervisor_pid(
            status_read, deadline=time.monotonic() + 2
        )
        assert (
            git_worktree_adapter._read_supervisor_completion(
                completion_read, deadline=time.monotonic() + 5
            )
            == 0
        )
        assert supervisor.wait(timeout=2) == 0
        anchor.wait(timeout=2)
    finally:
        for descriptor in (
            control_write,
            status_read,
            completion_read,
            liveness_read,
            liveness_write,
            anchor_read,
            anchor_write,
        ):
            with suppress(OSError):
                os.close(descriptor)
        for process in (anchor, supervisor):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
        if anchor.poll() is None:
            os.killpg(anchor.pid, signal.SIGKILL)
            anchor.wait(timeout=2)


def test_parent_rejects_completion_without_final_group_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        git_worktree_adapter,
        "_read_supervisor_completion",
        lambda *_args, **_kwargs: git_worktree_adapter.UNPROVEN_GROUP_TERMINATION,
    )
    monkeypatch.setattr(
        git_worktree_adapter,
        "_wait_for_process_group_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            GitError("observation unavailable")
        ),
    )

    with pytest.raises(GitQuiescenceError, match="Cannot confirm"):
        git_worktree_adapter._run_git_process(tmp_path, "--version")


def test_parent_does_not_start_git_without_ready_guardian(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Anchor:
        pid = 123

    commands: list[list[str]] = []
    write = os.write

    def start(command: list[str], **_kwargs: object) -> Anchor:
        commands.append(command)
        return Anchor()

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(
        os,
        "write",
        lambda descriptor, data: 1 if data == b"a" else write(descriptor, data),
    )
    monkeypatch.setattr(
        git_worktree_adapter,
        "_retain_quiescence_guardian",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("guardian failed")),
    )
    monkeypatch.setattr(git_worktree_adapter, "_cancel_process_group", lambda *_: None)
    monkeypatch.setattr(git_worktree_adapter, "_settle_process", lambda *_: None)

    with pytest.raises(GitError, match="guardian failed"):
        git_worktree_adapter._run_git_process(tmp_path, "--version")

    assert len(commands) == 1
    assert commands[0][2].endswith("_git_anchor.py")


def test_internal_git_helpers_do_not_inherit_coverage_instrumentation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environments: list[dict[str, str]] = []
    real_popen = subprocess.Popen

    def start(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        command = args[0]
        if isinstance(command, list) and any(
            str(value).endswith(
                ("_git_anchor.py", "_git_guardian.py", "_git_supervisor.py")
            )
            for value in command
        ):
            environments.append(kwargs["env"])
        return real_popen(*args, **kwargs)

    monkeypatch.setenv("COVERAGE_PROCESS_CONFIG", "instrument")
    monkeypatch.setenv("COVERAGE_PROCESS_START", "instrument")
    monkeypatch.setattr(subprocess, "Popen", start)
    result = git_worktree_adapter._run_git_process(tmp_path, "--version")

    assert result.returncode == 0
    assert len(environments) == 3
    assert all(
        not {"COVERAGE_PROCESS_CONFIG", "COVERAGE_PROCESS_START"} & value.keys()
        for value in environments
    )


def test_worktree_observation_batches_identity_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "repository"
    repository(source)
    calls: list[tuple[str, ...]] = []
    real_run_git = git_adapter._run_git

    def run_git(path: Path, *arguments: str, **kwargs: Any) -> str | None:
        calls.append(arguments)
        return real_run_git(path, *arguments, **kwargs)

    monkeypatch.setattr(git_adapter, "_run_git", run_git)

    assert observe_worktree(source).head is not None
    assert len(calls) == 7
    assert sum("--show-toplevel" in call for call in calls) == 2


def test_parent_group_probe_rejects_malformed_ps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda _path: False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "truncated\n", ""
        ),
    )

    with pytest.raises(OSError, match="parse process-group"):
        git_worktree_adapter._process_group_running(77)


def test_parent_group_probe_bounds_portable_ps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(Path, "is_dir", lambda _path: False)

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(["ps"], 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)

    assert git_worktree_adapter._process_group_running(77, timeout=0.25) is False
    observed_timeout = captured["timeout"]
    assert isinstance(observed_timeout, (int, float))
    assert 0 < observed_timeout <= 0.25


def test_parent_group_cleanup_stops_at_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[int] = []
    monkeypatch.setattr(os, "killpg", lambda _pid, sent: signals.append(sent))

    with pytest.raises(GitError, match="Cannot confirm Git process-group termination"):
        git_worktree_adapter._cancel_process_group(77, deadline=time.monotonic())

    assert signals == [signal.SIGTERM, signal.SIGKILL]


def test_parent_group_cleanup_permission_denial_stays_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes = 0

    def scan(*_args: object, **_kwargs: object) -> bool:
        nonlocal probes
        probes += 1
        raise GitQuiescenceError("still unproven")

    monkeypatch.setattr(
        os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(PermissionError()),
    )
    monkeypatch.setattr(git_worktree_adapter, "_wait_for_process_group_state", scan)

    with pytest.raises(GitQuiescenceError, match="still unproven"):
        git_worktree_adapter._cancel_process_group(77)

    assert probes == 2


def test_parent_group_cleanup_waits_through_running_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = iter((True, True, False))
    monkeypatch.setattr(
        git_worktree_adapter,
        "_process_group_running",
        lambda *_args, **_kwargs: next(observations),
    )
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    assert (
        git_worktree_adapter._wait_for_process_group_state(
            77, deadline=time.monotonic() + 1
        )
        is False
    )


def test_supervised_git_rejects_ignored_sigchld_before_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invoked = False

    def popen(*args: object, **kwargs: object) -> object:
        nonlocal invoked
        invoked = True
        raise AssertionError("supervisor must not start")

    monkeypatch.setattr(signal, "getsignal", lambda _signal: signal.SIG_IGN)
    monkeypatch.setattr(subprocess, "Popen", popen)
    liveness, writer = os.pipe()
    try:
        with pytest.raises(GitError, match="SIGCHLD"):
            git_worktree_adapter._run_git_process(
                tmp_path, "show-ref", liveness_fd=liveness
            )
    finally:
        os.close(liveness)
        os.close(writer)

    assert invoked is False


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


def test_git_cleanup_owns_supervisor_before_pending_interrupt(
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
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FANGORN_STARTED", str(started))
    monkeypatch.setenv("FANGORN_STOPPED", str(stopped))
    real_popen = subprocess.Popen

    def interrupt_after_spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        process = real_popen(*args, **kwargs)
        command = args[0]
        if isinstance(command, list) and any(
            str(value).endswith("_git_supervisor.py") for value in command
        ):
            os.kill(os.getpid(), signal.SIGINT)
        return process

    monkeypatch.setattr(subprocess, "Popen", interrupt_after_spawn)
    liveness, writer = os.pipe()
    try:
        with pytest.raises(KeyboardInterrupt):
            git_worktree_adapter._run_git_process(
                tmp_path, "fetch", liveness_fd=liveness
            )
    finally:
        os.close(liveness)
        os.close(writer)

    assert not started.exists() or stopped.read_text(encoding="utf-8") == "stopped"


def test_supervised_git_cleans_group_when_supervisor_dies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    process_group = tmp_path / "process-group"
    fake_git = fake_bin / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        'ps -o pgid= -p "$$" | tr -d " " > "$FANGORN_GROUP"\n'
        "(trap '' TERM; while :; do sleep 0.05; done) >/dev/null 2>&1 &\n"
        "sleep 0.05\n"
        'kill -KILL "$PPID"\n'
        "while :; do sleep 0.05; done\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FANGORN_GROUP", str(process_group))
    liveness, writer = os.pipe()
    try:
        with pytest.raises(GitError, match="failed before completion"):
            git_worktree_adapter._run_git_process(
                tmp_path, "fetch", liveness_fd=liveness
            )
    finally:
        os.close(liveness)
        os.close(writer)

    assert not git_worktree_adapter._process_group_running(
        int(process_group.read_text(encoding="ascii"))
    )


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


@pytest.mark.parametrize(
    ("timeout", "output_limit", "body", "message"),
    [
        (0, 8 * 1024 * 1024, "while :; do sleep 1; done\n", "exceeded one hour"),
        (3600, 1024, "head -c 4096 /dev/zero\n", "exceeded 8 MiB"),
    ],
)
def test_supervised_git_bounds_time_and_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeout: int,
    output_limit: int,
    body: str,
    message: str,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(git_worktree_adapter, "GIT_EFFECT_TIMEOUT_SECONDS", timeout)
    monkeypatch.setattr(git_worktree_adapter, "GIT_CAPTURE_LIMIT", output_limit)
    liveness, writer = os.pipe()
    try:
        result = git_worktree_adapter._run_git_process(
            tmp_path, "fetch", liveness_fd=liveness
        )
    finally:
        os.close(liveness)
        os.close(writer)

    assert result.returncode == 124
    assert message in result.stderr.decode("utf-8")
    assert len(result.stdout) <= output_limit


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
    original = git_worktree_adapter._require_target_parent
    checks = 0

    def swap_after_final_check(guard: object) -> None:
        nonlocal checks
        original(guard)  # type: ignore[arg-type]
        checks += 1
        if checks == 2:
            parent.rename(original_parent)
            parent.symlink_to(replacement, target_is_directory=True)

    monkeypatch.setattr(
        git_worktree_adapter, "_require_target_parent", swap_after_final_check
    )

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
    assert any(original_parent.iterdir())


def test_target_parent_walk_rejects_relative_and_symlink_paths(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="must be absolute"):
        git_worktree_adapter._walk_target_parent(Path("relative"), create=False)

    linked = tmp_path / "linked"
    linked.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(GitError, match="unsafe"):
        git_worktree_adapter._walk_target_parent(linked, create=False)


def test_target_parent_walk_rechecks_mkdir_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "raced"
    original = os.mkdir

    def raced_mkdir(
        path: str | bytes,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        original(path, mode, dir_fd=dir_fd)
        raise FileExistsError

    monkeypatch.setattr(os, "mkdir", raced_mkdir)
    descriptor = git_worktree_adapter._walk_target_parent(target, create=True)
    assert descriptor is not None
    os.close(descriptor)


def test_target_parent_helpers_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = tmp_path.stat()
    monkeypatch.setattr(os, "geteuid", lambda: metadata.st_uid + 1)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(GitError, match="unsafe"):
            git_worktree_adapter._require_safe_directory(metadata, descriptor)
    finally:
        os.close(descriptor)

    monkeypatch.undo()
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    guarded = git_worktree_adapter._prepare_target_parent(parent)
    parent.rename(tmp_path / "moved")
    try:
        with pytest.raises(GitError, match="changed"):
            git_worktree_adapter._require_target_parent(guarded)
    finally:
        os.close(guarded.descriptor)

    descriptor = os.open(tmp_path, os.O_RDONLY)
    monkeypatch.setattr(
        os, "fsync", lambda _descriptor: (_ for _ in ()).throw(OSError())
    )
    try:
        with pytest.raises(GitError, match="not durable"):
            git_worktree_adapter._fsync_descriptor(descriptor, "Target")
    finally:
        os.close(descriptor)


def test_cache_parent_guard_rejects_replacement(tmp_path: Path) -> None:
    namespace = tmp_path / "cache"
    parent = namespace / "repositories"
    guard = git_worktree_adapter._prepare_cache_parent(namespace, parent)
    moved = tmp_path / "moved-cache"
    parent.rename(moved)
    parent.mkdir(mode=0o700)
    try:
        with pytest.raises(GitError, match="namespace changed"):
            git_worktree_adapter._require_cache_parent(guard)
    finally:
        os.close(guard.descriptor)


def test_cache_parent_guard_rejects_lexical_escape(tmp_path: Path) -> None:
    namespace = tmp_path / "cache"

    with pytest.raises(GitError, match="namespace is unsafe"):
        git_worktree_adapter._prepare_cache_parent(
            namespace, namespace / ".." / "outside"
        )


def test_cache_staging_cleanup_failure_is_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_repository = tmp_path / "source"
    repository(source_repository)
    source = normalize_repository_source(source_repository.as_uri())

    def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        raise OSError("cleanup failed")

    monkeypatch.setattr("fangorn.git_worktree.shutil.rmtree", fail_cleanup)

    with pytest.raises(GitError, match="Failed to clean clone staging"):
        materialize_cache(source, tmp_path / "cache" / "repository.git")


def test_cache_staging_survives_unproven_git_quiescence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache" / "repository.git"
    owner = ProcessIdentity("owner", "boot", 1001, "start")
    source = RepositorySource("url", None, "https://example.invalid/repo.git", "source")
    monkeypatch.setattr(
        git_worktree_adapter, "require_supported_git", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        git_worktree_adapter,
        "_run_git_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(GitQuiescenceError("unproven")),
    )

    with pytest.raises(GitQuiescenceError, match="unproven"):
        materialize_cache(source, cache, owner=owner)

    staging = list(cache.parent.glob("clone-*"))
    assert len(staging) == 1
    assert (staging[0] / "owner.json").is_file()

    git_worktree_adapter._cleanup_abandoned_clones(
        cache.parent, lambda candidate: "dead" if candidate == owner else "live"
    )
    assert not staging[0].exists()


def test_quiescence_guardian_holds_invocation_until_group_is_absent(
    tmp_path: Path,
) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], process_group=0
    )
    owner = ProcessIdentity(
        "guardian-owner", workspaces_adapter._boot_identity(), 2**30, "start"
    )
    registry = Registry(tmp_path / "state" / "registry.sqlite3")
    workspaces = Workspaces(
        registry,
        process_identity=owner,
    )
    invocation = workspaces._invocation_process_identity()
    intent, _ = registry.begin_create_intent(
        request_key="guardian",
        request_id=None,
        request_json="{}",
        target_path=str(tmp_path / "target"),
        workspace_id="workspace",
        operation_id="operation",
        prepare_cache=False,
    )
    epoch = registry.acquire_lease(
        scope_kind="workspace",
        scope_key=intent.workspace_id,
        operation_id=intent.operation_id,
        owner=invocation,
        owner_status=workspaces._owner_status,
    )
    successor = ProcessIdentity("successor", "boot", 2**30 - 1, "start")
    try:
        git_worktree_adapter._retain_quiescence_guardian(
            process.pid,
            liveness_fd=workspaces._invocation_descriptor(invocation),
        )
        workspaces._finish_invocation(invocation)
        assert workspaces._owner_status(invocation) == "inconclusive"
        with pytest.raises(RegistryError, match="Workspace mutation is busy"):
            registry.acquire_lease(
                scope_kind="workspace",
                scope_key=intent.workspace_id,
                operation_id=intent.operation_id,
                owner=successor,
                owner_status=workspaces._owner_status,
            )
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)

    deadline = time.monotonic() + 2
    while workspaces._owner_status(invocation) != "dead":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert (
        registry.acquire_lease(
            scope_kind="workspace",
            scope_key=intent.workspace_id,
            operation_id=intent.operation_id,
            owner=successor,
            owner_status=workspaces._owner_status,
        )
        == epoch + 1
    )


def test_quiescence_guardian_treats_zombie_only_group_as_stopped() -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"], process_group=0)
    try:
        deadline = time.monotonic() + 2
        while git_worktree_adapter._process_group_running(process.pid):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert git_guardian._process_group_running(process.pid) is False
    finally:
        process.wait(timeout=2)


@pytest.mark.parametrize(
    ("output", "expected"),
    [("77 S\n", True), ("78 Z\n", False)],
)
def test_guardian_process_probe_has_portable_fallback(
    output: str, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda _path: False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, output, ""),
    )

    assert git_guardian._process_group_running(77) is expected


def test_receipt_staging_cleanup_failure_is_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = tmp_path / "receipt"
    descriptor = os.open(tmp_path, os.O_RDONLY)
    original_unlink = Path.unlink

    def fail_temporary_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path.name.startswith(".receipt."):
            raise OSError("cleanup failed")
        original_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", fail_temporary_unlink)
    try:
        with pytest.raises(GitError, match="Failed to clean receipt staging"):
            git_worktree_adapter._create_staging_receipt(receipt, "a" * 64, descriptor)
    finally:
        os.close(descriptor)


def test_preparation_receipt_preserves_write_and_cleanup_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert repository_generation(tmp_path, create=True)
    monkeypatch.setattr(
        os, "write", lambda *_args: (_ for _ in ()).throw(OSError("write failed"))
    )
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cleanup failed")),
    )

    with pytest.raises(
        GitError,
        match=(
            "Repository preparation receipt is unavailable; "
            "failed to clean receipt staging: cleanup failed"
        ),
    ):
        git_worktree_adapter._write_preparation_receipt(tmp_path, "operation", False)


def test_clone_cache_removes_only_proven_dead_private_clone(tmp_path: Path) -> None:
    source_repository = tmp_path / "source"
    repository(source_repository)
    cache = tmp_path / "cache" / "repository.git"
    cache.parent.mkdir(mode=0o700)
    abandoned = cache.parent / "clone-dead-private"
    abandoned.mkdir()
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


def test_abandoned_clone_cleanup_accepts_concurrent_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "cache"
    abandoned = parent / "clone-dead-private"
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
    remove = shutil.rmtree

    def raced_remove(path: Path) -> None:
        remove(path)
        raise FileNotFoundError

    monkeypatch.setattr(shutil, "rmtree", raced_remove)

    git_worktree_adapter._cleanup_abandoned_clones(parent, lambda _owner: "dead")

    assert not abandoned.exists()


def test_abandoned_clone_cleanup_recovers_missing_owner_metadata(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "cache"
    parent.mkdir()
    dead = ProcessIdentity("dead", "boot", 1001, "start")
    abandoned = Path(
        tempfile.mkdtemp(
            prefix=git_worktree_adapter._clone_owner_prefix(dead), dir=parent
        )
    )

    git_worktree_adapter._cleanup_abandoned_clones(parent, lambda _owner: "dead")

    assert not abandoned.exists()


@pytest.mark.parametrize("metadata", ["{", "[]", '{"pid":"1001"}'])
def test_abandoned_clone_cleanup_preserves_invalid_owner_metadata(
    tmp_path: Path, metadata: str
) -> None:
    parent = tmp_path / "cache"
    parent.mkdir()
    dead = ProcessIdentity("dead", "boot", 1001, "start")
    candidate = Path(
        tempfile.mkdtemp(
            prefix=git_worktree_adapter._clone_owner_prefix(dead), dir=parent
        )
    )
    (candidate / "owner.json").write_text(metadata, encoding="utf-8")

    git_worktree_adapter._cleanup_abandoned_clones(parent, lambda _owner: "dead")

    assert candidate.exists()


def test_abandoned_clone_cleanup_preserves_owner_mismatch(tmp_path: Path) -> None:
    parent = tmp_path / "cache"
    parent.mkdir()
    named_owner = ProcessIdentity("named", "boot", 1001, "start")
    recorded_owner = ProcessIdentity("recorded", "boot", 1002, "start")
    candidate = Path(
        tempfile.mkdtemp(
            prefix=git_worktree_adapter._clone_owner_prefix(named_owner), dir=parent
        )
    )
    git_worktree_adapter._write_clone_owner(candidate, recorded_owner)

    git_worktree_adapter._cleanup_abandoned_clones(parent, lambda _owner: "dead")

    assert candidate.exists()


def test_abandoned_clone_cleanup_preserves_unreadable_owner_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "cache"
    parent.mkdir()
    dead = ProcessIdentity("dead", "boot", 1001, "start")
    candidate = Path(
        tempfile.mkdtemp(
            prefix=git_worktree_adapter._clone_owner_prefix(dead), dir=parent
        )
    )
    metadata = candidate / "owner.json"
    metadata.write_text("{}", encoding="utf-8")
    original = Path.read_text

    def unreadable(path: Path, *args: object, **kwargs: object) -> str:
        if path == metadata:
            raise PermissionError
        return original(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", unreadable)

    git_worktree_adapter._cleanup_abandoned_clones(parent, lambda _owner: "dead")

    assert candidate.exists()


def test_clone_owner_publication_is_file_replace_directory_ordered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = ProcessIdentity("owner", "boot", 1001, "start")
    events: list[str] = []
    original_fsync = os.fsync
    original_replace = os.replace

    def record_fsync(descriptor: int) -> None:
        events.append(
            "directory-fsync"
            if stat.S_ISDIR(os.fstat(descriptor).st_mode)
            else "file-fsync"
        )
        original_fsync(descriptor)

    def record_replace(source: Path, target: Path) -> None:
        events.append("replace")
        original_replace(source, target)

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(os, "replace", record_replace)

    git_worktree_adapter._write_clone_owner(tmp_path, owner)

    assert events == ["file-fsync", "replace", "directory-fsync"]


def test_clone_cache_cleans_invocation_when_owner_metadata_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_repository = tmp_path / "source"
    repository(source_repository)
    cache = tmp_path / "cache" / "repository.git"

    def fail_owner(_path: Path, _owner: ProcessIdentity) -> None:
        raise OSError("metadata unavailable")

    monkeypatch.setattr(git_worktree_adapter, "_write_clone_owner", fail_owner)
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
    monkeypatch.setenv("GIT_NO_REPLACE_OBJECTS", "0")
    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "0")
    captured: dict[str, str] = {}

    def run(
        command: list[str], environment: dict[str, str], **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        captured.update(environment)
        return subprocess.CompletedProcess([], 0, b"a" * 40, b"")

    monkeypatch.setattr(git_worktree_adapter, "_run_supervised_git", run)
    git_worktree_adapter._run_git_process(tmp_path, "--version")

    assert captured["LC_ALL"] == "C"
    assert captured["LANG"] == "C"
    assert captured["GIT_CONFIG_NOSYSTEM"] == "1"
    assert captured["GIT_CONFIG_SYSTEM"] == os.devnull
    assert captured["GIT_CONFIG_GLOBAL"] == os.devnull
    assert captured["GIT_ATTR_NOSYSTEM"] == "1"
    assert captured["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert captured["GIT_NO_LAZY_FETCH"] == "1"


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
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
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


def test_worktree_reconciliation_rejects_another_repository(tmp_path: Path) -> None:
    expected = tmp_path / "expected"
    commit = repository(expected)
    other = tmp_path / "other"
    previous_umask = os.umask(0o077)
    try:
        git(tmp_path, "clone", "--no-hardlinks", str(expected), str(other))
    finally:
        os.umask(previous_umask)
    target = tmp_path / "target"
    token = "8" * 64
    create_worktree(
        other,
        target=target,
        branch="topic",
        commit=commit,
        ownership_token=token,
        reconcile=False,
    )

    with pytest.raises(GitError, match="Repository identity"):
        create_worktree(
            expected,
            target=target,
            branch="topic",
            commit=commit,
            ownership_token=token,
            reconcile=True,
        )


def test_staged_worktree_reconciliation_rejects_another_repository(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "expected"
    commit = repository(expected)
    other = tmp_path / "other"
    git(tmp_path, "clone", "--no-hardlinks", str(expected), str(other))
    target = tmp_path / "target"
    token = "7" * 64
    staging = target.parent / f".fangorn-{token}"
    receipt = target.parent / f".fangorn-{token}.intent"
    git(other, "worktree", "add", "-b", "topic", str(staging), commit)
    establish_worktree_generation(observe_worktree(staging).git_dir, token)
    receipt.write_text(token, encoding="ascii")

    with pytest.raises(GitError, match="Repository identity"):
        create_worktree(
            expected,
            target=target,
            branch="topic",
            commit=commit,
            ownership_token=token,
            reconcile=True,
        )

    assert staging.exists()
    assert not target.exists()


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


def test_matching_staged_worktree_rejects_executable_worktree_configuration(
    tmp_path: Path,
) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    git(source, "config", "extensions.worktreeConfig", "true")
    target = tmp_path / "target"
    token = "4" * 64
    staging = target.parent / f".fangorn-{token}"
    receipt = target.parent / f".fangorn-{token}.intent"
    invoked = tmp_path / "filter-invoked"
    previous_umask = os.umask(0o077)
    try:
        git(source, "worktree", "add", "-b", "topic", str(staging), commit)
        observation = observe_worktree(staging)
        establish_worktree_generation(observation.git_dir, token)
        receipt.write_text(token, encoding="ascii")
        git(
            staging,
            "config",
            "--worktree",
            "filter.evil.smudge",
            f"touch {invoked}",
        )
    finally:
        os.umask(previous_umask)

    with pytest.raises(GitError, match="executable checkout configuration"):
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
    assert not invoked.exists()


def test_matching_final_worktree_rejects_tampered_executable_configuration(
    tmp_path: Path,
) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    git(source, "config", "extensions.worktreeConfig", "true")
    target = tmp_path / "target"
    token = "3" * 64
    invoked = tmp_path / "filter-invoked"
    create_worktree(
        source,
        target=target,
        branch="topic",
        commit=commit,
        ownership_token=token,
        reconcile=False,
    )
    previous_umask = os.umask(0o077)
    try:
        git(
            target,
            "config",
            "--worktree",
            "filter.evil.smudge",
            f"touch {invoked}",
        )
    finally:
        os.umask(previous_umask)

    with pytest.raises(GitError, match="executable checkout configuration"):
        create_worktree(
            source,
            target=target,
            branch="topic",
            commit=commit,
            ownership_token=token,
            reconcile=True,
        )

    assert target.exists()
    assert not invoked.exists()


def test_checkout_configuration_rejects_core_worktree(tmp_path: Path) -> None:
    source = tmp_path / "repository"
    repository(source)
    git(source, "config", "core.worktree", str(tmp_path / "outside"))

    with pytest.raises(GitError, match="unsafe checkout configuration"):
        git_worktree_adapter._reject_executable_checkout_configuration(source)


def test_reconciliation_rejects_reported_worktree_path_redirection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    target = tmp_path / "target"
    token = "6" * 64
    create_worktree(
        source,
        target=target,
        branch="topic",
        commit=commit,
        ownership_token=token,
        reconcile=False,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    observe = cast(
        Callable[..., WorktreeObservation],
        vars(git_worktree_adapter)["observe_worktree"],
    )

    def redirected(path: Path, **kwargs: Any) -> WorktreeObservation:
        observation = observe(path, **kwargs)
        return replace(observation, path=outside) if path == target else observation

    monkeypatch.setattr(git_worktree_adapter, "observe_worktree", redirected)

    with pytest.raises(GitError, match="Worktree path"):
        create_worktree(
            source,
            target=target,
            branch="topic",
            commit=commit,
            ownership_token=token,
            reconcile=True,
        )


@pytest.mark.parametrize("entry", ["directory", "config"])
def test_worktree_creation_rejects_shared_repository_configuration(
    tmp_path: Path, entry: str
) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    administrative = source / ".git"
    unsafe = administrative if entry == "directory" else administrative / "config"
    unsafe.chmod(unsafe.stat().st_mode | stat.S_IWOTH)

    with pytest.raises(GitError, match="checkout configuration is unsafe"):
        create_worktree(
            source,
            target=tmp_path / "target",
            branch="topic",
            commit=commit,
            ownership_token="a" * 64,
            reconcile=False,
        )

    assert not (tmp_path / "target").exists()


def test_worktree_creation_rejects_group_writable_configuration(
    tmp_path: Path,
) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    configured = source / ".git" / "config"
    configured.chmod(configured.stat().st_mode | stat.S_IWGRP)

    with pytest.raises(GitError, match="checkout configuration is unsafe"):
        create_worktree(
            source,
            target=tmp_path / "target",
            branch="topic",
            commit=commit,
            ownership_token="f" * 64,
            reconcile=False,
        )


@pytest.mark.parametrize(
    ("permissions", "terminal_errno", "expected", "raises"),
    [
        (0, errno.EINVAL, False, False),
        (4, 0, True, False),
        (0, errno.EPERM, False, True),
    ],
)
def test_darwin_acl_uses_native_iteration_and_einval_termination(
    monkeypatch: pytest.MonkeyPatch,
    permissions: int,
    terminal_errno: int,
    expected: bool,
    raises: bool,
) -> None:
    calls: list[int] = []
    freed: list[int] = []

    class Function:
        argtypes: object = None
        restype: object = None

        def __init__(self, callback: Callable[..., int]) -> None:
            self.callback = callback

        def __call__(self, *arguments: Any) -> int:
            return self.callback(*arguments)

    def set_tag(_entry: object, target: Any) -> int:
        ctypes.cast(target, ctypes.POINTER(ctypes.c_int)).contents.value = 1
        return 0

    def set_permissions(_entry: object, target: Any) -> int:
        ctypes.cast(
            target, ctypes.POINTER(ctypes.c_uint64)
        ).contents.value = permissions
        return 0

    def get_entry(_acl: object, which: int, _entry: object) -> int:
        calls.append(which)
        if len(calls) <= 2:
            ctypes.set_errno(0)
            return 0
        ctypes.set_errno(terminal_errno)
        return -1

    def free_acl(acl: int) -> int:
        freed.append(acl)
        return 0

    class Library:
        acl_get_fd_np = Function(lambda _fd, _kind: 123)
        acl_get_entry = Function(get_entry)
        acl_get_tag_type = Function(set_tag)
        acl_get_permset_mask_np = Function(set_permissions)
        acl_free = Function(free_acl)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: Library())

    if raises:
        with pytest.raises(GitError, match="ACL is unsafe"):
            git_worktree_adapter._darwin_acl_allows_write(10)
    else:
        assert git_worktree_adapter._darwin_acl_allows_write(10) is expected
    assert calls == ([0] if expected else [0, -1, -1])
    assert freed == [123]


@pytest.mark.parametrize("permissions", [2, 8, 1 << 7, 1 << 9, 1 << 11])
def test_darwin_private_acl_rejects_read_and_search(
    monkeypatch: pytest.MonkeyPatch, permissions: int
) -> None:
    class Function:
        argtypes: object = None
        restype: object = None

        def __init__(self, callback: Callable[..., int]) -> None:
            self.callback = callback

        def __call__(self, *arguments: Any) -> int:
            return self.callback(*arguments)

    def set_tag(_entry: object, target: Any) -> int:
        ctypes.cast(target, ctypes.POINTER(ctypes.c_int)).contents.value = 1
        return 0

    def set_permissions(_entry: object, target: Any) -> int:
        ctypes.cast(
            target, ctypes.POINTER(ctypes.c_uint64)
        ).contents.value = permissions
        return 0

    calls = 0

    def get_entry(_acl: object, _which: int, _entry: object) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return 0
        ctypes.set_errno(errno.EINVAL)
        return -1

    class Library:
        acl_get_fd_np = Function(lambda _fd, _kind: 123)
        acl_get_entry = Function(get_entry)
        acl_get_tag_type = Function(set_tag)
        acl_get_permset_mask_np = Function(set_permissions)
        acl_free = Function(lambda _acl: 0)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: Library())

    assert permissions_adapter.descriptor_has_private_acl(10)


def test_cache_namespace_rejects_non_private_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        git_worktree_adapter,
        "_darwin_acl_allows_private_access",
        lambda _descriptor: True,
    )

    with pytest.raises(GitError, match="cache namespace is unsafe"):
        git_worktree_adapter._prepare_cache_parent(
            tmp_path / "cache", tmp_path / "cache" / "repositories"
        )


def test_staging_rejects_non_private_acl_after_fchmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(
        git_worktree_adapter,
        "_darwin_acl_allows_private_access",
        lambda _descriptor: True,
    )
    try:
        with pytest.raises(GitError, match="Workspace staging path is unsafe"):
            git_worktree_adapter._secure_staging_directory(parent, staging.name)
    finally:
        os.close(parent)


def test_reconciliation_rejects_non_private_target_acl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    target = tmp_path / "target"
    token = "2" * 64
    create_worktree(
        source,
        target=target,
        branch="topic",
        commit=commit,
        ownership_token=token,
        reconcile=False,
    )
    target_identity = (target.stat().st_dev, target.stat().st_ino)

    def target_has_non_private_acl(descriptor: int) -> bool:
        metadata = os.fstat(descriptor)
        return (metadata.st_dev, metadata.st_ino) == target_identity

    monkeypatch.setattr(
        git_worktree_adapter,
        "_darwin_acl_allows_private_access",
        target_has_non_private_acl,
    )

    with pytest.raises(GitError, match="Workspace target path is unsafe"):
        create_worktree(
            source,
            target=target,
            branch="topic",
            commit=commit,
            ownership_token=token,
            reconcile=True,
        )

    assert target.exists()


def test_worktree_creation_rejects_symlinked_repository_configuration(
    tmp_path: Path,
) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    configured = source / ".git" / "config"
    replacement = source / ".git" / "real-config"
    configured.rename(replacement)
    configured.symlink_to(replacement.name)

    with pytest.raises(GitError, match="checkout configuration is unsafe"):
        create_worktree(
            source,
            target=tmp_path / "target",
            branch="topic",
            commit=commit,
            ownership_token="9" * 64,
            reconcile=False,
        )


def test_worktree_creation_rejects_local_configuration_includes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "repository"
    commit = repository(source)
    included = tmp_path / "included.gitconfig"
    included.write_text("", encoding="utf-8")
    git(source, "config", "include.path", str(included))

    with pytest.raises(GitError, match="checkout configuration includes"):
        create_worktree(
            source,
            target=tmp_path / "target",
            branch="topic",
            commit=commit,
            ownership_token="b" * 64,
            reconcile=False,
        )

    assert not (tmp_path / "target").exists()


def test_worktree_creation_rejects_gitdir_conditional_filters_before_checkout(
    tmp_path: Path,
) -> None:
    source = tmp_path / "repository"
    repository(source)
    invoked = tmp_path / "conditional-filter-invoked"
    included = tmp_path / "worktree.gitconfig"
    included.write_text(
        f'[filter "evil"]\n\tsmudge = touch {invoked}\n', encoding="utf-8"
    )
    (source / ".gitattributes").write_text("*.payload filter=evil\n", encoding="utf-8")
    (source / "content.payload").write_text("content\n", encoding="utf-8")
    git(source, "add", ".gitattributes", "content.payload")
    git(source, "commit", "-m", "add conditionally filtered content")
    commit = git(source, "rev-parse", "HEAD")
    git(
        source,
        "config",
        f"includeIf.gitdir:{source / '.git' / 'worktrees'}/**.path",
        str(included),
    )

    with pytest.raises(GitError, match="checkout configuration includes"):
        create_worktree(
            source,
            target=tmp_path / "target",
            branch="topic",
            commit=commit,
            ownership_token="c" * 64,
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
    observe_worktree(source, create_repository_generation=True)
    original_write = os.write
    interrupted = False

    def interrupt_write(descriptor: int, value: bytes) -> int:
        nonlocal interrupted
        if not interrupted and stat.S_ISREG(os.fstat(descriptor).st_mode):
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
    git(source, "worktree", "add", "-b", "topic", str(unrelated), commit)

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
