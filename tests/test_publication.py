from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest
from git_helpers import git, initialize_repository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_VERSION = "0.1.0"
LICENSE_BYTES = (PROJECT_ROOT / "LICENSE").read_bytes()
NOTICES_BYTES = (PROJECT_ROOT / "THIRD_PARTY_NOTICES.md").read_bytes()
EXACT_CLICK_REQUIREMENT = "click>=8.1.8,<9"


PRIVATE_KEY_HEADERS = tuple(
    "-----BEGIN " + prefix + "PRIVATE KEY-----"
    for prefix in ("", "RSA ", "EC ", "DSA ", "OPENSSH ")
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
PRIVATE_PATHS = (
    "/home/" + "example/private/",
    "/Users/" + "example/private/",
)
PRIVATE_REPORT_URL = (
    "https://github.com/iurimadeira/fangorn/security/" + "advisories/new"
)
REQUIRED_PUBLIC_MARKERS = (
    (".github/ISSUE_TEMPLATE/bug.yml", "name: Bug report"),
    (".github/ISSUE_TEMPLATE/bug.yml", "sanitized"),
    (".github/ISSUE_TEMPLATE/bug.yml", PRIVATE_REPORT_URL),
    (".github/ISSUE_TEMPLATE/config.yml", "blank_issues_enabled: false"),
    (".github/ISSUE_TEMPLATE/config.yml", PRIVATE_REPORT_URL),
    (".github/ISSUE_TEMPLATE/proposal.yml", "name: Proposal"),
    (".github/ISSUE_TEMPLATE/proposal.yml", "accepted Issue"),
    (".github/SECURITY.md", "Report vulnerabilities confidentially"),
    (".github/SECURITY.md", "Do not open a public Issue"),
    (".github/SECURITY.md", PRIVATE_REPORT_URL),
    (".github/pull_request_template.md", "Closes #"),
    (".github/pull_request_template.md", "sanitized"),
    ("CODE_OF_CONDUCT.md", "Contributor Covenant"),
    ("CODE_OF_CONDUCT.md", "version 2.1"),
    ("CODE_OF_CONDUCT.md", PRIVATE_REPORT_URL),
    ("CONTRIBUTING.md", "accepted Issue"),
    ("CONTRIBUTING.md", "uv sync --locked --dev"),
    ("CONTRIBUTING.md", "uv run coverage erase"),
    ("CONTRIBUTING.md", "uv run coverage run --branch -m pytest"),
    ("CONTRIBUTING.md", "uv run coverage combine"),
    ("CONTRIBUTING.md", "uv run coverage report --fail-under=85.0"),
    ("CONTRIBUTING.md", "uv run mypy src scripts tests"),
    ("CONTRIBUTING.md", "sanitized"),
    ("CONTRIBUTING.md", PRIVATE_REPORT_URL),
)


def run_publication_gate(
    *artifacts: Path,
    source: Path = PROJECT_ROOT,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- test controls executable and argv
        [
            sys.executable,
            PROJECT_ROOT / "scripts" / "check_publication.py",
            "--source",
            source,
            *artifacts,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def copy_source_to_temporary_repository(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    shutil.copytree(
        PROJECT_ROOT,
        source,
        ignore=shutil.ignore_patterns(
            ".coverage-data",
            ".git",
            ".hunk",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            "__pycache__",
            "build",
            "dist",
        ),
    )
    initialize_repository(source)
    git(source, "add", "--all")
    return source


def write_test_wheel(
    path: Path,
    *,
    payload: bytes = b"value = 1\n",
    homepage: str = "https://github.com/iurimadeira/fangorn",
    metadata_version: str = PROJECT_VERSION,
    dependency: str = EXACT_CLICK_REQUIREMENT,
    metadata_content: str | None = None,
    license_entries: dict[str, bytes] | None = None,
    extra_entries: tuple[tuple[str, bytes], ...] = (),
) -> None:
    root = f"fangorn_cli-{PROJECT_VERSION}.dist-info"
    metadata = metadata_content
    if metadata is None:
        metadata = project_metadata(
            version=metadata_version,
            homepage=homepage,
            dependency=dependency,
        )
    if license_entries is None:
        license_entries = {
            f"{root}/licenses/LICENSE": LICENSE_BYTES,
            f"{root}/licenses/THIRD_PARTY_NOTICES.md": NOTICES_BYTES,
        }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("fangorn/__init__.py", payload)
        archive.writestr(f"{root}/METADATA", metadata)
        for name, content in (*license_entries.items(), *extra_entries):
            archive.writestr(name, content)


def project_metadata(
    *,
    version: str = PROJECT_VERSION,
    homepage: str = "https://github.com/iurimadeira/fangorn",
    dependency: str = EXACT_CLICK_REQUIREMENT,
) -> str:
    return f"""Metadata-Version: 2.4
Name: fangorn-cli
Version: {version}
License-Expression: MIT
Requires-Python: >=3.12
Requires-Dist: {dependency}
Project-URL: Homepage, {homepage}
Project-URL: Issues, https://github.com/iurimadeira/fangorn/issues

Fangorn
"""


def write_test_sdist(
    path: Path,
    *,
    metadata_version: str = PROJECT_VERSION,
    dependency: str = EXACT_CLICK_REQUIREMENT,
    metadata_content: str | None = None,
    license_entries: dict[str, bytes] | None = None,
    extra_entries: tuple[tuple[str, bytes], ...] = (),
) -> None:
    root = f"fangorn_cli-{PROJECT_VERSION}"
    if license_entries is None:
        license_entries = {
            f"{root}/LICENSE": LICENSE_BYTES,
            f"{root}/THIRD_PARTY_NOTICES.md": NOTICES_BYTES,
        }
    metadata = metadata_content
    if metadata is None:
        metadata = project_metadata(
            version=metadata_version,
            dependency=dependency,
        )
    entries = {
        f"{root}/PKG-INFO": metadata.encode(),
        f"{root}/fangorn/__init__.py": b"value = 1\n",
        **license_entries,
    }
    with tarfile.open(path, "w:gz") as archive:
        for name, content in (*entries.items(), *extra_entries):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(content))


def write_valid_artifact_set(tmp_path: Path) -> tuple[Path, Path]:
    wheel = tmp_path / f"fangorn_cli-{PROJECT_VERSION}-py3-none-any.whl"
    sdist = tmp_path / f"fangorn_cli-{PROJECT_VERSION}.tar.gz"
    write_test_wheel(wheel)
    write_test_sdist(sdist)
    return wheel, sdist


def alter_first_wheel_member(
    path: Path, *, encrypted: bool = False, compression: int | None = None
) -> None:
    content = bytearray(path.read_bytes())
    local = content.index(b"PK\x03\x04")
    central = content.index(b"PK\x01\x02")
    if encrypted:
        for offset in (local + 6, central + 8):
            flags = int.from_bytes(content[offset : offset + 2], "little") | 0x1
            content[offset : offset + 2] = flags.to_bytes(2, "little")
    if compression is not None:
        for offset in (local + 8, central + 10):
            content[offset : offset + 2] = compression.to_bytes(2, "little")
    path.write_bytes(content)


def test_publication_gate_accepts_the_public_source_tree() -> None:
    result = run_publication_gate()

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Publication checks passed: source tree\n"
    assert result.stderr == ""
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)
    project = pyproject["project"]
    assert project["name"] == "fangorn-cli"
    assert pyproject["build-system"]["requires"] == ["uv_build==0.12.7"]
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "uv tool install fangorn-cli" in readme
    assert "pipx install fangorn-cli" in readme


def test_publication_gate_ignores_untracked_coverage_data(tmp_path: Path) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    coverage_data = source / ".coverage-data"
    coverage_data.mkdir()
    (coverage_data / ".coverage.worker").write_bytes(b"\x00coverage database")

    result = run_publication_gate(source=source)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Publication checks passed: source tree\n"


@pytest.mark.parametrize(
    "relative_name",
    [
        ".github/ISSUE_TEMPLATE/bug.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/ISSUE_TEMPLATE/proposal.yml",
        ".github/SECURITY.md",
        ".github/pull_request_template.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
    ],
)
def test_publication_gate_requires_public_contribution_files(
    tmp_path: Path, relative_name: str
) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    (source / relative_name).unlink()

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Missing required public file" in result.stderr


@pytest.mark.parametrize(("relative_name", "marker"), REQUIRED_PUBLIC_MARKERS)
def test_publication_gate_requires_public_contribution_markers(
    tmp_path: Path, relative_name: str, marker: str
) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    path = source / relative_name
    content = path.read_text(encoding="utf-8")
    replacement = "removed marker"
    if path.suffix == ".yml" and ": " in marker:
        replacement = f"{marker.split(':', 1)[0]}: removed marker"
    path.write_text(content.replace(marker, replacement), encoding="utf-8")

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Required public" in result.stderr


def test_publication_gate_rejects_non_utf8_public_file(tmp_path: Path) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    (source / "CONTRIBUTING.md").write_bytes(b"invalid: \xff\n")

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert "Required file is not valid UTF-8" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("relative_name", "replacements", "hidden_markers"),
    [
        (
            ".github/ISSUE_TEMPLATE/bug.yml",
            ((PRIVATE_REPORT_URL, "https://example.invalid/report"),),
            f"description: placeholder # {PRIVATE_REPORT_URL}\n",
        ),
        (
            ".github/ISSUE_TEMPLATE/config.yml",
            (
                ("blank_issues_enabled: false", "blank_issues_enabled: true"),
                (PRIVATE_REPORT_URL, "https://example.invalid/report"),
            ),
            f"# blank_issues_enabled: false\n# {PRIVATE_REPORT_URL}\n",
        ),
        (
            ".github/SECURITY.md",
            (
                (
                    "Report vulnerabilities confidentially",
                    "Publish vulnerabilities openly",
                ),
                ("Do not open a public Issue", "Open a public Issue"),
                (PRIVATE_REPORT_URL, "https://example.invalid/report"),
            ),
            "<!-- Report vulnerabilities confidentially\n"
            "Do not open a public Issue\n"
            f"{PRIVATE_REPORT_URL} -->\n",
        ),
        (
            "CODE_OF_CONDUCT.md",
            ((PRIVATE_REPORT_URL, "https://example.invalid/report"),),
            f"<!-- {PRIVATE_REPORT_URL} -->\n",
        ),
        (
            "CONTRIBUTING.md",
            ((PRIVATE_REPORT_URL, "https://example.invalid/report"),),
            f"<!-- {PRIVATE_REPORT_URL} -->\n",
        ),
    ],
)
def test_publication_gate_rejects_hidden_public_policy_markers(
    tmp_path: Path,
    relative_name: str,
    replacements: tuple[tuple[str, str], ...],
    hidden_markers: str,
) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    path = source / relative_name
    content = path.read_text(encoding="utf-8")
    for expected, replacement in replacements:
        content = content.replace(expected, replacement)
    path.write_text(content + hidden_markers, encoding="utf-8")

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert "Required public" in result.stderr


def test_publication_gate_rejects_duplicate_issue_form_keys(tmp_path: Path) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    config = source / ".github/ISSUE_TEMPLATE/config.yml"
    config.write_text(
        config.read_text(encoding="utf-8") + "blank_issues_enabled: true\n",
        encoding="utf-8",
    )

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert "duplicate YAML key" in result.stderr


def test_publication_gate_rejects_custom_tagged_issue_form_scalar(
    tmp_path: Path,
) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    bug_form = source / ".github/ISSUE_TEMPLATE/bug.yml"
    bug_form.write_text(
        bug_form.read_text(encoding="utf-8").replace(
            "name: Bug report", "name: !unsafe Bug report"
        ),
        encoding="utf-8",
    )

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert "unsupported YAML tag" in result.stderr


@pytest.mark.parametrize(
    ("target", "replacement"),
    [
        ("name: Bug report", "!unsafe\nname: Bug report"),
        ("body:\n", "body: !unsafe\n"),
        ("      options:\n", "      options: !unsafe\n"),
    ],
    ids=("root-map", "body-sequence", "options-sequence"),
)
def test_publication_gate_rejects_custom_tagged_issue_form_collections(
    tmp_path: Path,
    target: str,
    replacement: str,
) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    bug_form = source / ".github/ISSUE_TEMPLATE/bug.yml"
    bug_form.write_text(
        bug_form.read_text(encoding="utf-8").replace(target, replacement, 1),
        encoding="utf-8",
    )

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert "unsupported YAML tag" in result.stderr


def test_publication_gate_rejects_unsafe_bug_privacy_checkbox(tmp_path: Path) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    bug_form = source / ".github/ISSUE_TEMPLATE/bug.yml"
    bug_form.write_text(
        bug_form.read_text(encoding="utf-8").replace(
            "I removed credentials, private paths, and private infrastructure details.",
            "I included credentials and private paths for debugging.",
        ),
        encoding="utf-8",
    )

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert "privacy checkbox" in result.stderr


def test_publication_gate_rejects_optional_proposal_readiness(tmp_path: Path) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    proposal = source / ".github/ISSUE_TEMPLATE/proposal.yml"
    proposal.write_text(
        proposal.read_text(encoding="utf-8").replace(
            "I will wait for an accepted Issue before starting implementation.\n"
            "          required: true",
            "I will wait for an accepted Issue before starting implementation.\n"
            "          required: false",
        ),
        encoding="utf-8",
    )

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert "proposal readiness" in result.stderr


def test_ci_smoke_tests_installed_artifact_help_and_version() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "uv run --isolated --no-project --with dist/*.whl fangorn --help" in workflow
    assert (
        "uv run --isolated --no-project --with dist/*.whl fangorn --version" in workflow
    )
    assert (
        "uv run --isolated --no-project --with dist/*.tar.gz fangorn --help" in workflow
    )
    assert (
        "uv run --isolated --no-project --with dist/*.tar.gz fangorn --version"
        in workflow
    )
    assert (
        "uv run --isolated --no-project --with dist/*.whl fangorn workspace create"
        in workflow
    )
    assert (
        "uv run --isolated --no-project --with dist/*.tar.gz fangorn workspace create"
        in workflow
    )


def test_publication_gate_accepts_exact_release_artifact_set(tmp_path: Path) -> None:
    artifacts = write_valid_artifact_set(tmp_path)

    result = run_publication_gate(*artifacts)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Publication checks passed: source tree and 2 artifact(s)\n"
    assert result.stderr == ""


@pytest.mark.parametrize(
    "dependency",
    [
        "click @ https://example.invalid/click.whl",
        "click[extra]>=8.1.8,<9",
        "click>=8.1.8,<9; python_version >= '3.12'",
        "click>=9",
    ],
)
def test_publication_gate_rejects_nonexact_source_click_requirement(
    tmp_path: Path,
    dependency: str,
) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    pyproject = source / "pyproject.toml"
    content = pyproject.read_text(encoding="utf-8")
    content = content.replace(
        f'dependencies = ["{EXACT_CLICK_REQUIREMENT}"]',
        f'dependencies = ["{dependency}"]',
    )
    pyproject.write_text(content, encoding="utf-8")

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "exactly click>=8.1.8,<9" in result.stderr


@pytest.mark.parametrize(
    "dependency",
    [
        "click @ https://example.invalid/click.whl",
        "click[extra]>=8.1.8,<9",
        "click>=8.1.8,<9; python_version >= '3.12'",
        "click>=9",
    ],
)
def test_publication_gate_rejects_nonexact_artifact_click_requirement(
    tmp_path: Path,
    dependency: str,
) -> None:
    wheel, sdist = write_valid_artifact_set(tmp_path)
    write_test_wheel(wheel, dependency=dependency)

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "exact Requires-Dist" in result.stderr


def test_publication_gate_requires_one_wheel_and_one_sdist(tmp_path: Path) -> None:
    wheel, _sdist = write_valid_artifact_set(tmp_path)

    result = run_publication_gate(wheel)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "exactly one wheel and one source distribution" in result.stderr


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
def test_publication_gate_rejects_wrong_artifact_filename_version(
    tmp_path: Path,
    artifact_kind: str,
) -> None:
    wheel, sdist = write_valid_artifact_set(tmp_path)
    if artifact_kind == "wheel":
        wheel = tmp_path / "fangorn_cli-0.2.0-py3-none-any.whl"
        write_test_wheel(wheel)
    else:
        sdist = tmp_path / "fangorn_cli-0.2.0.tar.gz"
        write_test_sdist(sdist)

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "artifact filename" in result.stderr


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
def test_publication_gate_rejects_wrong_artifact_metadata_version(
    tmp_path: Path,
    artifact_kind: str,
) -> None:
    wheel, sdist = write_valid_artifact_set(tmp_path)
    if artifact_kind == "wheel":
        write_test_wheel(wheel, metadata_version="0.2.0")
    else:
        write_test_sdist(sdist, metadata_version="0.2.0")

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "metadata version" in result.stderr


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Name", "fangorn-cli"),
        ("Version", PROJECT_VERSION),
        ("License-Expression", "MIT"),
        ("Requires-Python", ">=3.12"),
        ("Requires-Dist", EXACT_CLICK_REQUIREMENT),
    ],
)
def test_publication_gate_rejects_duplicate_singleton_metadata_fields(
    tmp_path: Path,
    artifact_kind: str,
    field: str,
    value: str,
) -> None:
    wheel, sdist = write_valid_artifact_set(tmp_path)
    metadata = project_metadata().replace(
        f"{field}: {value}\n",
        f"{field}: unexpected\n{field}: {value}\n",
    )
    if artifact_kind == "wheel":
        write_test_wheel(wheel, metadata_content=metadata)
    else:
        write_test_sdist(sdist, metadata_content=metadata)

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "metadata" in result.stderr.lower()


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
def test_publication_gate_rejects_folded_metadata_additions(
    tmp_path: Path,
    artifact_kind: str,
) -> None:
    wheel, sdist = write_valid_artifact_set(tmp_path)
    metadata = project_metadata().replace(
        f"Requires-Dist: {EXACT_CLICK_REQUIREMENT}\n",
        f"Requires-Dist: {EXACT_CLICK_REQUIREMENT}\n unexpected\n",
    )
    if artifact_kind == "wheel":
        write_test_wheel(wheel, metadata_content=metadata)
    else:
        write_test_sdist(sdist, metadata_content=metadata)

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Requires-Dist" in result.stderr


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
def test_publication_gate_rejects_duplicate_project_url_labels(
    tmp_path: Path,
    artifact_kind: str,
) -> None:
    wheel, sdist = write_valid_artifact_set(tmp_path)
    homepage = "https://github.com/iurimadeira/fangorn"
    metadata = project_metadata().replace(
        f"Project-URL: Homepage, {homepage}\n",
        "Project-URL: Homepage, https://unexpected.example.invalid/fangorn\n"
        f"Project-URL: Homepage, {homepage}\n",
    )
    if artifact_kind == "wheel":
        write_test_wheel(wheel, metadata_content=metadata)
    else:
        write_test_sdist(sdist, metadata_content=metadata)

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "project URLs do not match" in result.stderr


def test_publication_gate_rejects_stale_extra_artifact(tmp_path: Path) -> None:
    wheel, sdist = write_valid_artifact_set(tmp_path)
    stale = tmp_path / "fangorn_cli-0.1.0.post1-py3-none-any.whl"
    write_test_wheel(stale)

    result = run_publication_gate(wheel, sdist, stale)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "exactly one wheel and one source distribution" in result.stderr


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
def test_publication_gate_rejects_license_basename_collisions(
    tmp_path: Path,
    artifact_kind: str,
) -> None:
    wheel, sdist = write_valid_artifact_set(tmp_path)
    if artifact_kind == "wheel":
        root = f"fangorn_cli-{PROJECT_VERSION}.dist-info/licenses"
        write_test_wheel(
            wheel,
            license_entries={
                f"{root}/LICENSE": b"",
                f"{root}/THIRD_PARTY_NOTICES.md": NOTICES_BYTES,
                "unrelated/LICENSE": LICENSE_BYTES,
            },
        )
    else:
        root = f"fangorn_cli-{PROJECT_VERSION}"
        write_test_sdist(
            sdist,
            license_entries={
                f"{root}/LICENSE": b"",
                f"{root}/THIRD_PARTY_NOTICES.md": NOTICES_BYTES,
                "unrelated/LICENSE": LICENSE_BYTES,
            },
        )

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "LICENSE does not match source" in result.stderr


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize("noncanonical", ["payload/./file.py", "payload//file.py"])
def test_publication_gate_rejects_noncanonical_archive_member_paths(
    tmp_path: Path,
    artifact_kind: str,
    noncanonical: str,
) -> None:
    wheel, sdist = write_valid_artifact_set(tmp_path)
    if artifact_kind == "wheel":
        write_test_wheel(wheel, extra_entries=((noncanonical, b"value = 1\n"),))
    else:
        write_test_sdist(sdist, extra_entries=((noncanonical, b"value = 1\n"),))

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Archive path is not canonical" in result.stderr


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
def test_publication_gate_rejects_duplicate_canonical_license_paths(
    tmp_path: Path,
    artifact_kind: str,
) -> None:
    wheel, sdist = write_valid_artifact_set(tmp_path)
    if artifact_kind == "wheel":
        license_name = f"fangorn_cli-{PROJECT_VERSION}.dist-info/licenses/LICENSE"
        with pytest.warns(UserWarning, match="Duplicate name"):
            write_test_wheel(
                wheel,
                extra_entries=((license_name, LICENSE_BYTES),),
            )
    else:
        license_name = f"fangorn_cli-{PROJECT_VERSION}/LICENSE"
        write_test_sdist(
            sdist,
            extra_entries=((license_name, LICENSE_BYTES),),
        )

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Archive contains duplicate file path" in result.stderr


@pytest.mark.parametrize(
    "relative_name",
    [
        ".coverage-data/sensitive.pem",
        "dist/sensitive.pem",
        "nested/build/sensitive.pem",
        ".venv/sensitive.pem",
        ".pytest_cache/line\nansi\x1b\u202esensitive.pem",
    ],
)
def test_publication_gate_scans_force_tracked_files_in_excluded_directories(
    tmp_path: Path,
    relative_name: str,
) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    sensitive = source / relative_name
    sensitive.parent.mkdir(parents=True, exist_ok=True)
    sensitive.write_text("sensitive\n", encoding="utf-8")
    git(source, "add", "-f", "--", relative_name)

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Sensitive file type included" in result.stderr
    if "\n" in relative_name:
        assert result.stderr.count("\n") == 1
        assert "\\x0a" in result.stderr
        assert "\\x1b" in result.stderr
        assert "\\u202e" in result.stderr
        assert "\x1b" not in result.stderr
        assert "\u202e" not in result.stderr


def test_publication_gate_skips_untracked_local_review_context(tmp_path: Path) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    context = source / ".hunk" / "agent-context.json"
    context.parent.mkdir()
    context.write_text(
        '{"path":"/' + 'home/private-user/worktree"}\n',
        encoding="utf-8",
    )

    result = run_publication_gate(source=source)

    assert result.returncode == 0, result.stderr


def test_publication_gate_rejects_tracked_local_review_context(tmp_path: Path) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    context = source / ".hunk" / "agent-context.json"
    context.parent.mkdir()
    context.write_text("local review notes\n", encoding="utf-8")
    git(source, "add", "-f", "--", ".hunk/agent-context.json")

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert "Sensitive file included" in result.stderr


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
def test_publication_gate_rejects_archived_local_review_context(
    tmp_path: Path, artifact_kind: str
) -> None:
    wheel, sdist = write_valid_artifact_set(tmp_path)
    entry = (".hunk/agent-context.json", b"local review notes\n")
    if artifact_kind == "wheel":
        write_test_wheel(wheel, extra_entries=(entry,))
    else:
        write_test_sdist(sdist, extra_entries=(entry,))

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert "Sensitive file included" in result.stderr


@pytest.mark.parametrize("identifier", PRIVATE_INFRASTRUCTURE_IDENTIFIERS)
def test_publication_gate_rejects_private_infrastructure_identifiers_in_source(
    tmp_path: Path, identifier: str
) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    payload = source / "private-identifier.txt"
    payload.write_text(f"endpoint={identifier}\n", encoding="utf-8")
    git(source, "add", "--", payload.name)

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert "Forbidden private infrastructure identifier" in result.stderr


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize("identifier", PRIVATE_INFRASTRUCTURE_IDENTIFIERS)
def test_publication_gate_rejects_private_infrastructure_identifiers_in_artifacts(
    tmp_path: Path, artifact_kind: str, identifier: str
) -> None:
    wheel, sdist = write_valid_artifact_set(tmp_path)
    payload = f"endpoint={identifier}\n".encode()
    if artifact_kind == "wheel":
        write_test_wheel(wheel, payload=payload)
    else:
        write_test_sdist(sdist, extra_entries=(("private-identifier.txt", payload),))

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert "Forbidden private infrastructure identifier" in result.stderr


@pytest.mark.parametrize("identifier", PRIVATE_INFRASTRUCTURE_IDENTIFIERS)
def test_publication_gate_rejects_private_infrastructure_identifiers_in_source_paths(
    tmp_path: Path, identifier: str
) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    payload = source / "docs" / f"{identifier}.txt"
    payload.write_text("public text\n", encoding="utf-8")
    git(source, "add", "--", str(payload.relative_to(source)))

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert "Forbidden private infrastructure identifier" in result.stderr


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize("identifier", PRIVATE_INFRASTRUCTURE_IDENTIFIERS)
def test_publication_gate_rejects_private_infrastructure_identifiers_in_archive_paths(
    tmp_path: Path, artifact_kind: str, identifier: str
) -> None:
    wheel, sdist = write_valid_artifact_set(tmp_path)
    entry = (f"docs/{identifier}.txt", b"public text\n")
    if artifact_kind == "wheel":
        write_test_wheel(wheel, extra_entries=(entry,))
    else:
        write_test_sdist(sdist, extra_entries=(entry,))

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert "Forbidden private infrastructure identifier" in result.stderr


@pytest.mark.parametrize("separator", ["-", "_"])
def test_publication_gate_rejects_delimited_private_identifier_in_source(
    tmp_path: Path, separator: str
) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    identifier = "lab-" + "tmux"
    payload = source / "private-identifier.txt"
    payload.write_text(
        f"value=public{separator}{identifier}{separator}notes\n",
        encoding="utf-8",
    )
    git(source, "add", "--", payload.name)

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert "Forbidden private infrastructure identifier" in result.stderr


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize("separator", ["-", "_"])
def test_publication_gate_rejects_delimited_private_identifier_in_archive_path(
    tmp_path: Path, artifact_kind: str, separator: str
) -> None:
    wheel, sdist = write_valid_artifact_set(tmp_path)
    identifier = "lab-" + "tmux"
    entry = (f"public{separator}{identifier}{separator}notes.txt", b"public text\n")
    if artifact_kind == "wheel":
        write_test_wheel(wheel, extra_entries=(entry,))
    else:
        write_test_sdist(sdist, extra_entries=(entry,))

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert "Forbidden private infrastructure identifier" in result.stderr


@pytest.mark.parametrize(
    "relative_name",
    [".hunk\\agent-context.json", "nested/.hunk\\agent-context.json"],
)
def test_publication_gate_rejects_backslash_local_review_context_in_source(
    tmp_path: Path, relative_name: str
) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    context = source / relative_name
    context.parent.mkdir(exist_ok=True)
    context.write_text("local review notes\n", encoding="utf-8")
    git(source, "add", "--", relative_name)

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert "Sensitive file included" in result.stderr


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "relative_name",
    [".hunk\\agent-context.json", "nested/.hunk\\agent-context.json"],
)
def test_publication_gate_rejects_backslash_local_review_context_in_artifacts(
    tmp_path: Path, artifact_kind: str, relative_name: str
) -> None:
    wheel, sdist = write_valid_artifact_set(tmp_path)
    entry = (relative_name, b"local review notes\n")
    if artifact_kind == "wheel":
        write_test_wheel(wheel, extra_entries=(entry,))
    else:
        write_test_sdist(sdist, extra_entries=(entry,))

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert "backslash" in result.stderr or "Sensitive file included" in result.stderr


@pytest.mark.parametrize("private_path", PRIVATE_PATHS)
def test_publication_gate_rejects_private_paths_in_source(
    tmp_path: Path, private_path: str
) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    payload = source / "private-path.txt"
    payload.write_text(f"path={private_path}\n", encoding="utf-8")
    git(source, "add", "--", payload.name)

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert "Private data pattern found" in result.stderr


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize("private_path", PRIVATE_PATHS)
def test_publication_gate_rejects_private_paths_in_artifacts(
    tmp_path: Path, artifact_kind: str, private_path: str
) -> None:
    wheel, sdist = write_valid_artifact_set(tmp_path)
    payload = f"path={private_path}\n".encode()
    if artifact_kind == "wheel":
        write_test_wheel(wheel, payload=payload)
    else:
        write_test_sdist(sdist, extra_entries=(("private-path.txt", payload),))

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert "Private data pattern found" in result.stderr


def test_publication_gate_renders_dynamic_error_paths_on_one_safe_line(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "fangorn_cli-0.1.0-control\nansi\x1b\u202e-py3-none-any.whl"
    artifact.write_text("unsupported\n", encoding="utf-8")
    sdist = tmp_path / f"fangorn_cli-{PROJECT_VERSION}.tar.gz"
    write_test_sdist(sdist)

    result = run_publication_gate(artifact, sdist)

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr.count("\n") == 1
    assert "\\x0a" in result.stderr
    assert "\\x1b" in result.stderr
    assert "\\u202e" in result.stderr
    assert "\x1b" not in result.stderr
    assert "\u202e" not in result.stderr


@pytest.mark.parametrize("header", PRIVATE_KEY_HEADERS)
def test_publication_gate_rejects_private_key_headers_in_tracked_source(
    tmp_path: Path,
    header: str,
) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    payload = source / "private-key-header.txt"
    payload.write_text(f"{header}\n", encoding="utf-8")
    git(source, "add", "--", payload.name)

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Private key material found" in result.stderr


@pytest.mark.parametrize("header", PRIVATE_KEY_HEADERS)
def test_publication_gate_rejects_private_key_headers_in_artifacts(
    tmp_path: Path,
    header: str,
) -> None:
    wheel = tmp_path / "fangorn_cli-0.1.0-py3-none-any.whl"
    write_test_wheel(wheel, payload=f"{header}\n".encode())
    sdist = tmp_path / f"fangorn_cli-{PROJECT_VERSION}.tar.gz"
    write_test_sdist(sdist)

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Private key material found" in result.stderr


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        (b"value = b'bad\\0payload'\0\n", "Unexpected binary content"),
        (b"value = '\xff'\n", "Content is not valid UTF-8"),
    ],
)
def test_publication_gate_rejects_non_text_distribution_entries(
    tmp_path: Path,
    payload: bytes,
    expected_error: str,
) -> None:
    wheel = tmp_path / "fangorn_cli-0.1.0-py3-none-any.whl"
    write_test_wheel(wheel, payload=payload)
    sdist = tmp_path / f"fangorn_cli-{PROJECT_VERSION}.tar.gz"
    write_test_sdist(sdist)

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert result.stdout == ""
    assert expected_error in result.stderr


def test_publication_gate_rejects_unexpected_project_metadata_url(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "fangorn_cli-0.1.0-py3-none-any.whl"
    write_test_wheel(
        wheel,
        homepage="https://unexpected.example.invalid/fangorn",
    )
    sdist = tmp_path / f"fangorn_cli-{PROJECT_VERSION}.tar.gz"
    write_test_sdist(sdist)

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "project URLs do not match" in result.stderr


@pytest.mark.parametrize(
    "token",
    [
        "github_" + "pat_" + "11AA22BB33CC44DD55EE66FF77GG88HH",
        "sk-" + "proj-" + "11AA22BB33CC44DD55EE66FF77GG88HH",
    ],
)
def test_publication_gate_rejects_current_credential_formats_in_source(
    tmp_path: Path,
    token: str,
) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    payload = source / "credential.txt"
    payload.write_text(f"value={token}\n", encoding="utf-8")
    git(source, "add", "--", payload.name)

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Private data pattern found" in result.stderr


@pytest.mark.parametrize(
    "token",
    [
        "github_" + "pat_" + "11AA22BB33CC44DD55EE66FF77GG88HH",
        "sk-" + "proj-" + "11AA22BB33CC44DD55EE66FF77GG88HH",
    ],
)
def test_publication_gate_rejects_current_credential_formats_in_artifacts(
    tmp_path: Path,
    token: str,
) -> None:
    wheel = tmp_path / "fangorn_cli-0.1.0-py3-none-any.whl"
    write_test_wheel(wheel, payload=f"value={token}\n".encode())
    sdist = tmp_path / f"fangorn_cli-{PROJECT_VERSION}.tar.gz"
    write_test_sdist(sdist)

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Private data pattern found" in result.stderr


def test_publication_gate_preserves_archive_io_causes(tmp_path: Path) -> None:
    malformed = tmp_path / "fangorn_cli-0.1.0-py3-none-any.whl"
    malformed.write_text("not a zip archive\n", encoding="utf-8")
    sdist = tmp_path / f"fangorn_cli-{PROJECT_VERSION}.tar.gz"
    write_test_sdist(sdist)
    malformed_result = run_publication_gate(malformed, sdist)
    assert malformed_result.returncode != 0
    assert "Malformed wheel archive" in malformed_result.stderr

    malformed.unlink()
    unreadable = malformed
    unreadable.mkdir()
    unreadable_result = run_publication_gate(unreadable, sdist)
    assert unreadable_result.returncode != 0
    assert "Cannot read wheel" in unreadable_result.stderr
    assert "Malformed wheel" not in unreadable_result.stderr


@pytest.mark.parametrize(
    ("encrypted", "compression", "expected"),
    [
        (True, None, "Encrypted wheel member"),
        (False, 99, "Unsupported compression for wheel member"),
    ],
)
def test_wheel_member_read_failures_are_translated(
    tmp_path: Path,
    encrypted: bool,
    compression: int | None,
    expected: str,
) -> None:
    wheel = tmp_path / "fangorn_cli-0.1.0-py3-none-any.whl"
    write_test_wheel(wheel)
    alter_first_wheel_member(
        wheel,
        encrypted=encrypted,
        compression=compression,
    )
    sdist = tmp_path / f"fangorn_cli-{PROJECT_VERSION}.tar.gz"
    write_test_sdist(sdist)

    result = run_publication_gate(wheel, sdist)

    assert result.returncode != 0
    assert result.stdout == ""
    assert expected in result.stderr
    assert result.stderr.count("\n") == 1
    assert "Traceback" not in result.stderr
