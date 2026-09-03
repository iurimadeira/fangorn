from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
from git_helpers import git, initialize_repository

import fangorn._git_anchor as git_anchor
import fangorn._git_supervisor as git_supervisor
import fangorn.git_worktree as git_worktree_adapter
from fangorn.git import GitError, establish_worktree_generation, repository_generation
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

    def drain(value: object, _process_group: int) -> bool:
        drained.append(value)
        return True

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
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(
        os, "write", lambda *args: (_ for _ in ()).throw(BrokenPipeError())
    )
    monkeypatch.setattr(os, "close", lambda _descriptor: None)
    monkeypatch.setattr(git_supervisor, "_drain", drain)
    monkeypatch.setattr(git_supervisor, "_child_running", lambda _child: False)
    monkeypatch.setattr(git_supervisor, "_finish_captures", lambda *_args: "ok")

    assert git_supervisor.main() == 0
    assert drained == [child]


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


def test_supervisor_observes_child_without_reaping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    monkeypatch.setattr(os, "waitid", lambda *args: object())

    assert git_supervisor._child_running(child) is False


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


def test_supervisor_pid_reader_deadline_rejects_unclosed_frame() -> None:
    read, write = os.pipe()
    try:
        os.write(write, b"123\n")

        assert (
            git_worktree_adapter._read_supervisor_pid(
                read, deadline=time.monotonic() + 0.05
            )
            is None
        )
    finally:
        os.close(read)
        os.close(write)


def test_supervisor_pid_reader_rejects_eof_before_newline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = iter((b"123", b""))
    monkeypatch.setattr(os, "read", lambda *args: next(chunks))

    assert git_worktree_adapter._read_supervisor_pid(10) is None


def test_supervisor_pid_reader_rejects_split_trailing_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = iter((b"123\n", b"extra", b""))
    monkeypatch.setattr(os, "read", lambda *args: next(chunks))

    assert git_worktree_adapter._read_supervisor_pid(10) is None


def test_supervisor_completion_reader_requires_one_complete_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = iter((b"-1", b"5\n", b""))
    monkeypatch.setattr(os, "read", lambda *args: next(chunks))
    assert git_worktree_adapter._read_supervisor_completion(10) == -15

    chunks = iter((b"0\n", b"extra", b""))
    assert git_worktree_adapter._read_supervisor_completion(10) is None


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


def test_group_probe_falls_back_when_proc_scan_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda _path: True)
    monkeypatch.setattr(Path, "iterdir", lambda _path: iter((Path("/proc/123"),)))
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError()),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, "123 77 S\n", ""
        ),
    )

    assert git_supervisor._process_group_running(77) is True


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

    assert git_supervisor._completion_failure(False, {}, 1, time.monotonic()) == 125
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


def test_anchor_survives_group_termination_before_arming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_reader, control_writer = os.pipe()
    liveness_reader, liveness_writer = os.pipe()
    handlers: list[tuple[int, Any]] = []
    os.write(control_writer, b"x")
    monkeypatch.setattr(
        sys,
        "argv",
        ["anchor", str(control_reader), str(liveness_reader), "1"],
    )
    monkeypatch.setattr(
        signal, "signal", lambda number, handler: handlers.append((number, handler))
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

    assert handlers == [(signal.SIGTERM, signal.SIG_IGN)]


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
    assert captured["timeout"] == 0.25


def test_parent_group_cleanup_stops_at_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[int] = []
    monkeypatch.setattr(os, "killpg", lambda _pid, sent: signals.append(sent))

    with pytest.raises(GitError, match="Cannot confirm Git process-group termination"):
        git_worktree_adapter._cancel_process_group(77, deadline=time.monotonic())

    assert signals == [signal.SIGTERM, signal.SIGKILL]


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
    with pytest.raises(GitError, match="unsafe"):
        git_worktree_adapter._require_safe_directory(metadata)

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
    captured: dict[str, str] = {}

    def run(
        command: list[str], environment: dict[str, str], **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        captured.update(environment)
        return subprocess.CompletedProcess([], 0, b"a" * 40, b"")

    monkeypatch.setattr(git_worktree_adapter, "_run_supervised_git", run)
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
