from __future__ import annotations

import argparse
import re
import stat
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
PRIVATE_KEY_MARKER = "-----BEGIN " + "PRIVATE KEY-----"


class CheckFailure(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise CheckFailure(f"Cannot read required file: {path}") from error


def _source_files(source: Path) -> Iterable[tuple[str, bytes]]:
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in EXCLUDED_DIRECTORIES for part in relative.parts):
            continue
        if path.is_symlink():
            raise CheckFailure(f"Source tree contains a symlink: {relative}")
        if path.is_file():
            yield relative.as_posix(), path.read_bytes()


def _validate_public_content(name: str, content: bytes) -> None:
    path = PurePosixPath(name)
    lowered_name = path.name.lower()
    _require(lowered_name not in SENSITIVE_NAMES, f"Sensitive file included: {name}")
    _require(
        path.suffix.lower() not in SENSITIVE_SUFFIXES,
        f"Sensitive file type included: {name}",
    )
    if b"\0" in content:
        return
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return
    _require(PRIVATE_KEY_MARKER not in text, f"Private key material found: {name}")
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
    dependencies = project.get("dependencies", [])
    dev_dependencies = pyproject.get("dependency-groups", {}).get("dev", [])
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
    _require("Click" in notices, "Click provenance notice is missing")
    _require("BSD-3-Clause" in notices, "Click license notice is missing")
    _require("uv tool install fangorn" in readme, "uv install instructions missing")
    _require("pipx install fangorn" in readme, "pipx install instructions missing")
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
    except (OSError, zipfile.BadZipFile) as error:
        raise CheckFailure(f"Invalid wheel: {path}") from error
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
    except (OSError, tarfile.TarError) as error:
        raise CheckFailure(f"Invalid source distribution: {path}") from error
    return contents


def validate_artifact(path: Path) -> None:
    if path.suffix == ".whl":
        contents = _zip_contents(path)
    elif path.name.endswith(".tar.gz"):
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
        metadata = metadata_entries[0].decode("utf-8")
        _require("License-Expression: MIT" in metadata, "Wheel MIT metadata missing")
        _require("Requires-Python: >=3.12" in metadata, "Wheel Python metadata missing")
        runtime_dependencies = re.findall(
            r"^Requires-Dist:\s*([A-Za-z0-9_.-]+)", metadata, re.MULTILINE
        )
        _require(
            [dependency.lower() for dependency in runtime_dependencies] == ["click"],
            "Wheel must contain only the Click runtime dependency",
        )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Fangorn license, provenance, and public artifact privacy."
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
        print(f"Publication check failed: {error}", file=sys.stderr)
        return 1

    checked = "source tree"
    if arguments.artifacts:
        checked += f" and {len(arguments.artifacts)} artifact(s)"
    print(f"Publication checks passed: {checked}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
