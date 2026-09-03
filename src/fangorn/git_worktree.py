from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit

from fangorn.git import (
    REPOSITORY_LOCAL_ENVIRONMENT,
    GitError,
    WorktreeObservation,
    establish_worktree_generation,
    observe_worktree,
    repository_generation,
    require_supported_git,
)

if TYPE_CHECKING:
    from fangorn.registry import ProcessIdentity

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
        if ":" in hostname:
            hostname = f"[{hostname}]"
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
    require_supported_git(resolved)
    common = _required_git_path(
        resolved, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    if common.name == ".git":
        name = common.parent.name
    else:
        name = common.name.removesuffix(".git")
    return RepositorySource(str(common), resolved, None, name or "repository")


def resolve_commit(repository: Path, ref: str | None, *, remote: bool = False) -> str:
    selected = ref or "HEAD"
    candidates = _remote_ref_candidates(ref) if remote else (selected,)
    error: GitError | None = None
    for candidate in candidates:
        try:
            value = _run_git(
                repository, "rev-parse", "--verify", f"{candidate}^{{commit}}"
            )
        except GitError as candidate_error:
            error = candidate_error
            continue
        if not re.fullmatch(r"[0-9a-f]{40,64}", value):
            raise GitError(f"Git returned an invalid commit for {candidate}")
        return value
    raise GitError(f"Cannot resolve Git base {selected}: {error}")


def _remote_ref_candidates(ref: str | None) -> tuple[str, ...]:
    if ref is None or ref == "HEAD":
        return ("refs/remotes/origin/HEAD",)
    if ref.startswith("refs/heads/"):
        return (f"refs/remotes/origin/{ref.removeprefix('refs/heads/')}",)
    if ref.startswith("origin/"):
        return (f"refs/remotes/origin/{ref.removeprefix('origin/')}",)
    if ref.startswith("refs/") or re.fullmatch(r"[0-9a-f]{40,64}", ref):
        return (ref,)
    return (f"refs/remotes/origin/{ref}", f"refs/tags/{ref}")


def validate_branch_name(branch: str) -> None:
    result = _run_git_process(Path.cwd(), "check-ref-format", "--branch", branch)
    if result.returncode != 0:
        raise GitError("Workspace branch is invalid")


def read_configuration(repository: Path, commit: str, explicit: Path | None) -> bytes:
    if explicit is not None:
        try:
            descriptor = os.open(explicit, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise GitError("Configuration must be a regular non-symlink file")
                with os.fdopen(descriptor, "rb") as opened:
                    descriptor = -1
                    return opened.read()
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        except GitError:
            raise
        except (OSError, RuntimeError) as error:
            if isinstance(error, OSError) and error.errno == errno.ELOOP:
                raise GitError(
                    "Configuration must be a regular non-symlink file"
                ) from error
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


def materialize_cache(
    source: RepositorySource,
    cache_path: Path,
    *,
    owner: ProcessIdentity | None = None,
    owner_status: Callable[[ProcessIdentity], str] | None = None,
    refresh: bool = True,
    refresh_default_head: bool = True,
    liveness_fd: int | None = None,
    preparation_id: str | None = None,
) -> Path:
    if source.clone_url is None:
        if source.path is None:
            raise GitError("Local repository path is unavailable")
        return source.path
    _mkdir_durable(cache_path.parent)
    if owner is not None and owner_status is not None:
        _cleanup_abandoned_clones(cache_path.parent, owner_status)
    if cache_path.exists():
        _verify_bare_repository(cache_path, source.normalized)
        if repository_generation(cache_path, create=False) is None:
            raise GitError("Repository cache generation marker is missing")
        prepared = preparation_id is not None and _preparation_receipt_matches(
            cache_path, preparation_id, refresh_default_head
        )
        if refresh and not prepared:
            _refresh_bare_repository(
                cache_path,
                update_default=refresh_default_head,
                liveness_fd=liveness_fd,
            )
            if preparation_id is not None:
                _write_preparation_receipt(
                    cache_path, preparation_id, refresh_default_head
                )
        _fsync_directory(cache_path.parent, "Repository cache publication")
        return cache_path
    require_supported_git(cache_path.parent)
    prefix = f"clone-{owner.process_instance_id}-" if owner is not None else "clone-"
    invocation = Path(tempfile.mkdtemp(prefix=prefix, dir=cache_path.parent))
    clone = invocation / "repository.git"
    try:
        if owner is not None:
            (invocation / "owner.json").write_text(
                json.dumps(
                    {
                        "boot_identity": owner.boot_identity,
                        "pid": owner.pid,
                        "process_instance_id": owner.process_instance_id,
                        "process_start_identity": owner.process_start_identity,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        result = _run_git_process(
            cache_path.parent,
            "clone",
            "--bare",
            "--",
            source.clone_url,
            str(clone),
            use_c=True,
            liveness_fd=liveness_fd,
        )
        if result.returncode != 0:
            raise GitError(_git_error(result))
        _verify_bare_repository(clone, source.normalized)
        _refresh_bare_repository(
            clone,
            update_default=refresh_default_head,
            liveness_fd=liveness_fd,
        )
        if repository_generation(clone, create=True) is None:
            raise GitError("Repository cache generation marker is unavailable")
        try:
            os.replace(clone, cache_path)
        except FileExistsError:
            _verify_bare_repository(cache_path, source.normalized)
        _fsync_directory(cache_path.parent, "Repository cache publication")
        if preparation_id is not None:
            _write_preparation_receipt(cache_path, preparation_id, refresh_default_head)
        return cache_path
    finally:
        if invocation.parent == cache_path.parent and invocation.name.startswith(
            "clone-"
        ):
            shutil.rmtree(invocation, ignore_errors=True)


def _cleanup_abandoned_clones(
    parent: Path, owner_status: Callable[[ProcessIdentity], str]
) -> None:
    from fangorn.registry import ProcessIdentity

    resolved_parent = parent.resolve(strict=True)
    for candidate in parent.iterdir():
        if not candidate.name.startswith("clone-"):
            continue
        try:
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            resolved = candidate.resolve(strict=True)
            if resolved.parent != resolved_parent:
                continue
            metadata = resolved / "owner.json"
            metadata_stat = metadata.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata_stat.st_mode)
                or metadata_stat.st_size > 4096
                or metadata.is_symlink()
            ):
                continue
            value = json.loads(metadata.read_text(encoding="utf-8"))
            owner = ProcessIdentity(
                process_instance_id=str(value["process_instance_id"]),
                boot_identity=str(value["boot_identity"]),
                pid=int(value["pid"]),
                process_start_identity=str(value["process_start_identity"]),
            )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            continue
        if owner_status(owner) == "dead":
            shutil.rmtree(resolved)


def _refresh_bare_repository(
    path: Path, *, update_default: bool, liveness_fd: int | None
) -> None:
    fetched = _run_git_process(
        path,
        "fetch",
        "--prune",
        "--prune-tags",
        "origin",
        "+refs/heads/*:refs/remotes/origin/*",
        "+refs/tags/*:refs/tags/*",
        liveness_fd=liveness_fd,
    )
    if fetched.returncode != 0:
        raise GitError(_git_error(fetched))
    if update_default:
        head = _run_git_process(
            path,
            "remote",
            "set-head",
            "origin",
            "--auto",
            liveness_fd=liveness_fd,
        )
        if head.returncode != 0:
            raise GitError(_git_error(head))


def _preparation_receipt_path(path: Path, preparation_id: str) -> Path:
    digest = hashlib.sha256(preparation_id.encode()).hexdigest()
    return path / f"fangorn-preparation-{digest}.json"


def _preparation_receipt_matches(
    path: Path, preparation_id: str, update_default: bool
) -> bool:
    receipt = _preparation_receipt_path(path, preparation_id)
    try:
        loaded: object = json.loads(receipt.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(loaded, dict):
        return False
    value = cast(dict[str, object], loaded)
    return value == {
        "repository_generation": repository_generation(path, create=False),
        "update_default": update_default,
    }


def _write_preparation_receipt(
    path: Path, preparation_id: str, update_default: bool
) -> None:
    generation = repository_generation(path, create=False)
    if generation is None:
        raise GitError("Repository cache generation marker is unavailable")
    receipt = _preparation_receipt_path(path, preparation_id)
    payload = json.dumps(
        {"repository_generation": generation, "update_default": update_default},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{receipt.name}.", dir=path)
    temporary = Path(temporary_name)
    try:
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("short write")
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, receipt)
        _fsync_directory(path, "Repository preparation receipt")
    except OSError as error:
        raise GitError("Repository preparation receipt is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def create_worktree(
    repository: Path,
    *,
    target: Path,
    branch: str,
    commit: str,
    ownership_token: str,
    reconcile: bool,
    liveness_fd: int | None = None,
) -> WorktreeObservation:
    staging = target.parent / f".fangorn-{ownership_token}"
    receipt = target.parent / f".fangorn-{ownership_token}.intent"
    if target.exists():
        if not reconcile:
            raise GitError(f"Workspace target path already exists: {target}")
        observation = observe_worktree(target)
        if observation.git_dir_generation != ownership_token:
            raise GitError("Existing target is not owned by this Workspace create")
        if observation.head != commit or observation.branch != branch:
            raise GitError("Existing target does not match the interrupted create")
        result = observe_worktree(
            target,
            create_repository_generation=True,
            create_worktree_generation=False,
        )
        _remove_staging_receipt(receipt, ownership_token)
        return result

    _mkdir_durable(target.parent)
    if reconcile:
        if receipt.exists():
            _require_staging_receipt(receipt, ownership_token)
        elif staging.exists():
            raise GitError(
                "Workspace staging path already exists without ownership receipt"
            )
        else:
            _create_staging_receipt(receipt, ownership_token)
    else:
        if staging.exists():
            raise GitError(
                "Workspace staging path already exists without ownership receipt"
            )
        _create_staging_receipt(receipt, ownership_token)
    if staging.exists():
        observation = observe_worktree(staging)
        if observation.git_dir_generation not in {None, ownership_token}:
            raise GitError("Staged Worktree belongs to another Workspace create")
        if observation.head != commit or observation.branch not in {None, branch}:
            raise GitError("Staged Worktree does not match the interrupted create")
    else:
        branch_exists = _run_git_process(
            repository,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            liveness_fd=liveness_fd,
        )
        if branch_exists.returncode == 0:
            raise GitError("Workspace branch already exists")
        if branch_exists.returncode != 1:
            raise GitError(_git_error(branch_exists))
        added = _run_git_process(
            repository,
            "worktree",
            "add",
            "--detach",
            str(staging),
            commit,
            liveness_fd=liveness_fd,
        )
        if added.returncode != 0:
            raise GitError(_git_error(added))
        observation = observe_worktree(staging)
    establish_worktree_generation(observation.git_dir, ownership_token)
    observation = observe_worktree(staging)
    if observation.head != commit or observation.branch != branch:
        branch_exists = _run_git_process(
            repository,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            liveness_fd=liveness_fd,
        )
        checkout = (
            ("checkout", branch)
            if branch_exists.returncode == 0
            else ("checkout", "-b", branch, commit)
        )
        selected = _run_git_process(staging, *checkout, liveness_fd=liveness_fd)
        if selected.returncode != 0:
            raise GitError(_git_error(selected))
        observation = observe_worktree(staging)
        if observation.head != commit or observation.branch != branch:
            raise GitError("Staged Worktree does not match the interrupted create")
    _fsync_directory(target.parent, "Workspace staging publication")
    moved = _run_git_process(
        repository,
        "worktree",
        "move",
        str(staging),
        str(target),
        liveness_fd=liveness_fd,
    )
    if moved.returncode != 0:
        raise GitError(_git_error(moved))
    _fsync_directory(target.parent, "Workspace target publication")
    result = observe_worktree(
        target,
        create_repository_generation=True,
        create_worktree_generation=False,
    )
    _remove_staging_receipt(receipt, ownership_token)
    return result


def _create_staging_receipt(path: Path, ownership_token: str) -> None:
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        temporary = Path(temporary_name)
        payload = ownership_token.encode()
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError("short write")
            written += count
        os.fsync(descriptor)
        os.link(temporary, path, follow_symlinks=False)
        _fsync_directory(path.parent, "Workspace staging ownership")
    except OSError as error:
        raise GitError("Workspace staging ownership receipt is unavailable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
                _fsync_directory(path.parent, "Workspace staging ownership")
            except OSError:
                pass


def _require_staging_receipt(path: Path, ownership_token: str) -> None:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            value = os.read(descriptor, 129)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise GitError("Workspace staging ownership receipt is unavailable") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or value != ownership_token.encode()
    ):
        raise GitError("Workspace staging ownership receipt is invalid")


def _remove_staging_receipt(path: Path, ownership_token: str) -> None:
    if not path.exists():
        return
    _require_staging_receipt(path, ownership_token)
    try:
        path.unlink()
    except OSError as error:
        raise GitError(
            "Workspace staging ownership receipt cannot be removed"
        ) from error
    _fsync_directory(path.parent, "Workspace staging ownership")


def _fsync_directory(path: Path, label: str) -> None:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise GitError(f"{label} directory is not durable") from error


def _mkdir_durable(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    path.mkdir(parents=True, exist_ok=True)
    for directory in reversed(missing):
        _fsync_directory(directory.parent, "Created directory")


def inspect_owned_worktree(
    target: Path,
    *,
    expected_commit: str | None,
    expected_branch: str | None,
    ownership_token: str | None = None,
) -> WorktreeObservation:
    if not target.exists():
        raise GitError(f"Worktree Resource is absent: {target}")
    observation = observe_worktree(target)
    if expected_commit is not None and observation.head != expected_commit:
        raise GitError("Worktree Resource does not match its immutable definition")
    if expected_branch is not None and observation.branch != expected_branch:
        raise GitError("Worktree Resource does not match its immutable definition")
    if (
        ownership_token is not None
        and observation.git_dir_generation != ownership_token
    ):
        raise GitError("Worktree Resource ownership token does not match")
    return observation


def _verify_bare_repository(path: Path, normalized_source: str) -> None:
    try:
        bare = _run_git(path, "rev-parse", "--is-bare-repository")
    except GitError as error:
        raise GitError(
            f"Repository cache entry is not a bare repository: {path}"
        ) from error
    if bare != "true":
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
    path: Path,
    *arguments: str,
    use_c: bool = False,
    liveness_fd: int | None = None,
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
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"
    for name in REPOSITORY_LOCAL_ENVIRONMENT:
        environment.pop(name, None)
    control_read: int | None = None
    control_write: int | None = None
    supervised = command
    inherited: tuple[int, ...] = ()
    if liveness_fd is not None:
        control_read, control_write = os.pipe()
        supervised = [
            sys.executable,
            "-m",
            "fangorn._git_supervisor",
            str(control_read),
            *command,
        ]
        inherited = (control_read, liveness_fd)
    try:
        if liveness_fd is None:
            return subprocess.run(  # noqa: S603
                command,
                check=False,
                capture_output=True,
                env=environment,
            )
        process = subprocess.Popen(  # noqa: S603
            supervised,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            pass_fds=inherited,
            start_new_session=liveness_fd is not None,
        )
        try:
            stdout, stderr = process.communicate()
        except BaseException:
            if control_write is not None:
                os.close(control_write)
                control_write = None
            process.communicate()
            raise
        return subprocess.CompletedProcess(
            supervised, process.returncode, stdout, stderr
        )
    except FileNotFoundError as error:
        raise GitError("Git executable was not found") from error
    except OSError as error:
        raise GitError(f"Cannot run Git: {error}") from error
    finally:
        if control_read is not None:
            os.close(control_read)
        if control_write is not None:
            os.close(control_write)


def _git_error(result: subprocess.CompletedProcess[bytes]) -> str:
    detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
    return detail or f"Git command failed with exit status {result.returncode}"
