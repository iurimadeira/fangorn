from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tomllib
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import cast
from uuid import uuid4

from fangorn.git import GitError
from fangorn.git_worktree import _open_configuration_file
from fangorn.registry import RegistryError, _open_registry_state_directory

SCRIPT_LIMIT = 1024 * 1024
ScriptReader = Callable[[str], tuple[bytes, int]]


def parse_configuration(content: bytes | None) -> dict[str, object]:
    if content is None:
        return {"schema_version": 1}
    try:
        value = tomllib.loads(content.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("fangorn.toml must be valid UTF-8 TOML") from error
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("fangorn.toml values must be finite JSON values") from error
    unknown = value.keys() - {"schema_version", "services"}
    if unknown:
        raise ValueError(
            f"fangorn.toml contains unsupported top-level key: {min(unknown)}"
        )
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("fangorn.toml requires schema_version = 1")
    services = value.get("services", {})
    if not isinstance(services, dict):
        raise ValueError("fangorn.toml services must be a table")
    for name, service in services.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or name == "worktree":
            raise ValueError("Service name is invalid or reserved")
        if not isinstance(service, dict) or not isinstance(service.get("adapter"), str):
            raise ValueError("Each Service requires an adapter ID")
        major = service.get("adapter_api_major", 1)
        if type(major) is not int or major != 1:
            raise ValueError("Service requires compatible adapter_api_major = 1")
        if "external_reference" in service and not isinstance(
            service["external_reference"], str
        ):
            raise ValueError("Service external_reference must be a string")
        scripts = service.get("scripts", [])
        if not isinstance(scripts, list) or any(
            not isinstance(name, str) or not name or "\0" in name for name in scripts
        ):
            raise ValueError("Service scripts must be an array of nonempty path names")
        if service["adapter"] == "fangorn.command":
            _validate_command(service)
    return cast(dict[str, object], value)


def _validate_command(service: dict[str, object]) -> None:
    if service.keys() - {
        "adapter",
        "create",
        "inspect",
        "start",
        "stop",
        "delete",
        "cwd",
        "timeout",
        "environment",
        "scripts",
        "adapter_api_major",
        "external_reference",
    }:
        raise ValueError("Command Service contains unsupported keys")
    for action in ("create", "inspect", "start", "stop", "delete"):
        argv = service.get(action)
        if action == "create" and argv is None:
            continue
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in argv)
        ):
            raise ValueError(f"Command Service {action} requires a nonempty argv array")
    timeout = service.get("timeout", 60)
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise ValueError("Command timeout must be an integer from 1 to 3600 seconds")
    cwd = service.get("cwd", ".")
    if not isinstance(cwd, str) or not cwd or "\0" in cwd:
        raise ValueError("Command cwd must be a path string")
    for field in ("environment", "scripts"):
        values = service.get(field, [])
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ValueError(f"Command {field} must be an array of names")
        if field == "environment" and any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) for v in values
        ):
            raise ValueError(
                "Environment entries must be reference names, never values"
            )


def _script_names(value: Mapping[str, object]) -> tuple[str, ...]:
    names: dict[str, None] = {}
    services = cast(Mapping[str, Mapping[str, object]], value.get("services", {}))
    for service in services.values():
        for name in cast(list[str], service.get("scripts", [])):
            names[name] = None
        if service.get("adapter") == "fangorn.command":
            for action in ("create", "inspect", "start", "stop", "delete"):
                argv = cast(list[str], service.get(action, []))
                if argv and "/" in argv[0] and not Path(argv[0]).is_absolute():
                    names[argv[0]] = None
    return tuple(names)


@dataclass(frozen=True)
class ScriptSnapshot:
    name: str
    content: bytes
    mode: int


@dataclass(frozen=True)
class ConfigurationSnapshot:
    content: bytes
    value: Mapping[str, object]
    digest: str
    scripts: tuple[ScriptSnapshot, ...]

    def serialize(self) -> dict[str, object]:
        return {
            "content": base64.b64encode(self.content).decode("ascii"),
            "scripts": [
                {
                    "name": s.name,
                    "content": base64.b64encode(s.content).decode("ascii"),
                    "mode": s.mode,
                }
                for s in self.scripts
            ],
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> ConfigurationSnapshot:
        content = base64.b64decode(str(record["content"]), validate=True)
        scripts = tuple(
            ScriptSnapshot(
                str(item["name"]),
                base64.b64decode(str(item["content"]), validate=True),
                int(cast(int, item["mode"])),
            )
            for item in cast(list[dict[str, object]], record["scripts"])
        )
        return _snapshot(
            content, parse_configuration(content if content else None), scripts
        )

    def paths(self, root: Path) -> Mapping[str, Path]:
        result: dict[str, Path] = {}
        if not self.scripts:
            return MappingProxyType(result)
        directory = _open_registry_state_directory(root / self.digest, create=False)
        if directory is None:
            raise ValueError("Script snapshot is unavailable")
        os.close(directory)
        for index, script in enumerate(self.scripts):
            path = root / self.digest / str(index)
            content, mode = _read_script(path)
            if content != script.content or mode != script.mode:
                raise ValueError(
                    "Stored script snapshot differs from consented bytes or mode"
                )
            result[script.name] = path
        return MappingProxyType(result)

    def materialize(self, root: Path) -> Mapping[str, Path]:
        if not self.scripts:
            return MappingProxyType({})
        directory = _open_registry_state_directory(root / self.digest, create=True)
        if directory is None:
            raise ValueError("Script snapshot directory is unavailable")
        try:
            for index, script in enumerate(self.scripts):
                _publish_file(
                    directory, str(index), script.content, script.mode, replace=False
                )
        finally:
            os.close(directory)
        return self.paths(root)


def _read_script(path: Path) -> tuple[bytes, int]:
    try:
        descriptor = _open_configuration_file(path.absolute())
    except OSError as error:
        raise ValueError(
            "Direct local script must be a regular non-symlink file"
        ) from error
    with os.fdopen(descriptor, "rb") as opened:
        content = opened.read(SCRIPT_LIMIT + 1)
        mode = stat.S_IMODE(os.fstat(opened.fileno()).st_mode)
    if len(content) > SCRIPT_LIMIT:
        raise ValueError("Direct local script exceeds 1 MiB")
    if mode & 0o7000:
        raise ValueError("Direct local script must not carry special permission bits")
    return content, mode


def snapshot_configuration(
    content: bytes | None,
    source_root: Path,
    *,
    script_reader: ScriptReader | None = None,
) -> ConfigurationSnapshot:
    value = parse_configuration(content)
    scripts: list[ScriptSnapshot] = []
    root = source_root.absolute()
    for name in _script_names(value):
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("Direct local script must remain beneath its source root")
        data, mode = script_reader(name) if script_reader else _read_script(root / path)
        if (
            not isinstance(data, bytes)
            or len(data) > SCRIPT_LIMIT
            or type(mode) is not int
            or not 0 <= mode <= 0o777
        ):
            raise ValueError("Direct local script bytes or mode are invalid")
        scripts.append(ScriptSnapshot(name, data, mode))
    return _snapshot(content if content is not None else b"", value, tuple(scripts))


def _snapshot(
    content: bytes, value: Mapping[str, object], scripts: tuple[ScriptSnapshot, ...]
) -> ConfigurationSnapshot:
    covered = content
    if scripts:
        covered += (
            b"\0fangorn-scripts-v1\0"
            + json.dumps(
                [
                    {
                        "name": s.name,
                        "mode": s.mode,
                        "sha256": hashlib.sha256(s.content).hexdigest(),
                    }
                    for s in scripts
                ],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
    return ConfigurationSnapshot(
        content,
        cast(Mapping[str, object], _freeze(value)),
        hashlib.sha256(covered).hexdigest(),
        scripts,
    )


class ConsentStore:
    def __init__(self, state_root: Path | None = None) -> None:
        self.root = (
            state_root
            or Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
            / "fangorn"
            / "consent"
        )

    def _path(self, digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(
                "Configuration digest must be 64 lowercase hexadecimal characters"
            )
        return self.root / digest

    def list(self) -> tuple[str, ...]:
        directory = _open_registry_state_directory(self.root.absolute(), create=False)
        if directory is None:
            return ()
        try:
            return tuple(
                sorted(
                    name
                    for name in os.listdir(directory)
                    if re.fullmatch(r"[0-9a-f]{64}", name) and self._granted(name)
                )
            )
        finally:
            os.close(directory)

    def _granted(self, digest: str) -> bool:
        try:
            directory = _open_registry_state_directory(
                self.root.absolute(), create=False
            )
            if directory is None:
                return False
            os.close(directory)
            descriptor = _open_configuration_file(self._path(digest).absolute())
            with os.fdopen(descriptor, "rb") as opened:
                info = os.fstat(opened.fileno())
                if (
                    info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                    or info.st_mode & 0o077
                ):
                    return False
                return opened.read(128) == (digest + "\n").encode("ascii")
        except (OSError, ValueError, RegistryError, GitError):
            return False

    def require(self, digest: str) -> None:
        self._path(digest)
        if not self._granted(digest):
            raise ValueError(
                f"Configuration lacks consent: {digest}; "
                f"run fangorn consent grant {digest}"
            )

    def grant(self, digest: str) -> None:
        self._path(digest)
        directory = _open_registry_state_directory(self.root.absolute(), create=True)
        if directory is None:
            raise ValueError("Consent directory unavailable")
        try:
            _publish_file(
                directory, digest, (digest + "\n").encode("ascii"), 0o600, replace=True
            )
        finally:
            os.close(directory)

    def revoke(self, digest: str) -> None:
        self._path(digest)
        directory = _open_registry_state_directory(self.root.absolute(), create=False)
        if directory is not None:
            try:
                try:
                    os.unlink(digest, dir_fd=directory)
                    os.fsync(directory)
                except FileNotFoundError:
                    pass
            finally:
                os.close(directory)


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _publish_file(
    directory: int, name: str, content: bytes, mode: int, *, replace: bool
) -> None:
    temporary = f".fangorn-{uuid4().hex}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        with os.fdopen(descriptor, "wb") as opened:
            opened.write(content)
            opened.flush()
            os.fchmod(opened.fileno(), mode)
            os.fsync(opened.fileno())
        if replace:
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        else:
            with suppress(FileExistsError):
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
        os.fsync(directory)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)
