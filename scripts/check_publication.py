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
from collections import Counter
from collections.abc import Iterable
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import cast

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

EXCLUDED_DIRECTORIES = {
    ".coverage-data",
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
LOCAL_REVIEW_CONTEXT = PurePosixPath(".hunk/agent-context.json")
CONTENT_PATTERNS = (
    re.compile(r"/(?:home|Users)/[^/\s]+/"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github[_]pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk[-]proj-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
)
PRIVATE_INFRASTRUCTURE_IDENTIFIERS = (
    "ac" + "c",
    "ac" + "o",
    "ai-account" + "-router",
    "iuri-sync" + "-vault",
    "iurimadeira" + "-dot-files",
    "lab" + ".local",
    "lab-" + "lan",
    "lab-" + "tmux",
    "mac-" + "import",
    "oh-my-" + "fangorn",
    "ws" + "n",
)
PRIVATE_INFRASTRUCTURE_PATTERNS = tuple(
    re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(identifier)}(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    for identifier in PRIVATE_INFRASTRUCTURE_IDENTIFIERS
)
PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
EXPECTED_PROJECT_URLS = {
    "Homepage": "https://github.com/iurimadeira/fangorn",
    "Issues": "https://github.com/iurimadeira/fangorn/issues",
}
EXACT_RUNTIME_DEPENDENCY = "click>=8.1.8,<9"
PRIVATE_REPORT_URL = "https://github.com/iurimadeira/fangorn/security/advisories/new"
REQUIRED_PUBLIC_FILES = {
    ".github/ISSUE_TEMPLATE/bug.yml": (
        "Bug report",
        "sanitized",
        PRIVATE_REPORT_URL,
    ),
    ".github/ISSUE_TEMPLATE/config.yml": (PRIVATE_REPORT_URL,),
    ".github/ISSUE_TEMPLATE/proposal.yml": ("Proposal", "accepted Issue"),
    ".github/SECURITY.md": (
        "Report vulnerabilities confidentially",
        "Do not open a public Issue",
        PRIVATE_REPORT_URL,
    ),
    ".github/pull_request_template.md": ("Closes #", "sanitized"),
    "CODE_OF_CONDUCT.md": (
        "Contributor Covenant",
        "version 2.1",
        PRIVATE_REPORT_URL,
    ),
    "CONTRIBUTING.md": (
        "accepted Issue",
        "uv sync --locked --dev",
        "uv run coverage erase",
        "uv run coverage run --branch -m pytest",
        "uv run coverage combine",
        "uv run coverage report --fail-under=85.0",
        "uv run mypy src scripts tests",
        "sanitized",
        PRIVATE_REPORT_URL,
    ),
}
REPORTING_LINK_LABELS = {
    ".github/SECURITY.md": "GitHub Private Vulnerability Reporting",
    "CODE_OF_CONDUCT.md": "GitHub Private Vulnerability Reporting",
    "CONTRIBUTING.md": "GitHub Private Vulnerability Reporting",
}
REPORTING_POLICY_PATTERNS = {
    ".github/ISSUE_TEMPLATE/bug.yml": re.compile(
        r"Security reports do not belong in public Issues\. Use the\s+"
        r"\[confidential reporting form\]\([^)]*\)\s+instead\."
    ),
    ".github/SECURITY.md": re.compile(
        r"Report vulnerabilities confidentially with\s+"
        r"\[GitHub Private Vulnerability Reporting\]\([^)]*\)\.\s+"
        r"Do not open a public Issue, discussion, or pull request"
    ),
    "CODE_OF_CONDUCT.md": re.compile(
        r"Instances of abusive, harassing, or otherwise unacceptable behavior may be "
        r"reported through \[GitHub Private Vulnerability Reporting\]\([^)]*\)\."
    ),
    "CONTRIBUTING.md": re.compile(
        r"Report vulnerabilities or conduct incidents through\s+"
        r"\[GitHub Private Vulnerability Reporting\]\([^)]*\),\s+"
        r"as described in \[the security policy\]\(\.github/SECURITY\.md\)\."
    ),
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
    except UnicodeDecodeError as error:
        raise CheckFailure(f"Required file is not valid UTF-8: {path}") from error
    except OSError as error:
        raise CheckFailure(f"Cannot read required file: {path}") from error


def _visible_policy_text(text: str) -> str:
    without_html_comments = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return "\n".join(
        line
        for line in without_html_comments.splitlines()
        if not line.lstrip().startswith("#")
    )


def _parse_yaml(text: str, name: str) -> Node:
    try:
        document = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as error:
        raise CheckFailure(f"Required public YAML is invalid: {name}") from error
    _require(document is not None, f"Required public YAML is empty: {name}")
    document = cast(Node, document)
    _reject_duplicate_yaml_keys(document, name)
    return document


def _reject_duplicate_yaml_keys(node: Node, name: str) -> None:
    if isinstance(node, MappingNode):
        keys: set[str] = set()
        for key, value in node.value:
            _require(
                isinstance(key, ScalarNode),
                f"Required public YAML has a non-scalar key: {name}",
            )
            scalar_key = cast(ScalarNode, key)
            _require(
                scalar_key.value not in keys,
                f"Required public YAML has a duplicate YAML key: {name}: "
                f"{scalar_key.value}",
            )
            keys.add(scalar_key.value)
            _reject_duplicate_yaml_keys(value, name)
    elif isinstance(node, SequenceNode):
        for value in node.value:
            _reject_duplicate_yaml_keys(value, name)


def _yaml_mapping(node: Node | None, name: str) -> dict[str, Node]:
    _require(
        isinstance(node, MappingNode),
        f"Required public YAML is not a map: {name}",
    )
    mapping = cast(MappingNode, node)
    return {
        key.value: value for key, value in mapping.value if isinstance(key, ScalarNode)
    }


def _yaml_scalar(node: Node | None, name: str, field: str) -> str:
    _require(
        isinstance(node, ScalarNode),
        f"Required public YAML field is not scalar: {name}: {field}",
    )
    value = cast(ScalarNode, node).value
    _require(
        isinstance(value, str),
        f"Required public YAML field is not text: {name}: {field}",
    )
    return cast(str, value)


def _yaml_scalar_values(node: Node) -> Iterable[str]:
    if isinstance(node, ScalarNode):
        yield node.value
    elif isinstance(node, MappingNode):
        for _key, value in node.value:
            yield from _yaml_scalar_values(value)
    elif isinstance(node, SequenceNode):
        for value in node.value:
            yield from _yaml_scalar_values(value)


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
        if PurePosixPath(name) == LOCAL_REVIEW_CONTEXT:
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
        result = subprocess.run(  # noqa: S603 -- fixed Git argv, no shell
            [  # noqa: S607 -- Git lookup intentionally follows process PATH
                "git",
                "-C",
                source,
                "ls-files",
                "--cached",
                "-z",
                "--",
            ],
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
    path = PurePosixPath(name.replace("\\", "/"))
    lowered_name = path.name.lower()
    _require(
        tuple(part.lower() for part in path.parts[-2:])
        != tuple(part.lower() for part in LOCAL_REVIEW_CONTEXT.parts),
        f"Sensitive file included: {name}",
    )
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
    for pattern in PRIVATE_INFRASTRUCTURE_PATTERNS:
        _require(
            pattern.search(name) is None and pattern.search(text) is None,
            f"Forbidden private infrastructure identifier found: {name}",
        )


def validate_source(source: Path) -> tuple[str, bytes, bytes]:
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
    version = project.get("version")
    _require(project.get("name") == "fangorn-cli", "Project name must be fangorn-cli")
    _require(isinstance(version, str) and bool(version), "Project version is missing")
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
        build_system.get("requires") == ["uv_build==0.12.7"],
        "Build backend requirement must be uv_build==0.12.7",
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
        dependencies == [EXACT_RUNTIME_DEPENDENCY],
        f"Runtime dependency must be exactly {EXACT_RUNTIME_DEPENDENCY}",
    )
    for tool in ("pytest", "ruff", "mypy"):
        _require(
            any(
                str(dependency).lower().startswith(tool)
                for dependency in dev_dependencies
            ),
            f"Missing required development tool: {tool}",
        )

    public_text: dict[str, str] = {}
    yaml_documents: dict[str, Node] = {}
    for name, markers in REQUIRED_PUBLIC_FILES.items():
        path = source / name
        _require(path.is_file(), f"Missing required public file: {name}")
        raw_text = _text(path)
        if path.suffix == ".yml":
            document = _parse_yaml(raw_text, name)
            yaml_documents[name] = document
            text = "\n".join(_yaml_scalar_values(document))
        else:
            text = _visible_policy_text(raw_text)
        public_text[name] = text
        for marker in markers:
            _require(marker in text, f"Required public marker missing from {name}")

    for name, label in REPORTING_LINK_LABELS.items():
        destinations = re.findall(
            rf"\[{re.escape(label)}\]\(([^)\s]+)\)", public_text[name]
        )
        _require(
            destinations == [PRIVATE_REPORT_URL],
            f"Required public reporting link is invalid in {name}",
        )
        _require(
            REPORTING_POLICY_PATTERNS[name].search(public_text[name]) is not None,
            f"Required public reporting guidance is invalid in {name}",
        )

    for name, expected_name in (
        (".github/ISSUE_TEMPLATE/bug.yml", "Bug report"),
        (".github/ISSUE_TEMPLATE/proposal.yml", "Proposal"),
    ):
        issue_form = _yaml_mapping(yaml_documents[name], name)
        _require(
            _yaml_scalar(issue_form.get("name"), name, "name") == expected_name,
            f"Required public Issue-form name is invalid: {name}",
        )

    bug_name = ".github/ISSUE_TEMPLATE/bug.yml"
    bug = _yaml_mapping(yaml_documents[bug_name], bug_name)
    bug_body = bug.get("body")
    _require(
        isinstance(bug_body, SequenceNode),
        f"Required public YAML field is not a list: {bug_name}: body",
    )
    markdown_values: list[str] = []
    privacy_items: list[dict[str, Node]] = []
    for item in cast(SequenceNode, bug_body).value:
        item_mapping = _yaml_mapping(item, bug_name)
        item_type = _yaml_scalar(item_mapping.get("type"), bug_name, "body.type")
        if "id" in item_mapping and (
            _yaml_scalar(item_mapping["id"], bug_name, "body.id") == "privacy"
        ):
            privacy_items.append(item_mapping)
        if item_type != "markdown":
            continue
        attributes = _yaml_mapping(item_mapping.get("attributes"), bug_name)
        markdown_values.append(
            _yaml_scalar(attributes.get("value"), bug_name, "body.attributes.value")
        )
    _require(
        len(markdown_values) == 1,
        f"Required public reporting guidance is invalid in {bug_name}",
    )
    bug_guidance = markdown_values[0]
    bug_destinations = re.findall(
        r"\[confidential reporting form\]\(([^)\s]+)\)", bug_guidance
    )
    _require(
        bug_destinations == [PRIVATE_REPORT_URL]
        and REPORTING_POLICY_PATTERNS[bug_name].search(bug_guidance) is not None,
        f"Required public reporting guidance is invalid in {bug_name}",
    )

    _require(
        len(privacy_items) == 1
        and _yaml_scalar(privacy_items[0].get("type"), bug_name, "body.type")
        == "checkboxes",
        f"Required public privacy checkbox is invalid in {bug_name}",
    )
    privacy_attributes = _yaml_mapping(privacy_items[0].get("attributes"), bug_name)
    privacy_options = privacy_attributes.get("options")
    _require(
        isinstance(privacy_options, SequenceNode) and len(privacy_options.value) == 1,
        f"Required public privacy checkbox is invalid in {bug_name}",
    )
    privacy_option = _yaml_mapping(
        cast(SequenceNode, privacy_options).value[0], bug_name
    )
    privacy_required = privacy_option.get("required")
    _require(
        _yaml_scalar(privacy_option.get("label"), bug_name, "body.options.label")
        == "I removed credentials, private paths, and private infrastructure details."
        and isinstance(privacy_required, ScalarNode)
        and privacy_required.tag == "tag:yaml.org,2002:bool"
        and privacy_required.value == "true",
        f"Required public privacy checkbox is invalid in {bug_name}",
    )

    proposal_name = ".github/ISSUE_TEMPLATE/proposal.yml"
    proposal = _yaml_mapping(yaml_documents[proposal_name], proposal_name)
    proposal_body = proposal.get("body")
    _require(
        isinstance(proposal_body, SequenceNode),
        f"Required public YAML field is not a list: {proposal_name}: body",
    )
    readiness_items: list[dict[str, Node]] = []
    for item in cast(SequenceNode, proposal_body).value:
        item_mapping = _yaml_mapping(item, proposal_name)
        if "id" in item_mapping and (
            _yaml_scalar(item_mapping["id"], proposal_name, "body.id") == "readiness"
        ):
            readiness_items.append(item_mapping)
    _require(
        len(readiness_items) == 1
        and _yaml_scalar(readiness_items[0].get("type"), proposal_name, "body.type")
        == "checkboxes",
        f"Required public proposal readiness is invalid in {proposal_name}",
    )
    readiness_attributes = _yaml_mapping(
        readiness_items[0].get("attributes"), proposal_name
    )
    readiness_options = readiness_attributes.get("options")
    expected_readiness_labels = (
        "I will wait for an accepted Issue before starting implementation.",
        "I sanitized all public examples and removed private data.",
    )
    _require(
        isinstance(readiness_options, SequenceNode)
        and len(readiness_options.value) == len(expected_readiness_labels),
        f"Required public proposal readiness is invalid in {proposal_name}",
    )
    for option_node, expected_label in zip(
        cast(SequenceNode, readiness_options).value,
        expected_readiness_labels,
        strict=True,
    ):
        option = _yaml_mapping(option_node, proposal_name)
        required = option.get("required")
        _require(
            _yaml_scalar(option.get("label"), proposal_name, "body.options.label")
            == expected_label
            and isinstance(required, ScalarNode)
            and required.tag == "tag:yaml.org,2002:bool"
            and required.value == "true",
            f"Required public proposal readiness is invalid in {proposal_name}",
        )

    config_name = ".github/ISSUE_TEMPLATE/config.yml"
    config = _yaml_mapping(yaml_documents[config_name], config_name)
    blank_issues = config.get("blank_issues_enabled")
    _require(
        isinstance(blank_issues, ScalarNode)
        and blank_issues.tag == "tag:yaml.org,2002:bool"
        and blank_issues.value == "false",
        "Required public Issue-form configuration is invalid",
    )
    contact_links = config.get("contact_links")
    _require(
        isinstance(contact_links, SequenceNode) and len(contact_links.value) == 1,
        "Required public Issue-form contact links are invalid",
    )
    contact = _yaml_mapping(cast(SequenceNode, contact_links).value[0], config_name)
    _require(
        _yaml_scalar(contact.get("name"), config_name, "contact_links.name")
        == "Confidential security or conduct report"
        and _yaml_scalar(contact.get("url"), config_name, "contact_links.url")
        == PRIVATE_REPORT_URL
        and _yaml_scalar(contact.get("about"), config_name, "contact_links.about")
        == "Use GitHub Private Vulnerability Reporting instead of a public Issue.",
        "Required public Issue-form reporting link is invalid",
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

    return (
        version,
        (source / "LICENSE").read_bytes(),
        (source / "THIRD_PARTY_NOTICES.md").read_bytes(),
    )


def _canonical_archive_name(name: str, *, directory: bool) -> str:
    _require("\\" not in name, f"Archive path uses a backslash: {name}")
    path = PurePosixPath(name)
    _require(not path.is_absolute(), f"Archive has an absolute path: {name}")
    _require(".." not in path.parts, f"Archive path escapes its root: {name}")
    canonical = path.as_posix()
    comparable = name[:-1] if directory and name.endswith("/") else name
    _require(
        canonical != "." and comparable == canonical,
        f"Archive path is not canonical: {name}",
    )
    return canonical


def _zip_contents(path: Path) -> list[tuple[str, bytes]]:
    contents: list[tuple[str, bytes]] = []
    file_names: set[str] = set()
    try:
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                name = _canonical_archive_name(
                    member.filename, directory=member.is_dir()
                )
                mode = (member.external_attr >> 16) & 0o170000
                _require(
                    mode != stat.S_IFLNK, f"Archive contains symlink: {member.filename}"
                )
                if not member.is_dir():
                    _require(
                        name not in file_names,
                        f"Archive contains duplicate file path: {name}",
                    )
                    file_names.add(name)
                    try:
                        content = archive.read(member)
                    except NotImplementedError as error:
                        raise CheckFailure(
                            "Unsupported compression for wheel member: "
                            f"{member.filename}"
                        ) from error
                    except RuntimeError as error:
                        if not member.flag_bits & 0x1:
                            raise
                        raise CheckFailure(
                            f"Encrypted wheel member cannot be read: {member.filename}"
                        ) from error
                    contents.append((name, content))
    except zipfile.BadZipFile as error:
        raise CheckFailure(f"Malformed wheel archive: {path}") from error
    except OSError as error:
        detail = error.strerror or str(error)
        raise CheckFailure(f"Cannot read wheel {path}: {detail}") from error
    return contents


def _tar_contents(path: Path) -> list[tuple[str, bytes]]:
    contents: list[tuple[str, bytes]] = []
    file_names: set[str] = set()
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                name = _canonical_archive_name(member.name, directory=member.isdir())
                _require(
                    member.isfile() or member.isdir(),
                    f"Archive contains special entry: {member.name}",
                )
                if member.isfile():
                    _require(
                        name not in file_names,
                        f"Archive contains duplicate file path: {name}",
                    )
                    file_names.add(name)
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise CheckFailure(f"Cannot read archive entry: {member.name}")
                    contents.append((name, extracted.read()))
    except tarfile.TarError as error:
        raise CheckFailure(f"Malformed source distribution archive: {path}") from error
    except OSError as error:
        detail = error.strerror or str(error)
        raise CheckFailure(
            f"Cannot read source distribution {path}: {detail}"
        ) from error
    return contents


def validate_artifact_set(
    paths: list[Path],
    *,
    version: str,
    license_content: bytes,
    notices_content: bytes,
) -> None:
    wheels = [path for path in paths if path.suffix == ".whl"]
    sdists = [path for path in paths if path.name.endswith(".tar.gz")]
    _require(
        len(paths) == 2 and len(wheels) == 1 and len(sdists) == 1,
        "Release set must contain exactly one wheel and one source distribution",
    )
    _require(
        wheels[0].name == f"fangorn_cli-{version}-py3-none-any.whl",
        f"Wheel artifact filename does not match project version: {wheels[0].name}",
    )
    _require(
        sdists[0].name == f"fangorn_cli-{version}.tar.gz",
        "Source distribution artifact filename does not match project version: "
        f"{sdists[0].name}",
    )
    for path in paths:
        validate_artifact(
            path,
            version=version,
            license_content=license_content,
            notices_content=notices_content,
        )


def validate_artifact(
    path: Path,
    *,
    version: str,
    license_content: bytes,
    notices_content: bytes,
) -> None:
    if path.suffix == ".whl":
        root = f"fangorn_cli-{version}.dist-info"
        metadata_name = f"{root}/METADATA"
        license_name = f"{root}/licenses/LICENSE"
        notices_name = f"{root}/licenses/THIRD_PARTY_NOTICES.md"
        contents = _zip_contents(path)
    elif path.name.endswith(".tar.gz"):
        root = f"fangorn_cli-{version}"
        metadata_name = f"{root}/PKG-INFO"
        license_name = f"{root}/LICENSE"
        notices_name = f"{root}/THIRD_PARTY_NOTICES.md"
        contents = _tar_contents(path)
    else:
        raise CheckFailure(f"Unsupported distribution artifact: {path}")

    for name, content in contents:
        _validate_public_content(name, content)
    entries = dict(contents)
    _require(metadata_name in entries, f"Distribution metadata missing from {path}")
    _require(license_name in entries, f"Expected LICENSE path missing from {path}")
    _require(
        notices_name in entries,
        f"Expected third-party notices path missing from {path}",
    )
    _require(
        entries[license_name] == license_content,
        f"Packaged LICENSE does not match source: {path}",
    )
    _require(
        entries[notices_name] == notices_content,
        f"Packaged third-party notices do not match source: {path}",
    )
    _validate_project_metadata(entries[metadata_name], path, version=version)


def _validate_project_metadata(content: bytes, path: Path, *, version: str) -> None:
    metadata = BytesParser(policy=policy.default).parsebytes(content, headersonly=True)
    _require(
        not metadata.defects,
        f"Distribution metadata is malformed: {path}",
    )
    expected_fields = (
        (
            "Name",
            "fangorn-cli",
            f"Distribution metadata project name does not match: {path}",
        ),
        (
            "Version",
            version,
            f"Distribution metadata version does not match project: {path}",
        ),
        (
            "License-Expression",
            "MIT",
            f"Distribution metadata License-Expression must be exactly MIT: {path}",
        ),
        (
            "Requires-Python",
            ">=3.12",
            f"Distribution metadata Requires-Python must be exactly >=3.12: {path}",
        ),
        (
            "Requires-Dist",
            EXACT_RUNTIME_DEPENDENCY,
            "Distribution metadata must contain exact Requires-Dist: "
            f"{EXACT_RUNTIME_DEPENDENCY}: {path}",
        ),
    )
    for field, expected, error in expected_fields:
        _require(_metadata_values(metadata, field) == [expected], error)

    project_urls = _metadata_values(metadata, "Project-URL")
    expected_urls = [f"{label}, {url}" for label, url in EXPECTED_PROJECT_URLS.items()]
    _require(
        Counter(project_urls) == Counter(expected_urls),
        f"Distribution project URLs do not match: {path}",
    )


def _metadata_values(metadata: Message, field: str) -> list[str]:
    return [str(value) for value in metadata.get_all(field, [])]


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
        version, license_content, notices_content = validate_source(
            arguments.source.resolve(strict=True)
        )
        if arguments.artifacts:
            validate_artifact_set(
                [artifact.resolve(strict=True) for artifact in arguments.artifacts],
                version=version,
                license_content=license_content,
                notices_content=notices_content,
            )
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
