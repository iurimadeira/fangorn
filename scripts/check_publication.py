from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

EXCLUDED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}
SENSITIVE_NAMES = {
    ".env",
    "id_ed25519",
    "id_rsa",
    "registry.sqlite3",
}
SENSITIVE_SUFFIXES = {".key", ".p12", ".pem"}
CONTENT_PATTERNS = (
    re.compile(r"/(?:home|Users)/[^/\s]+/"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
)
PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
EXPECTED_PROJECT_URLS = {
    "Homepage": "https://github.com/iurimadeira/fangorn",
    "Issues": "https://github.com/iurimadeira/fangorn/issues",
}


class CheckFailure(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def _terminal_safe(value: str) -> str:
    rendered: list[str] = []
    for character in value:
        code_point = ord(character)
        if code_point < 0x20 or 0x7F <= code_point <= 0x9F:
            rendered.append(f"\\x{code_point:02x}")
        elif not character.isprintable():
            if code_point <= 0xFFFF:
                rendered.append(f"\\u{code_point:04x}")
            else:
                rendered.append(f"\\U{code_point:08x}")
        else:
            rendered.append(character)
    return "".join(rendered)


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise CheckFailure(f"Cannot read required file: {path}") from error


def _source_files(source: Path) -> Iterable[tuple[str, bytes]]:
    tracked_names: set[str] = set()
    for name, content in _tracked_source_files(source):
        tracked_names.add(name)
        yield name, content

    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        name = relative.as_posix()
        if name in tracked_names:
            continue
        if any(part in EXCLUDED_DIRECTORIES for part in relative.parts):
            continue
        if path.is_symlink():
            raise CheckFailure(f"Source tree contains a symlink: {relative}")
        if path.is_file():
            try:
                content = path.read_bytes()
            except OSError as error:
                detail = error.strerror or str(error)
                raise CheckFailure(
                    f"Cannot read source file {relative}: {detail}"
                ) from error
            yield name, content


def _tracked_source_files(source: Path) -> Iterable[tuple[str, bytes]]:
    environment = os.environ.copy()
    for name in (
        "GIT_COMMON_DIR",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_INDEX_FILE",
        "GIT_PREFIX",
        "GIT_WORK_TREE",
    ):
        environment.pop(name, None)
    try:
        result = subprocess.run(
            ["git", "-C", source, "ls-files", "--cached", "-z", "--"],
            check=False,
            capture_output=True,
            env=environment,
        )
    except FileNotFoundError as error:
        raise CheckFailure("Git executable was not found") from error
    except OSError as error:
        detail = error.strerror or str(error)
        raise CheckFailure(
            f"Cannot enumerate tracked source files: {detail}"
        ) from error
    if result.returncode != 0:
        detail = result.stderr.removesuffix(b"\n").decode(
            "utf-8", errors="backslashreplace"
        )
        raise CheckFailure(detail or "Cannot enumerate tracked source files")

    records = result.stdout.split(b"\0")
    _require(records[-1] == b"", "Git tracked path output is not NUL terminated")
    for raw_name in records[:-1]:
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError as error:
            raise CheckFailure("Git tracked path is not valid UTF-8") from error
        relative = PurePosixPath(name)
        _require(
            not relative.is_absolute() and ".." not in relative.parts,
            f"Git tracked path escapes the source tree: {name}",
        )
        path = source.joinpath(*relative.parts)
        try:
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise CheckFailure(f"Source tree contains a symlink: {name}")
            if not stat.S_ISREG(metadata.st_mode):
                raise CheckFailure(f"Tracked source is not a regular file: {name}")
            content = path.read_bytes()
        except CheckFailure:
            raise
        except OSError as error:
            detail = error.strerror or str(error)
            raise CheckFailure(f"Cannot read source file {name}: {detail}") from error
        yield name, content


def _validate_public_content(name: str, content: bytes) -> None:
    path = PurePosixPath(name)
    lowered_name = path.name.lower()
    _require(lowered_name not in SENSITIVE_NAMES, f"Sensitive file included: {name}")
    _require(
        path.suffix.lower() not in SENSITIVE_SUFFIXES,
        f"Sensitive file type included: {name}",
    )
    _require(b"\0" not in content, f"Unexpected binary content: {name}")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CheckFailure(f"Content is not valid UTF-8: {name}") from error
    _require(
        PRIVATE_KEY_PATTERN.search(text) is None,
        f"Private key material found: {name}",
    )
    for pattern in CONTENT_PATTERNS:
        _require(pattern.search(text) is None, f"Private data pattern found: {name}")


def validate_source(source: Path) -> None:
    pyproject_path = source / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as file:
            pyproject = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CheckFailure("pyproject.toml is missing or invalid") from error

    project = pyproject.get("project", {})
    build_system = pyproject.get("build-system", {})
    build_backend = pyproject.get("tool", {}).get("uv", {}).get("build-backend", {})
    dependencies = project.get("dependencies", [])
    dev_dependencies = pyproject.get("dependency-groups", {}).get("dev", [])
    _require(project.get("name") == "fangorn-cli", "Project name must be fangorn-cli")
    _require(project.get("license") == "MIT", "Project license must be MIT")
    _require(
        set(project.get("license-files", [])) == {"LICENSE", "THIRD_PARTY_NOTICES.md"},
        "License files must include LICENSE and THIRD_PARTY_NOTICES.md",
    )
    _require(
        project.get("requires-python") == ">=3.12",
        "Supported Python must be 3.12 or newer",
    )
    _require(
        build_system.get("build-backend") == "uv_build",
        "Build backend must be uv_build",
    )
    _require(
        build_backend.get("module-name") == "fangorn",
        "Build module must remain fangorn",
    )
    _require(
        project.get("urls") == EXPECTED_PROJECT_URLS,
        "Project URLs do not match the public repository",
    )
    _require(
        isinstance(dependencies, list)
        and len(dependencies) == 1
        and dependencies[0].lower().startswith("click"),
        "Click must be the only runtime dependency",
    )
    for tool in ("pytest", "ruff", "mypy"):
        _require(
            any(
                str(dependency).lower().startswith(tool)
                for dependency in dev_dependencies
            ),
            f"Missing required development tool: {tool}",
        )

    license_text = _text(source / "LICENSE")
    notices = _text(source / "THIRD_PARTY_NOTICES.md")
    readme = _text(source / "README.md")
    workflow = _text(source / ".github" / "workflows" / "ci.yml")
    _require("MIT License" in license_text, "LICENSE is not the MIT License")
    _require("Click" in notices, "Click dependency notice is missing")
    _require("BSD-3-Clause" in notices, "Click license notice is missing")
    _require("uv tool install fangorn-cli" in readme, "uv install instructions missing")
    _require("pipx install fangorn-cli" in readme, "pipx install instructions missing")
    _require("ubuntu-latest" in workflow, "Linux CI is missing")
    _require("macos-latest" in workflow, "macOS CI is missing")
    _require("uv build" in workflow, "Distribution build check is missing")
    action_uses = re.findall(r"uses:\s+[^@\s]+@([^\s]+)", workflow)
    _require(bool(action_uses), "CI has no pinned actions")
    _require(
        all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in action_uses),
        "Every CI action must be pinned to a full commit SHA",
    )

    for name, content in _source_files(source):
        _validate_public_content(name, content)


def _safe_archive_name(name: str) -> None:
    path = PurePosixPath(name)
    _require(not path.is_absolute(), f"Archive has an absolute path: {name}")
    _require(".." not in path.parts, f"Archive path escapes its root: {name}")


def _zip_contents(path: Path) -> list[tuple[str, bytes]]:
    contents: list[tuple[str, bytes]] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                _safe_archive_name(member.filename)
                mode = (member.external_attr >> 16) & 0o170000
                _require(
                    mode != stat.S_IFLNK, f"Archive contains symlink: {member.filename}"
                )
                if not member.is_dir():
                    contents.append((member.filename, archive.read(member)))
    except zipfile.BadZipFile as error:
        raise CheckFailure(f"Malformed wheel archive: {path}") from error
    except OSError as error:
        detail = error.strerror or str(error)
        raise CheckFailure(f"Cannot read wheel {path}: {detail}") from error
    return contents


def _tar_contents(path: Path) -> list[tuple[str, bytes]]:
    contents: list[tuple[str, bytes]] = []
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                _safe_archive_name(member.name)
                _require(
                    member.isfile() or member.isdir(),
                    f"Archive contains special entry: {member.name}",
                )
                if member.isfile():
                    extracted = archive.extractfile(member)
                    _require(
                        extracted is not None,
                        f"Cannot read archive entry: {member.name}",
                    )
                    contents.append((member.name, extracted.read()))
    except tarfile.TarError as error:
        raise CheckFailure(f"Malformed source distribution archive: {path}") from error
    except OSError as error:
        detail = error.strerror or str(error)
        raise CheckFailure(
            f"Cannot read source distribution {path}: {detail}"
        ) from error
    return contents


def validate_artifact(path: Path) -> None:
    if path.suffix == ".whl":
        _require(
            path.name.startswith("fangorn_cli-"),
            f"Unexpected wheel name: {path.name}",
        )
        contents = _zip_contents(path)
    elif path.name.endswith(".tar.gz"):
        _require(
            path.name.startswith("fangorn_cli-"),
            f"Unexpected source distribution name: {path.name}",
        )
        contents = _tar_contents(path)
    else:
        raise CheckFailure(f"Unsupported distribution artifact: {path}")

    names = [name for name, _ in contents]
    _require(
        any(PurePosixPath(name).name == "LICENSE" for name in names),
        f"LICENSE missing from {path}",
    )
    _require(
        any(PurePosixPath(name).name == "THIRD_PARTY_NOTICES.md" for name in names),
        f"Third-party notices missing from {path}",
    )
    for name, content in contents:
        _validate_public_content(name, content)

    if path.suffix == ".whl":
        metadata_entries = [
            content
            for name, content in contents
            if name.endswith(".dist-info/METADATA")
        ]
        _require(len(metadata_entries) == 1, f"Wheel metadata missing from {path}")
    else:
        metadata_entries = [
            content for name, content in contents if name.endswith("/PKG-INFO")
        ]
        _require(
            len(metadata_entries) == 1,
            f"Source distribution metadata missing from {path}",
        )
    _validate_project_metadata(metadata_entries[0], path)


def _validate_project_metadata(content: bytes, path: Path) -> None:
    metadata = content.decode("utf-8")
    _require(
        re.search(r"^Name: fangorn-cli$", metadata, re.MULTILINE) is not None,
        f"Distribution project name does not match: {path}",
    )
    _require("License-Expression: MIT" in metadata, "MIT metadata missing")
    _require("Requires-Python: >=3.12" in metadata, "Python metadata missing")
    runtime_dependencies = re.findall(
        r"^Requires-Dist:\s*([A-Za-z0-9_.-]+)", metadata, re.MULTILINE
    )
    _require(
        [dependency.lower() for dependency in runtime_dependencies] == ["click"],
        "Distribution must contain only the Click runtime dependency",
    )
    project_urls = {
        label: url
        for label, url in re.findall(
            r"^Project-URL:\s*([^,]+),\s*(\S+)$", metadata, re.MULTILINE
        )
    }
    _require(
        project_urls == EXPECTED_PROJECT_URLS,
        f"Distribution project URLs do not match: {path}",
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Fangorn license and public artifact privacy."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("artifacts", type=Path, nargs="*")
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    try:
        validate_source(arguments.source.resolve(strict=True))
        for artifact in arguments.artifacts:
            validate_artifact(artifact.resolve(strict=True))
    except (CheckFailure, OSError) as error:
        print(
            f"Publication check failed: {_terminal_safe(str(error))}",
            file=sys.stderr,
        )
        return 1

    checked = "source tree"
    if arguments.artifacts:
        checked += f" and {len(arguments.artifacts)} artifact(s)"
    print(f"Publication checks passed: {checked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
