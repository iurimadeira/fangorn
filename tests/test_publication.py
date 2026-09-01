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


def run_publication_gate(
    *artifacts: Path,
    source: Path = PROJECT_ROOT,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
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
            ".git",
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
    license_entries: dict[str, bytes] | None = None,
) -> None:
    root = f"fangorn_cli-{PROJECT_VERSION}.dist-info"
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
        for name, content in license_entries.items():
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
    license_entries: dict[str, bytes] | None = None,
) -> None:
    root = f"fangorn_cli-{PROJECT_VERSION}"
    if license_entries is None:
        license_entries = {
            f"{root}/LICENSE": LICENSE_BYTES,
            f"{root}/THIRD_PARTY_NOTICES.md": NOTICES_BYTES,
        }
    entries = {
        f"{root}/PKG-INFO": project_metadata(
            version=metadata_version,
            dependency=dependency,
        ).encode(),
        f"{root}/fangorn/__init__.py": b"value = 1\n",
        **license_entries,
    }
    with tarfile.open(path, "w:gz") as archive:
        for name, content in entries.items():
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


def test_ci_smoke_tests_the_installed_source_distribution() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert (
        "uv run --isolated --no-project --with dist/*.tar.gz fangorn --help" in workflow
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


@pytest.mark.parametrize(
    "relative_name",
    [
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
