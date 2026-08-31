from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class GitError(RuntimeError):
    """Git could not prove a requested worktree identity."""


@dataclass(frozen=True)
class WorktreeObservation:
    repository_common_dir: Path
    git_dir: Path
    git_dir_generation: str
    path: Path
    branch: str | None
    head: str
    observed_at: str


@dataclass(frozen=True)
class _Snapshot:
    repository_common_dir: Path
    git_dir: Path
    git_dir_generation: str
    path: Path
    branch: str | None
    head: str


REPOSITORY_LOCAL_ENVIRONMENT = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CEILING_DIRECTORIES",
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
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


def _run_git(
    path: Path,
    *arguments: str,
    allowed_exit_codes: frozenset[int] = frozenset(),
) -> str | None:
    environment = os.environ.copy()
    for name in REPOSITORY_LOCAL_ENVIRONMENT:
        environment.pop(name, None)
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *arguments],
            check=False,
            capture_output=True,
            env=environment,
        )
    except FileNotFoundError as error:
        raise GitError("Git executable was not found") from error
    except OSError as error:
        detail = error.strerror or str(error)
        raise GitError(f"Cannot run Git: {detail}") from error

    if result.returncode in allowed_exit_codes:
        return None
    if result.returncode != 0:
        detail = _record(result.stderr) or _record(result.stdout)
        if not detail:
            detail = f"Git command failed with exit status {result.returncode}"
        raise GitError(detail)
    return _record(result.stdout)


def observe_worktree(path: Path) -> WorktreeObservation:
    requested_path = _resolve_requested_path(path)
    last_error: GitError | None = None
    saw_mismatch = False

    for _ in range(OBSERVATION_ATTEMPTS):
        observed_at = _timestamp()
        try:
            first = _capture_snapshot(requested_path)
            second = _capture_snapshot(requested_path)
        except GitError as error:
            last_error = error
            continue
        if first == second:
            return WorktreeObservation(
                repository_common_dir=second.repository_common_dir,
                git_dir=second.git_dir,
                git_dir_generation=second.git_dir_generation,
                path=second.path,
                branch=second.branch,
                head=second.head,
                observed_at=observed_at,
            )
        saw_mismatch = True

    if saw_mismatch:
        raise GitError("Git worktree changed during observation; retry the command")
    if last_error is not None:
        raise last_error
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


def _capture_snapshot(requested_path: Path) -> _Snapshot:
    inside = _run_git(requested_path, "rev-parse", "--is-inside-work-tree")
    if inside != "true":
        raise GitError(f"Path is not inside a Git worktree: {requested_path}")

    worktree_path = _required_path(
        _run_git(requested_path, "rev-parse", "--show-toplevel"),
        "worktree path",
    )
    common_dir = _required_path(
        _run_git(
            requested_path,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ),
        "Git common directory",
    )
    git_dir = _required_path(
        _run_git(
            requested_path,
            "rev-parse",
            "--path-format=absolute",
            "--git-dir",
        ),
        "Git administrative directory",
    )
    generation_before = _directory_generation(git_dir)
    branch = _run_git(
        requested_path,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        allowed_exit_codes=frozenset({1}),
    )
    head = _run_git(requested_path, "rev-parse", "HEAD")
    if head is None:
        raise GitError("Git did not report HEAD")
    generation_after = _directory_generation(git_dir)
    if generation_before != generation_after:
        raise GitError("Git administrative directory changed during observation")

    return _Snapshot(
        repository_common_dir=common_dir,
        git_dir=git_dir,
        git_dir_generation=generation_after,
        path=worktree_path,
        branch=branch,
        head=head,
    )


def _required_path(value: str | None, label: str) -> Path:
    if value is None or value == "":
        raise GitError(f"Git did not report {label}")
    try:
        return Path(value).resolve(strict=True)
    except OSError as error:
        raise GitError(f"Git reported an invalid {label}: {value}") from error


def _directory_generation(path: Path) -> str:
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


def _record(value: bytes) -> str:
    try:
        return value.removesuffix(b"\n").decode("utf-8")
    except UnicodeDecodeError as error:
        raise GitError(
            "Git output is not valid UTF-8; Fangorn v1 requires UTF-8 paths and refs"
        ) from error


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
