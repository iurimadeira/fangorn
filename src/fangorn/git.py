from __future__ import annotations

import fcntl
import os
import re
import secrets
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class GitError(RuntimeError):
    """Git could not prove a requested worktree identity."""


@dataclass(frozen=True)
class WorktreeObservation:
    repository_common_dir: Path
    git_common_dir_generation: str | None
    git_dir: Path
    git_dir_generation: str | None
    path: Path
    branch: str | None
    head: str | None
    observed_at: str
    observation_token: int | None = None


@dataclass(frozen=True)
class _Snapshot:
    repository_common_dir: Path
    git_common_dir_generation: str | None
    git_dir: Path
    git_dir_generation: str | None
    path: Path
    branch: str | None
    head: str | None


REPOSITORY_LOCAL_ENVIRONMENT = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CEILING_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_SYSTEM",
    "GIT_DIR",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
)
OBSERVATION_ATTEMPTS = 3
MINIMUM_GIT_VERSION = (2, 31)
MARKER_LOCK_TIMEOUT_SECONDS = 0.25
MARKER_LOCK_RETRY_SECONDS = 0.01
GENERATION_MARKER_NAME = "fangorn-worktree-generation"
REPOSITORY_GENERATION_MARKER_NAME = "fangorn-repository-generation"


def require_supported_git(path: Path, *, liveness_fd: int | None = None) -> None:
    """Reject mutation when the process Git does not meet Fangorn's minimum."""
    _require_supported_git(path, liveness_fd=liveness_fd)


def establish_worktree_generation(directory: Path, ownership_token: str) -> str:
    """Establish the immutable planned owner token for a new Worktree Resource."""
    return _create_generation_marker(
        directory,
        expected_generation=ownership_token,
    )


def repository_generation(directory: Path, *, create: bool) -> str | None:
    """Read or establish the immutable identity of a Repository cache entry."""
    return _generation(
        directory,
        marker_name=REPOSITORY_GENERATION_MARKER_NAME,
        identity="repository",
        create=create,
    )


def _run_git(
    path: Path,
    *arguments: str,
    allowed_exit_codes: frozenset[int] = frozenset(),
    liveness_fd: int | None = None,
) -> str | None:
    from fangorn.git_worktree import _run_git_process

    result = _run_git_process(
        path,
        *arguments,
        liveness_fd=liveness_fd,
        disable_hooks=False,
        isolate_config=False,
    )

    if result.returncode in allowed_exit_codes:
        return None
    if result.returncode != 0:
        detail = _record(result.stderr) or _record(result.stdout)
        if not detail:
            detail = f"Git command failed with exit status {result.returncode}"
        raise GitError(detail)
    return _record(result.stdout)


def observe_worktree(
    path: Path,
    *,
    create_generation: bool = False,
    create_repository_generation: bool | None = None,
    create_worktree_generation: bool | None = None,
    reserve_observation: Callable[[], int] | None = None,
    liveness_fd: int | None = None,
) -> WorktreeObservation:
    requested_path = _resolve_requested_path(path)
    last_failure: GitError | None = None
    if create_repository_generation is None:
        create_repository_generation = create_generation
    if create_worktree_generation is None:
        create_worktree_generation = create_generation

    def capture() -> _Snapshot:
        if (
            create_repository_generation == create_generation
            and create_worktree_generation == create_generation
        ):
            if liveness_fd is None:
                return _capture_snapshot(
                    requested_path, create_generation=create_generation
                )
            return _capture_snapshot(
                requested_path,
                create_generation=create_generation,
                liveness_fd=liveness_fd,
            )
        if liveness_fd is None:
            return _capture_snapshot(
                requested_path,
                create_repository_generation=create_repository_generation,
                create_worktree_generation=create_worktree_generation,
            )
        return _capture_snapshot(
            requested_path,
            create_repository_generation=create_repository_generation,
            create_worktree_generation=create_worktree_generation,
            liveness_fd=liveness_fd,
        )

    for _ in range(OBSERVATION_ATTEMPTS):
        observed_at = _timestamp()
        try:
            _require_supported_git(requested_path, liveness_fd=liveness_fd)
            first = capture()
            observation_token = (
                reserve_observation() if reserve_observation is not None else None
            )
            second = capture()
        except GitError as error:
            last_failure = error
            continue
        if first == second:
            return WorktreeObservation(
                repository_common_dir=second.repository_common_dir,
                git_common_dir_generation=second.git_common_dir_generation,
                git_dir=second.git_dir,
                git_dir_generation=second.git_dir_generation,
                path=second.path,
                branch=second.branch,
                head=second.head,
                observed_at=observed_at,
                observation_token=observation_token,
            )
        last_failure = GitError(
            "Git worktree changed during observation; retry the command"
        )

    if last_failure is not None:
        raise last_failure
    raise GitError("Git worktree could not be observed consistently")


def _resolve_requested_path(path: Path) -> Path:
    try:
        requested_path = path.resolve(strict=True)
    except FileNotFoundError as error:
        if path.is_symlink():
            raise GitError(
                f"Cannot resolve path {path}: symlink target is unavailable"
            ) from error
        raise GitError(f"Path does not exist: {path}") from error
    except (OSError, RuntimeError) as error:
        detail = getattr(error, "strerror", None) or str(error)
        raise GitError(f"Cannot resolve path {path}: {detail}") from error
    if not requested_path.is_dir():
        raise GitError(f"Path is not a directory: {requested_path}")
    return requested_path


def _capture_snapshot(
    requested_path: Path,
    *,
    create_generation: bool = False,
    create_repository_generation: bool | None = None,
    create_worktree_generation: bool | None = None,
    liveness_fd: int | None = None,
) -> _Snapshot:
    if create_repository_generation is None:
        create_repository_generation = create_generation
    if create_worktree_generation is None:
        create_worktree_generation = create_generation
    inside = _run_git(
        requested_path,
        "rev-parse",
        "--is-inside-work-tree",
        liveness_fd=liveness_fd,
    )
    if inside != "true":
        raise GitError(f"Path is not inside a Git worktree: {requested_path}")

    worktree_path = _required_path(
        _run_git(
            requested_path,
            "rev-parse",
            "--show-toplevel",
            liveness_fd=liveness_fd,
        ),
        "worktree path",
    )
    common_dir = _required_path(
        _run_git(
            requested_path,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
            liveness_fd=liveness_fd,
        ),
        "Git common directory",
    )
    git_dir = _required_path(
        _run_git(
            requested_path,
            "rev-parse",
            "--path-format=absolute",
            "--git-dir",
            liveness_fd=liveness_fd,
        ),
        "Git administrative directory",
    )
    common_directory_before = _directory_identity(common_dir)
    git_directory_before = _directory_identity(git_dir)
    repository_generation_before = _generation(
        common_dir,
        marker_name=REPOSITORY_GENERATION_MARKER_NAME,
        identity="repository",
        create=create_repository_generation,
    )
    worktree_generation_before = _generation(
        git_dir,
        marker_name=GENERATION_MARKER_NAME,
        identity="worktree",
        create=create_worktree_generation,
    )
    branch = _run_git(
        requested_path,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        allowed_exit_codes=frozenset({1}),
        liveness_fd=liveness_fd,
    )
    head = _run_git(
        requested_path,
        "rev-parse",
        "--verify",
        "--quiet",
        "HEAD",
        allowed_exit_codes=frozenset({1}),
        liveness_fd=liveness_fd,
    )
    repository_generation_after = _generation(
        common_dir,
        marker_name=REPOSITORY_GENERATION_MARKER_NAME,
        identity="repository",
        create=create_repository_generation,
    )
    worktree_generation_after = _generation(
        git_dir,
        marker_name=GENERATION_MARKER_NAME,
        identity="worktree",
        create=create_worktree_generation,
    )
    common_directory_after = _directory_identity(common_dir)
    git_directory_after = _directory_identity(git_dir)
    if common_directory_before != common_directory_after:
        raise GitError("Git common directory changed during observation")
    if git_directory_before != git_directory_after:
        raise GitError("Git administrative directory changed during observation")
    if repository_generation_before != repository_generation_after:
        raise GitError(
            "Fangorn repository generation marker changed during observation; "
            "repository identity cannot be trusted"
        )
    if worktree_generation_before != worktree_generation_after:
        raise GitError(
            "Fangorn worktree generation marker changed during observation; "
            "worktree identity cannot be trusted"
        )

    return _Snapshot(
        repository_common_dir=common_dir,
        git_common_dir_generation=repository_generation_after,
        git_dir=git_dir,
        git_dir_generation=worktree_generation_after,
        path=worktree_path,
        branch=branch,
        head=head,
    )


def _required_path(value: str | None, label: str) -> Path:
    if value is None or value == "":
        raise GitError(f"Git did not report {label}")
    try:
        return Path(value).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise GitError(f"Git reported an invalid {label}: {value}") from error


def _require_supported_git(path: Path, *, liveness_fd: int | None = None) -> None:
    reported = _run_git(path, "--version", liveness_fd=liveness_fd)
    if reported is None:
        raise GitError("Cannot determine Git version; Git 2.31 or newer is required")
    match = re.match(r"git version ([0-9]+)\.([0-9]+)(?:\.([0-9]+))?", reported)
    if match is None:
        raise GitError("Cannot determine Git version; Git 2.31 or newer is required")
    version = (int(match[1]), int(match[2]))
    if version < MINIMUM_GIT_VERSION:
        found = ".".join(part for part in match.groups() if part is not None)
        raise GitError(f"Git 2.31 or newer is required; found {found}")


def _directory_identity(path: Path) -> str:
    try:
        metadata = path.stat()
    except FileNotFoundError as error:
        raise GitError(f"Git administrative directory disappeared: {path}") from error
    except OSError as error:
        detail = error.strerror or str(error)
        raise GitError(
            f"Cannot inspect Git administrative directory {path}: {detail}"
        ) from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise GitError(f"Git administrative path is not a directory: {path}")
    return f"{metadata.st_dev}:{metadata.st_ino}"


def _generation(
    directory: Path, *, marker_name: str, identity: str, create: bool
) -> str | None:
    generation = _read_generation_marker(
        directory, marker_name=marker_name, identity=identity
    )
    if generation is not None or not create:
        return generation
    return _create_generation_marker(
        directory, marker_name=marker_name, identity=identity
    )


def _read_generation_marker(
    directory: Path,
    *,
    marker_name: str = GENERATION_MARKER_NAME,
    identity: str = "worktree",
    directory_descriptor: int | None = None,
) -> str | None:
    marker = directory / marker_name
    try:
        if directory_descriptor is None:
            metadata = marker.lstat()
        else:
            metadata = os.stat(
                marker_name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
    except FileNotFoundError:
        return None
    except OSError as error:
        detail = error.strerror or str(error)
        raise GitError(
            f"Cannot inspect Fangorn {identity} generation marker {marker}: "
            f"{detail}; {identity} identity cannot be trusted"
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise GitError(
            f"Fangorn {identity} generation marker is not a regular file; "
            f"{identity} identity cannot be trusted"
        )

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(
            marker if directory_descriptor is None else marker_name,
            flags,
            dir_fd=directory_descriptor,
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise GitError(
                f"Fangorn {identity} generation marker is not a regular file; "
                f"{identity} identity cannot be trusted"
            )
        content = b""
        while len(content) < 66:
            chunk = os.read(descriptor, 66 - len(content))
            if not chunk:
                break
            content += chunk
    except GitError:
        raise
    except OSError as error:
        detail = error.strerror or str(error)
        raise GitError(
            f"Cannot read Fangorn {identity} generation marker {marker}: {detail}; "
            f"{identity} identity cannot be trusted"
        ) from error
    finally:
        if descriptor is not None:
            _close_descriptor(
                descriptor,
                context="generation marker read",
                ignore_errors=sys.exc_info()[0] is not None,
            )

    value = content[:-1]
    if (
        len(content) != 65
        or content[-1:] != b"\n"
        or any(byte not in b"0123456789abcdef" for byte in value)
    ):
        raise GitError(
            f"Fangorn {identity} generation marker is malformed; "
            f"{identity} identity cannot be trusted"
        )
    return value.decode("ascii")


def _create_generation_marker(
    directory: Path,
    *,
    marker_name: str = GENERATION_MARKER_NAME,
    identity: str = "worktree",
    expected_generation: str | None = None,
) -> str:
    marker = directory / marker_name
    pending_name = f".{marker_name}.pending"
    directory_descriptor: int | None = None
    pending_descriptor: int | None = None
    lock_acquired = False
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        directory_descriptor = os.open(directory, flags)
        if not stat.S_ISDIR(os.fstat(directory_descriptor).st_mode):
            raise GitError(
                f"Git {identity} administrative path is not a directory: {directory}"
            )
        _acquire_marker_lock(directory_descriptor)
        lock_acquired = True
        _verify_locked_directory(directory, directory_descriptor, identity=identity)

        winner = _read_generation_marker(
            directory,
            marker_name=marker_name,
            identity=identity,
            directory_descriptor=directory_descriptor,
        )
        if winner is not None:
            if expected_generation is not None and winner != expected_generation:
                raise GitError(
                    f"Fangorn {identity} generation marker does not match "
                    "the planned ownership token"
                )
            _verify_locked_directory(directory, directory_descriptor, identity=identity)
            _cleanup_pending_marker(
                pending_name, directory_descriptor, ignore_errors=False
            )
            os.fsync(directory_descriptor)
            return winner

        _cleanup_pending_marker(pending_name, directory_descriptor, ignore_errors=False)
        generation = expected_generation or secrets.token_hex(32)
        if not re.fullmatch(r"[0-9a-f]{64}", generation):
            raise GitError(f"Planned Fangorn {identity} ownership token is invalid")
        payload = f"{generation}\n".encode("ascii")
        pending_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            pending_flags |= os.O_NOFOLLOW
        pending_descriptor = os.open(
            pending_name,
            pending_flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        written = 0
        while written < len(payload):
            written += os.write(pending_descriptor, payload[written:])
        os.fsync(pending_descriptor)
        os.close(pending_descriptor)
        pending_descriptor = None
        _verify_locked_directory(directory, directory_descriptor, identity=identity)
        os.replace(
            pending_name,
            marker_name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        os.fsync(directory_descriptor)
        return generation
    except GitError:
        if directory_descriptor is not None and lock_acquired:
            _cleanup_pending_marker(
                pending_name, directory_descriptor, ignore_errors=True
            )
        raise
    except OSError as error:
        if directory_descriptor is not None and lock_acquired:
            _cleanup_pending_marker(
                pending_name, directory_descriptor, ignore_errors=True
            )
        detail = error.strerror or str(error)
        raise GitError(
            f"Cannot create Fangorn {identity} generation marker {marker}: {detail}"
        ) from error
    finally:
        primary_active = sys.exc_info()[0] is not None
        if pending_descriptor is not None:
            _close_descriptor(
                pending_descriptor,
                context="generation marker publication",
                ignore_errors=primary_active,
            )
        if directory_descriptor is not None:
            _close_descriptor(
                directory_descriptor,
                context="Git administrative directory",
                ignore_errors=primary_active,
            )


def _cleanup_pending_marker(
    pending_name: str, directory_descriptor: int, *, ignore_errors: bool
) -> None:
    try:
        os.unlink(pending_name, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
    except FileNotFoundError:
        return
    except OSError as error:
        if ignore_errors:
            return
        detail = error.strerror or str(error)
        raise GitError(
            f"Cannot clean up Fangorn generation marker publication: {detail}"
        ) from error


def _verify_locked_directory(
    directory: Path, directory_descriptor: int, *, identity: str
) -> None:
    try:
        opened = os.fstat(directory_descriptor)
        current = directory.stat()
    except OSError as error:
        detail = error.strerror or str(error)
        raise GitError(
            f"Cannot verify locked Git {identity} administrative directory "
            f"{directory}: {detail}"
        ) from error
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise GitError(
            f"Git {identity} administrative directory changed while locked: {directory}"
        )


def _acquire_marker_lock(descriptor: int) -> None:
    deadline = time.monotonic() + MARKER_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError as error:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitError(
                    "Timed out waiting for Fangorn marker lock; retry the command"
                ) from error
            time.sleep(min(MARKER_LOCK_RETRY_SECONDS, remaining))


def _close_descriptor(descriptor: int, *, context: str, ignore_errors: bool) -> None:
    try:
        os.close(descriptor)
    except OSError as error:
        if ignore_errors:
            return
        detail = error.strerror or str(error)
        raise GitError(f"Cannot close {context}: {detail}") from error


def _record(value: bytes) -> str:
    try:
        return value.removesuffix(b"\n").decode("utf-8")
    except UnicodeDecodeError as error:
        raise GitError(
            "Git output is not valid UTF-8; Fangorn v1 requires UTF-8 paths and refs"
        ) from error


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
