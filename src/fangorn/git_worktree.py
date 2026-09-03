from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from types import FrameType
from typing import TYPE_CHECKING, BinaryIO, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit

from fangorn.git import (
    REPOSITORY_LOCAL_ENVIRONMENT,
    GitError,
    GitQuiescenceError,
    WorktreeObservation,
    establish_worktree_generation,
    observe_worktree,
    repository_generation,
    require_supported_git,
)

if TYPE_CHECKING:
    from fangorn.registry import ProcessIdentity

SUPPORTED_URL_SCHEMES = frozenset({"file", "git", "http", "https", "ssh"})
GIT_EFFECT_TIMEOUT_SECONDS = 3600
GIT_CAPTURE_LIMIT = 8 * 1024 * 1024
UNPROVEN_GROUP_TERMINATION = 256


@dataclass(frozen=True)
class RepositorySource:
    normalized: str
    path: Path | None
    clone_url: str | None
    name: str


@dataclass(frozen=True)
class _TargetParent:
    path: Path
    descriptor: int
    device: int
    inode: int


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


def resolve_commit(
    repository: Path,
    ref: str | None,
    *,
    remote: bool = False,
    liveness_fd: int | None = None,
) -> str:
    selected = ref or "HEAD"
    candidates = _remote_ref_candidates(ref) if remote else (selected,)
    error: GitError | None = None
    for candidate in candidates:
        try:
            value = _run_git(
                repository,
                "rev-parse",
                "--verify",
                f"{candidate}^{{commit}}",
                liveness_fd=liveness_fd,
            )
        except GitQuiescenceError:
            raise
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


def read_configuration(
    repository: Path,
    commit: str,
    explicit: Path | None,
    *,
    liveness_fd: int | None = None,
) -> bytes | None:
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
    result = _run_git_process(
        repository, "show", f"{commit}:fangorn.toml", liveness_fd=liveness_fd
    )
    if result.returncode == 0:
        return result.stdout
    if (
        b"does not exist" in result.stderr
        or b"exists on disk, but not in" in result.stderr
    ):
        return None
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
        _verify_bare_repository(cache_path, source.normalized, liveness_fd=liveness_fd)
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
    require_supported_git(cache_path.parent, liveness_fd=liveness_fd)
    prefix = _clone_owner_prefix(owner) if owner is not None else "clone-"
    invocation = Path(tempfile.mkdtemp(prefix=prefix, dir=cache_path.parent))
    clone = invocation / "repository.git"
    primary_error: BaseException | None = None
    try:
        if owner is not None:
            _write_clone_owner(invocation, owner)
        result = _run_git_process(
            cache_path.parent,
            "clone",
            "--bare",
            "--",
            source.clone_url,
            str(clone),
            liveness_fd=liveness_fd,
        )
        if result.returncode != 0:
            raise GitError(_git_error(result))
        _verify_bare_repository(clone, source.normalized, liveness_fd=liveness_fd)
        _refresh_bare_repository(
            clone,
            update_default=refresh_default_head,
            liveness_fd=liveness_fd,
        )
        if repository_generation(clone, create=True) is None:
            raise GitError("Repository cache generation marker is unavailable")
        if preparation_id is not None:
            _write_preparation_receipt(clone, preparation_id, refresh_default_head)
        try:
            os.replace(clone, cache_path)
        except FileExistsError as collision:
            _verify_bare_repository(
                cache_path, source.normalized, liveness_fd=liveness_fd
            )
            if repository_generation(cache_path, create=False) is None:
                raise GitError(
                    "Repository cache generation marker is missing"
                ) from collision
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
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if (
            not isinstance(primary_error, GitQuiescenceError)
            and invocation.parent == cache_path.parent
            and invocation.name.startswith("clone-")
        ):
            try:
                shutil.rmtree(invocation)
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                if primary_error is not None:
                    raise GitError(
                        f"{primary_error}; failed to clean clone staging: "
                        f"{cleanup_error}"
                    ) from primary_error
                raise GitError("Failed to clean clone staging") from cleanup_error


def _cleanup_abandoned_clones(
    parent: Path, owner_status: Callable[[ProcessIdentity], str]
) -> None:
    from fangorn.registry import ProcessIdentity

    resolved_parent = parent.resolve(strict=True)
    for candidate in parent.iterdir():
        if not candidate.name.startswith("clone-"):
            continue
        owner_from_name = _clone_owner_from_name(candidate.name)
        try:
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            resolved = candidate.resolve(strict=True)
            if resolved.parent != resolved_parent:
                continue
            metadata = resolved / "owner.json"
            try:
                metadata_stat = metadata.stat(follow_symlinks=False)
            except FileNotFoundError:
                if owner_from_name is None:
                    continue
                owner = owner_from_name
            else:
                if (
                    not stat.S_ISREG(metadata_stat.st_mode)
                    or metadata_stat.st_size > 4096
                    or metadata.is_symlink()
                ):
                    continue
                value = json.loads(metadata.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    continue
                process_instance_id = value.get("process_instance_id")
                boot_identity = value.get("boot_identity")
                pid = value.get("pid")
                process_start_identity = value.get("process_start_identity")
                if (
                    not isinstance(process_instance_id, str)
                    or not isinstance(boot_identity, str)
                    or type(pid) is not int
                    or not isinstance(process_start_identity, str)
                ):
                    continue
                owner = ProcessIdentity(
                    process_instance_id=process_instance_id,
                    boot_identity=boot_identity,
                    pid=pid,
                    process_start_identity=process_start_identity,
                )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            continue
        if owner_from_name is not None and owner != owner_from_name:
            continue
        if owner_status(owner) == "dead":
            with suppress(FileNotFoundError):
                shutil.rmtree(resolved)


def _clone_owner_prefix(owner: ProcessIdentity) -> str:
    payload = json.dumps(
        [
            owner.process_instance_id,
            owner.boot_identity,
            owner.pid,
            owner.process_start_identity,
        ],
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    return f"clone-{encoded}."


def _clone_owner_from_name(name: str) -> ProcessIdentity | None:
    from fangorn.registry import ProcessIdentity

    encoded, separator, _suffix = name.removeprefix("clone-").partition(".")
    if not separator:
        return None
    try:
        payload = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
        values = json.loads(payload)
        if not isinstance(values, list) or len(values) != 4:
            return None
        process_instance_id, boot_identity, pid, process_start_identity = values
        if (
            not all(
                isinstance(value, str)
                for value in (
                    process_instance_id,
                    boot_identity,
                    process_start_identity,
                )
            )
            or type(pid) is not int
        ):
            return None
        return ProcessIdentity(
            process_instance_id, boot_identity, pid, process_start_identity
        )
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _write_clone_owner(invocation: Path, owner: ProcessIdentity) -> None:
    payload = json.dumps(
        {
            "boot_identity": owner.boot_identity,
            "pid": owner.pid,
            "process_instance_id": owner.process_instance_id,
            "process_start_identity": owner.process_start_identity,
        },
        sort_keys=True,
    ).encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=".owner.", dir=invocation)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, invocation / "owner.json")
        _fsync_directory(invocation, "Clone owner publication")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


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
    primary_error: GitError | None = None
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
        primary_error = GitError("Repository preparation receipt is unavailable")
        raise primary_error from error
    finally:
        cleanup_errors: list[OSError] = []
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as error:
                cleanup_errors.append(error)
        try:
            temporary.unlink(missing_ok=True)
        except OSError as error:
            cleanup_errors.append(error)
        if cleanup_errors:
            detail = "; ".join(str(error) for error in cleanup_errors)
            if primary_error is not None:
                raise GitError(
                    f"{primary_error}; failed to clean receipt staging: {detail}"
                ) from primary_error
            raise GitError("Failed to clean receipt staging") from cleanup_errors[0]


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
    parent = _prepare_target_parent(target.parent)
    try:
        target_kind = _entry_kind(parent.descriptor, target.name)
        if target_kind is not None:
            if target_kind == "symlink" or not reconcile:
                raise GitError(f"Workspace target path already exists: {target}")
            observation = observe_worktree(target, liveness_fd=liveness_fd)
            if observation.git_dir_generation != ownership_token:
                raise GitError("Existing target is not owned by this Workspace create")
            if observation.head != commit or observation.branch != branch:
                raise GitError("Existing target does not match the interrupted create")
            result = observe_worktree(
                target,
                create_repository_generation=True,
                create_worktree_generation=False,
                liveness_fd=liveness_fd,
            )
            _remove_staging_receipt(receipt, ownership_token, parent.descriptor)
            return result

        receipt_kind = _entry_kind(parent.descriptor, receipt.name)
        staging_kind = _entry_kind(parent.descriptor, staging.name)
        if reconcile:
            if receipt_kind is not None:
                _require_staging_receipt(receipt, ownership_token, parent.descriptor)
            elif staging_kind is not None:
                raise GitError(
                    "Workspace staging path already exists without ownership receipt"
                )
            else:
                _create_staging_receipt(receipt, ownership_token, parent.descriptor)
        else:
            if staging_kind is not None:
                raise GitError(
                    "Workspace staging path already exists without ownership receipt"
                )
            _create_staging_receipt(receipt, ownership_token, parent.descriptor)
        _reject_executable_checkout_configuration(repository, liveness_fd=liveness_fd)
        _require_target_parent(parent)
        if staging_kind is not None:
            if staging_kind == "symlink":
                raise GitError("Workspace staging path is unsafe")
            observation = observe_worktree(staging, liveness_fd=liveness_fd)
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
            if branch_exists.returncode == 0 and not reconcile:
                raise GitError("Workspace branch already exists")
            if branch_exists.returncode != 1:
                if branch_exists.returncode != 0:
                    raise GitError(_git_error(branch_exists))
                existing_commit = _run_git(
                    repository,
                    "rev-parse",
                    "--verify",
                    f"refs/heads/{branch}^{{commit}}",
                    liveness_fd=liveness_fd,
                )
                if existing_commit != commit:
                    raise GitError(
                        "Existing Workspace branch does not match interrupted create"
                    )
            _require_target_parent(parent)
            added = _run_git_process(
                _required_git_path(
                    repository,
                    "rev-parse",
                    "--absolute-git-dir",
                    liveness_fd=liveness_fd,
                ),
                "worktree",
                "add",
                "--detach",
                staging.name,
                commit,
                git_dir=True,
                liveness_fd=liveness_fd,
                extra_fds=(parent.descriptor,),
                working_directory_fd=parent.descriptor,
            )
            if added.returncode != 0:
                raise GitError(_git_error(added))
            _require_target_parent(parent)
            observation = observe_worktree(staging, liveness_fd=liveness_fd)
        establish_worktree_generation(observation.git_dir, ownership_token)
        observation = observe_worktree(staging, liveness_fd=liveness_fd)
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
            _require_target_parent(parent)
            staging_descriptor = os.open(
                staging.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent.descriptor,
            )
            try:
                selected = _run_git_process(
                    Path("."),
                    *checkout,
                    liveness_fd=liveness_fd,
                    extra_fds=(staging_descriptor,),
                    working_directory_fd=staging_descriptor,
                )
            finally:
                os.close(staging_descriptor)
            if selected.returncode != 0:
                raise GitError(_git_error(selected))
            _require_target_parent(parent)
            observation = observe_worktree(staging, liveness_fd=liveness_fd)
            if observation.head != commit or observation.branch != branch:
                raise GitError("Staged Worktree does not match the interrupted create")
        _fsync_descriptor(parent.descriptor, "Workspace staging publication")
        _require_target_parent(parent)
        moved = _run_git_process(
            _required_git_path(
                repository,
                "rev-parse",
                "--absolute-git-dir",
                liveness_fd=liveness_fd,
            ),
            "worktree",
            "move",
            staging.name,
            target.name,
            git_dir=True,
            liveness_fd=liveness_fd,
            finish_on_parent_exit=True,
            extra_fds=(parent.descriptor,),
            working_directory_fd=parent.descriptor,
        )
        if moved.returncode != 0:
            raise GitError(_git_error(moved))
        _require_target_parent(parent)
        _fsync_descriptor(parent.descriptor, "Workspace target publication")
        result = observe_worktree(
            target,
            create_repository_generation=True,
            create_worktree_generation=False,
            liveness_fd=liveness_fd,
        )
        _remove_staging_receipt(receipt, ownership_token, parent.descriptor)
        return result
    finally:
        os.close(parent.descriptor)


def _reject_executable_checkout_configuration(
    repository: Path, *, liveness_fd: int | None = None
) -> None:
    configured = _run_git_process(
        repository,
        "config",
        "--includes",
        "--local",
        "--name-only",
        "--list",
        liveness_fd=liveness_fd,
    )
    if configured.returncode != 0:
        raise GitError(_git_error(configured))
    for name in configured.stdout.decode("utf-8", errors="replace").splitlines():
        lowered = name.lower()
        if lowered == "core.fsmonitor" or (
            lowered.startswith("filter.")
            and lowered.rsplit(".", 1)[-1] in {"clean", "smudge", "process"}
        ):
            raise GitError("Repository has executable checkout configuration")


def _create_staging_receipt(
    path: Path, ownership_token: str, parent_descriptor: int
) -> None:
    descriptor: int | None = None
    temporary: Path | None = None
    primary_error: GitError | None = None
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
        os.link(
            temporary,
            path.name,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        _fsync_descriptor(parent_descriptor, "Workspace staging ownership")
    except OSError as error:
        primary_error = GitError("Workspace staging ownership receipt is unavailable")
        raise primary_error from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
                _fsync_directory(path.parent, "Workspace staging ownership")
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                if primary_error is not None:
                    raise GitError(
                        f"{primary_error}; failed to clean receipt staging: "
                        f"{cleanup_error}"
                    ) from primary_error
                raise GitError("Failed to clean receipt staging") from cleanup_error


def _require_staging_receipt(
    path: Path, ownership_token: str, parent_descriptor: int
) -> None:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
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


def _remove_staging_receipt(
    path: Path, ownership_token: str, parent_descriptor: int
) -> None:
    if _entry_kind(parent_descriptor, path.name) is None:
        return
    _require_staging_receipt(path, ownership_token, parent_descriptor)
    try:
        os.unlink(path.name, dir_fd=parent_descriptor)
    except OSError as error:
        raise GitError(
            "Workspace staging ownership receipt cannot be removed"
        ) from error
    _fsync_descriptor(parent_descriptor, "Workspace staging ownership")


def validate_target_path(target: Path) -> None:
    descriptor = _walk_target_parent(target.parent, create=False)
    if descriptor is not None:
        os.close(descriptor)


def _prepare_target_parent(path: Path) -> _TargetParent:
    descriptor = _walk_target_parent(path, create=True)
    if descriptor is None:
        raise GitError("Workspace target parent is unavailable")
    metadata = os.fstat(descriptor)
    return _TargetParent(path, descriptor, metadata.st_dev, metadata.st_ino)


def _walk_target_parent(path: Path, *, create: bool) -> int | None:
    if not path.is_absolute():
        raise GitError("Workspace target path must be absolute")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        _require_safe_directory(os.fstat(descriptor))
        for part in path.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    return None
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=descriptor)
            try:
                _require_safe_directory(os.fstat(child))
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        result = descriptor
        descriptor = -1
        return result
    except OSError as error:
        raise GitError("Workspace target parent is unsafe") from error
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)


def _require_safe_directory(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid not in {0, os.geteuid()}:
        raise GitError("Workspace target parent is unsafe")
    writable = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    if writable and not metadata.st_mode & stat.S_ISVTX:
        raise GitError("Workspace target parent is unsafe")


def _require_target_parent(parent: _TargetParent) -> None:
    try:
        metadata = parent.path.stat(follow_symlinks=False)
    except OSError as error:
        raise GitError("Workspace target parent changed during creation") from error
    if (
        metadata.st_dev != parent.device
        or metadata.st_ino != parent.inode
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise GitError("Workspace target parent changed during creation")


def _entry_kind(parent_descriptor: int, name: str) -> str | None:
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return "symlink" if stat.S_ISLNK(metadata.st_mode) else "present"


def _fsync_descriptor(descriptor: int, label: str) -> None:
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise GitError(f"{label} directory is not durable") from error


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
    liveness_fd: int | None = None,
) -> WorktreeObservation:
    if not target.exists():
        raise GitError(f"Worktree Resource is absent: {target}")
    observation = observe_worktree(target, liveness_fd=liveness_fd)
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


def _verify_bare_repository(
    path: Path, normalized_source: str, *, liveness_fd: int | None = None
) -> None:
    try:
        bare = _run_git(
            path, "rev-parse", "--is-bare-repository", liveness_fd=liveness_fd
        )
    except GitQuiescenceError:
        raise
    except GitError as error:
        raise GitError(
            f"Repository cache entry is not a bare repository: {path}"
        ) from error
    if bare != "true":
        raise GitError(f"Repository cache entry is not a bare repository: {path}")
    origin = _run_git(path, "remote", "get-url", "origin", liveness_fd=liveness_fd)
    try:
        observed = normalize_repository_source(origin).normalized
    except GitError as error:
        raise GitError("Repository cache origin is invalid") from error
    if observed != normalized_source:
        raise GitError("Repository cache entry belongs to another source")


def _required_git_path(
    path: Path, *arguments: str, liveness_fd: int | None = None
) -> Path:
    value = _run_git(path, *arguments, liveness_fd=liveness_fd)
    try:
        return Path(value).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise GitError("Git reported an unavailable repository path") from error


def _run_git(path: Path, *arguments: str, liveness_fd: int | None = None) -> str:
    result = _run_git_process(path, *arguments, liveness_fd=liveness_fd)
    if result.returncode != 0:
        raise GitError(_git_error(result))
    return result.stdout.decode("utf-8", errors="replace").strip()


def _run_git_process(
    path: Path,
    *arguments: str,
    git_dir: bool = False,
    liveness_fd: int | None = None,
    finish_on_parent_exit: bool = False,
    extra_fds: tuple[int, ...] = (),
    working_directory_fd: int | None = None,
    disable_hooks: bool = True,
    isolate_config: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    command = ["git"]
    if git_dir:
        command.append(f"--git-dir={path}")
    else:
        command.extend(("-C", str(path)))
    if disable_hooks:
        command.extend(("-c", "core.hooksPath=/dev/null"))
    command.extend(arguments)
    environment = os.environ.copy()
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"
    for name in REPOSITORY_LOCAL_ENVIRONMENT:
        environment.pop(name, None)
    if isolate_config:
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
    owned_liveness: tuple[int, int] | None = None
    try:
        if liveness_fd is None:
            owned_liveness = os.pipe()
            liveness_fd = owned_liveness[0]
        return _run_supervised_git(
            command,
            environment,
            liveness_fd=liveness_fd,
            finish_on_parent_exit=finish_on_parent_exit,
            extra_fds=extra_fds,
            working_directory_fd=working_directory_fd,
        )
    except FileNotFoundError as error:
        raise GitError("Git executable was not found") from error
    except OSError as error:
        raise GitError(f"Cannot run Git: {error.strerror or error}") from error
    finally:
        if owned_liveness is not None:
            for descriptor in owned_liveness:
                with suppress(OSError):
                    os.close(descriptor)


def _run_supervised_git(
    command: list[str],
    environment: dict[str, str],
    *,
    liveness_fd: int,
    finish_on_parent_exit: bool,
    extra_fds: tuple[int, ...],
    working_directory_fd: int | None,
) -> subprocess.CompletedProcess[bytes]:
    if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
        raise GitError("Cannot supervise Git while SIGCHLD is ignored")
    helper_environment = dict(environment)
    helper_environment.pop("COVERAGE_PROCESS_CONFIG", None)
    helper_environment.pop("COVERAGE_PROCESS_START", None)
    control_read, control_write = os.pipe()
    status_read, status_write = os.pipe()
    completion_read, completion_write = os.pipe()
    anchor_control_read, anchor_control_write = os.pipe()
    anchor: subprocess.Popen[bytes] | None = None
    process: subprocess.Popen[bytes] | None = None
    process_group: int | None = None
    settled = False
    deadline = time.monotonic() + GIT_EFFECT_TIMEOUT_SECONDS + 5
    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            interrupt_command = b"f" if finish_on_parent_exit else b"c"
            with _ignore_repeated_sigint(control_write, interrupt_command):
                try:
                    previous_mask = signal.pthread_sigmask(
                        signal.SIG_BLOCK, {signal.SIGINT}
                    )
                    try:
                        anchor_mask = signal.pthread_sigmask(
                            signal.SIG_BLOCK, {signal.SIGTERM}
                        )
                        try:
                            anchor = subprocess.Popen(  # noqa: S603 -- fixed watchdog argv
                                [
                                    sys.executable,
                                    "-I",
                                    str(Path(__file__).with_name("_git_anchor.py")),
                                    str(anchor_control_read),
                                    str(liveness_fd),
                                    str(GIT_EFFECT_TIMEOUT_SECONDS),
                                ],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                env=helper_environment,
                                pass_fds=(anchor_control_read, liveness_fd),
                                process_group=0,
                            )
                        finally:
                            signal.pthread_sigmask(signal.SIG_SETMASK, anchor_mask)
                        os.close(anchor_control_read)
                        anchor_control_read = -1
                        os.write(anchor_control_write, b"a")
                        _retain_quiescence_guardian(anchor.pid, liveness_fd=liveness_fd)
                        supervisor = [
                            sys.executable,
                            "-I",
                            str(Path(__file__).with_name("_git_supervisor.py")),
                            str(control_read),
                            str(status_write),
                            str(completion_write),
                            str(liveness_fd),
                            str(anchor_control_write),
                            str(anchor.pid),
                            ",".join(str(descriptor) for descriptor in extra_fds),
                            str(
                                working_directory_fd
                                if working_directory_fd is not None
                                else -1
                            ),
                            "finish" if finish_on_parent_exit else "cancel",
                            str(GIT_EFFECT_TIMEOUT_SECONDS),
                            str(GIT_CAPTURE_LIMIT),
                            *command,
                        ]
                        process = subprocess.Popen(  # noqa: S603
                            supervisor,
                            stdout=stdout,
                            stderr=stderr,
                            env=helper_environment,
                            pass_fds=(
                                control_read,
                                status_write,
                                completion_write,
                                liveness_fd,
                                anchor_control_write,
                                *extra_fds,
                            ),
                            process_group=0,
                        )
                        process_group = anchor.pid
                        os.close(control_read)
                        control_read = -1
                        os.close(status_write)
                        status_write = -1
                        os.close(completion_write)
                        completion_write = -1
                        os.close(anchor_control_write)
                        anchor_control_write = -1
                    finally:
                        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
                    child_pid = _read_supervisor_pid(status_read, deadline=deadline)
                    if child_pid is None:
                        raise GitError("Git supervisor failed before child startup")
                    returncode = _read_supervisor_completion(
                        completion_read, deadline=deadline
                    )
                    if returncode is None:
                        _cancel_process_group(process_group)
                        _settle_process(process)
                        _settle_process(anchor)
                        settled = True
                        raise GitError("Git supervisor failed before completion")
                    _settle_process(process)
                    _settle_process(anchor)
                    settled = True
                    if returncode == UNPROVEN_GROUP_TERMINATION:
                        try:
                            _wait_for_process_group_state(
                                process_group,
                                deadline=min(deadline, time.monotonic() + 5),
                            )
                        except GitError as error:
                            raise GitQuiescenceError(
                                "Cannot confirm Git process-group termination"
                            ) from error
                        returncode = 125
                except BaseException:
                    for descriptor in (control_read, status_write, completion_write):
                        if descriptor >= 0:
                            with suppress(OSError):
                                os.close(descriptor)
                    with suppress(OSError):
                        os.write(control_write, interrupt_command)
                    if process is not None and not settled:
                        completion = _read_supervisor_completion(
                            completion_read,
                            deadline=(
                                deadline
                                if finish_on_parent_exit
                                else min(deadline, time.monotonic() + 5)
                            ),
                        )
                        if completion is None and process_group is not None:
                            _cancel_process_group(process_group)
                        _settle_process(process)
                    if anchor is not None and not settled:
                        with suppress(OSError):
                            os.close(anchor_control_write)
                        _cancel_process_group(anchor.pid)
                        _settle_process(anchor)
                    raise
            stdout.seek(0)
            stderr.seek(0)
            return subprocess.CompletedProcess(
                command,
                returncode,
                _read_capture(stdout, GIT_CAPTURE_LIMIT),
                _read_capture(stderr, GIT_CAPTURE_LIMIT),
            )
    finally:
        for descriptor in (
            control_read,
            control_write,
            status_read,
            status_write,
            completion_read,
            completion_write,
            anchor_control_read,
            anchor_control_write,
        ):
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)


@contextmanager
def _ignore_repeated_sigint(
    control_descriptor: int, interrupt_command: bytes
) -> Iterator[None]:
    try:
        previous = signal.getsignal(signal.SIGINT)
        installed = True
        interrupted = False

        def interrupt(signum: int, frame: FrameType | None) -> None:
            nonlocal interrupted
            if interrupted:
                return
            interrupted = True
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            with suppress(OSError):
                os.write(control_descriptor, interrupt_command)
            if callable(previous):
                previous(signum, frame)
            elif previous != signal.SIG_IGN:
                raise KeyboardInterrupt

        signal.signal(signal.SIGINT, interrupt)
    except ValueError:
        installed = False
        previous = signal.SIG_DFL
    try:
        yield
    finally:
        if installed:
            signal.signal(signal.SIGINT, previous)


def _supervisor_pid(value: bytes) -> int | None:
    if not re.fullmatch(rb"[1-9][0-9]{0,9}\n", value):
        return None
    pid = int(value[:-1])
    return pid if pid <= 2_147_483_647 else None


def _read_supervisor_pid(
    descriptor: int, *, deadline: float | None = None
) -> int | None:
    value = _read_pipe_frame(descriptor, 11, deadline=deadline)
    if match := re.fullmatch(rb"!([0-9]{1,3})\n", value):
        number = int(match[1])
        raise OSError(number, os.strerror(number))
    return _supervisor_pid(value)


def _read_supervisor_completion(
    descriptor: int, *, deadline: float | None = None
) -> int | None:
    value = _read_pipe_frame(descriptor, 5, deadline=deadline)
    if not re.fullmatch(rb"-?[0-9]{1,3}\n", value):
        return None
    returncode = int(value[:-1])
    return returncode if -255 <= returncode <= UNPROVEN_GROUP_TERMINATION else None


def _read_pipe_frame(
    descriptor: int, limit: int, *, deadline: float | None = None
) -> bytes:
    value = bytearray()
    while len(value) <= limit:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return b""
            readable, _, _ = select.select((descriptor,), (), (), remaining)
            if not readable:
                return b""
        chunk = os.read(descriptor, limit + 1 - len(value))
        if not chunk:
            break
        value.extend(chunk)
        if b"\n" in value:
            if value.index(b"\n") != len(value) - 1:
                return bytes(value)
            readable, _, _ = select.select((descriptor,), (), (), 0)
            if readable:
                value.extend(os.read(descriptor, 1))
            return bytes(value)
    return bytes(value)


def _settle_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired as error:
        raise GitQuiescenceError("Cannot confirm Git supervisor termination") from error


def _retain_quiescence_guardian(process_group: int, *, liveness_fd: int) -> None:
    ready_read, ready_write = os.pipe()
    blocked = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    try:
        guardian = subprocess.Popen(  # noqa: S603 -- fixed guardian argv
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("_git_guardian.py")),
                str(process_group),
                str(liveness_fd),
                str(ready_write),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={
                name: value
                for name, value in os.environ.items()
                if name not in {"COVERAGE_PROCESS_CONFIG", "COVERAGE_PROCESS_START"}
            },
            pass_fds=(liveness_fd, ready_write),
            process_group=0,
        )
    except BaseException:
        os.close(ready_read)
        raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        os.close(ready_write)
    try:
        ready = _read_pipe_frame(ready_read, 2, deadline=time.monotonic() + 5)
    finally:
        os.close(ready_read)
    if ready != b"r\n":
        _settle_process(guardian)
        raise GitQuiescenceError("Cannot establish Git quiescence guardian")
    with suppress(RuntimeError):
        Thread(target=guardian.wait, daemon=True).start()


def _read_capture(stream: BinaryIO, limit: int) -> bytes:
    data = stream.read(limit + 1)
    if len(data) <= limit:
        return data
    marker = b"\n[diagnostic output truncated]\n"
    return (data[: max(0, limit - len(marker))] + marker)[:limit]


def _cancel_process_group(process_group: int, *, deadline: float | None = None) -> None:
    if deadline is None:
        deadline = time.monotonic() + 5
    with suppress(ProcessLookupError):
        os.killpg(process_group, signal.SIGTERM)
    try:
        _wait_for_process_group_state(
            process_group, deadline=min(deadline, time.monotonic() + 2)
        )
        return
    except GitQuiescenceError:
        pass
    with suppress(ProcessLookupError):
        os.killpg(process_group, signal.SIGKILL)
    _wait_for_process_group_state(process_group, deadline=deadline)


def _wait_for_process_group_state(process_group: int, *, deadline: float) -> bool:
    while time.monotonic() < deadline:
        try:
            if not _process_group_running(
                process_group, timeout=max(0.01, min(1, deadline - time.monotonic()))
            ):
                return False
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    raise GitQuiescenceError("Cannot confirm Git process-group termination")


def _process_group_running(process_group: int, *, timeout: float = 1) -> bool:
    deadline = time.monotonic() + timeout
    proc = Path("/proc")
    if proc.is_dir():
        parsed = False
        complete = True
        for entry in proc.iterdir():
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("/proc process-group scan", timeout)
            if not entry.name.isdigit():
                continue
            try:
                fields = (
                    (entry / "stat")
                    .read_text(encoding="ascii")
                    .rpartition(")")[2]
                    .split()
                )
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired("/proc process-group scan", timeout)
                parsed = True
                if int(fields[2]) == process_group and fields[0] != "Z":
                    return True
            except FileNotFoundError:
                pass
            except (IndexError, OSError, ValueError):
                complete = False
        if parsed and complete:
            return False
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired("process-group probe", timeout)
    result = subprocess.run(
        ["/bin/ps", "-axo", "pgid=,state="],
        check=True,
        capture_output=True,
        env={"LANG": "C", "PATH": "/usr/bin:/bin"},
        start_new_session=True,
        text=True,
        timeout=remaining,
    )
    running = False
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[0].isdigit():
            raise OSError("Cannot parse process-group state")
        if int(fields[0]) == process_group and not fields[1].startswith("Z"):
            running = True
    return running


def _git_error(result: subprocess.CompletedProcess[bytes]) -> str:
    detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
    return detail or f"Git command failed with exit status {result.returncode}"
