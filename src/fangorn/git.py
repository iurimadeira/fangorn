from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """Git could not prove a requested worktree identity."""


@dataclass(frozen=True)
class WorktreeObservation:
    repository_common_dir: Path
    git_dir: Path
    path: Path
    branch: str | None
    head: str


def _run_git(path: Path, *arguments: str, allow_failure: bool = False) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise GitError("Git executable was not found") from error

    if result.returncode != 0:
        if allow_failure:
            return None
        detail = result.stderr.strip() or result.stdout.strip() or "Git command failed"
        raise GitError(detail)
    return result.stdout.strip()


def observe_worktree(path: Path) -> WorktreeObservation:
    try:
        requested_path = path.resolve(strict=True)
    except OSError as error:
        raise GitError(f"Path does not exist: {path}") from error
    if not requested_path.is_dir():
        raise GitError(f"Path is not a directory: {requested_path}")

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
    branch = _run_git(
        requested_path,
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        allow_failure=True,
    )
    head = _run_git(requested_path, "rev-parse", "HEAD")
    if head is None:
        raise GitError("Git did not report HEAD")

    return WorktreeObservation(
        repository_common_dir=common_dir,
        git_dir=git_dir,
        path=worktree_path,
        branch=branch,
        head=head,
    )


def _required_path(value: str | None, label: str) -> Path:
    if not value:
        raise GitError(f"Git did not report {label}")
    try:
        return Path(value).resolve(strict=True)
    except OSError as error:
        raise GitError(f"Git reported an invalid {label}: {value}") from error
