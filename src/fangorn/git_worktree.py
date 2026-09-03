from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import SplitResult, urlsplit, urlunsplit

from fangorn.git import (
    GitError,
    WorktreeObservation,
    establish_worktree_generation,
    observe_worktree,
)

SUPPORTED_URL_SCHEMES = frozenset({"file", "git", "http", "https", "ssh"})


@dataclass(frozen=True)
class RepositorySource:
    normalized: str
    path: Path | None
    clone_url: str | None
    name: str


def normalize_repository_source(value: str) -> RepositorySource:
    parsed = urlsplit(value)
    if parsed.scheme:
        if parsed.scheme.lower() not in SUPPORTED_URL_SCHEMES:
            raise GitError(f"Unsupported repository URL scheme: {parsed.scheme}")
        if parsed.username is not None or parsed.password is not None:
            raise GitError("Repository URLs must not contain credentials")
        if parsed.query or parsed.fragment:
            raise GitError("Repository URLs must not contain query or fragment data")
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname.lower() if parsed.hostname else ""
        port = f":{parsed.port}" if parsed.port is not None else ""
        netloc = f"{hostname}{port}"
        path = re.sub("/+", "/", parsed.path).rstrip("/")
        normalized = urlunsplit(SplitResult(scheme, netloc, path, "", ""))
        name = Path(path).name.removesuffix(".git") or "repository"
        return RepositorySource(normalized, None, normalized, name)

    requested = Path(value)
    try:
        resolved = requested.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise GitError(f"Repository path is unavailable: {requested}") from error
    common = _required_git_path(
        resolved, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    if common.name == ".git":
        name = common.parent.name
    else:
        name = common.name.removesuffix(".git")
    return RepositorySource(str(common), common, None, name or "repository")


def resolve_commit(repository: Path, ref: str | None) -> str:
    selected = ref or "HEAD"
    value = _run_git(repository, "rev-parse", "--verify", f"{selected}^{{commit}}")
    if not re.fullmatch(r"[0-9a-f]{40,64}", value):
        raise GitError(f"Git returned an invalid commit for {selected}")
    return value


def read_configuration(repository: Path, commit: str, explicit: Path | None) -> bytes:
    if explicit is not None:
        try:
            resolved = explicit.resolve(strict=True)
            if resolved.is_symlink() or not resolved.is_file():
                raise GitError("Configuration must be a regular non-symlink file")
            return resolved.read_bytes()
        except GitError:
            raise
        except (OSError, RuntimeError) as error:
            raise GitError(f"Configuration is unavailable: {explicit}") from error
    result = _run_git_process(repository, "show", f"{commit}:fangorn.toml")
    if result.returncode == 0:
        return result.stdout
    if (
        b"does not exist" in result.stderr
        or b"exists on disk, but not in" in result.stderr
    ):
        return b""
    raise GitError(_git_error(result))


def materialize_cache(source: RepositorySource, cache_path: Path) -> Path:
    if source.clone_url is None:
        if source.path is None:
            raise GitError("Local repository path is unavailable")
        return source.path
    if cache_path.exists():
        _verify_bare_repository(cache_path, source.normalized)
        return cache_path
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    invocation = Path(tempfile.mkdtemp(prefix="clone-", dir=cache_path.parent))
    clone = invocation / "repository.git"
    try:
        result = _run_git_process(
            cache_path.parent,
            "clone",
            "--bare",
            "--no-tags",
            "--",
            source.clone_url,
            str(clone),
            use_c=True,
        )
        if result.returncode != 0:
            raise GitError(_git_error(result))
        _verify_bare_repository(clone, source.normalized)
        try:
            os.replace(clone, cache_path)
        except FileExistsError:
            _verify_bare_repository(cache_path, source.normalized)
        return cache_path
    finally:
        if invocation.parent == cache_path.parent and invocation.name.startswith(
            "clone-"
        ):
            shutil.rmtree(invocation, ignore_errors=True)


def create_worktree(
    repository: Path,
    *,
    target: Path,
    branch: str,
    commit: str,
    ownership_token: str,
    reconcile: bool,
) -> WorktreeObservation:
    if target.exists():
        if not reconcile:
            raise GitError(f"Workspace target path already exists: {target}")
        observation = observe_worktree(target)
        if observation.head != commit or observation.branch != branch:
            raise GitError("Existing target does not match the interrupted create")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        result = _run_git_process(
            repository,
            "worktree",
            "add",
            "-b",
            branch,
            str(target),
            commit,
        )
        if result.returncode != 0:
            raise GitError(_git_error(result))
        observation = observe_worktree(target)
    establish_worktree_generation(observation.git_dir, ownership_token)
    return observe_worktree(
        target,
        create_repository_generation=True,
        create_worktree_generation=False,
    )


def inspect_owned_worktree(
    target: Path,
    *,
    expected_commit: str,
    expected_branch: str,
    ownership_token: str | None = None,
) -> WorktreeObservation:
    if not target.exists():
        raise GitError(f"Worktree Resource is absent: {target}")
    observation = observe_worktree(target)
    if observation.head != expected_commit or observation.branch != expected_branch:
        raise GitError("Worktree Resource does not match its immutable definition")
    if (
        ownership_token is not None
        and observation.git_dir_generation != ownership_token
    ):
        raise GitError("Worktree Resource ownership token does not match")
    return observation


def _verify_bare_repository(path: Path, normalized_source: str) -> None:
    if _run_git(path, "rev-parse", "--is-bare-repository") != "true":
        raise GitError(f"Repository cache entry is not a bare repository: {path}")
    origin = _run_git(path, "remote", "get-url", "origin")
    try:
        observed = normalize_repository_source(origin).normalized
    except GitError as error:
        raise GitError("Repository cache origin is invalid") from error
    if observed != normalized_source:
        raise GitError("Repository cache entry belongs to another source")


def _required_git_path(path: Path, *arguments: str) -> Path:
    value = _run_git(path, *arguments)
    try:
        return Path(value).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise GitError("Git reported an unavailable repository path") from error


def _run_git(path: Path, *arguments: str) -> str:
    result = _run_git_process(path, *arguments)
    if result.returncode != 0:
        raise GitError(_git_error(result))
    return result.stdout.decode("utf-8", errors="replace").strip()


def _run_git_process(
    path: Path, *arguments: str, use_c: bool = False
) -> subprocess.CompletedProcess[bytes]:
    command = ["git"]
    if use_c:
        command.extend(("-C", str(path)))
    else:
        location = (
            ("--git-dir", str(path))
            if path.is_dir() and path.name.endswith(".git")
            else ("-C", str(path))
        )
        command.extend(location)
    command.extend(arguments)
    try:
        return subprocess.run(command, check=False, capture_output=True)  # noqa: S603
    except FileNotFoundError as error:
        raise GitError("Git executable was not found") from error
    except OSError as error:
        raise GitError(f"Cannot run Git: {error}") from error


def _git_error(result: subprocess.CompletedProcess[bytes]) -> str:
    detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
    return detail or f"Git command failed with exit status {result.returncode}"
