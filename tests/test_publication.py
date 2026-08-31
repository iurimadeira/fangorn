from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
    subprocess.run(
        ["git", "init", "--initial-branch=main", source],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", source, "add", "--all"],
        check=True,
        capture_output=True,
        text=True,
    )
    return source


def write_test_wheel(
    path: Path,
    *,
    payload: bytes = b"value = 1\n",
    homepage: str = "https://github.com/iurimadeira/fangorn",
) -> None:
    metadata = f"""Metadata-Version: 2.4
Name: fangorn-cli
Version: 0.1.0
License-Expression: MIT
Requires-Python: >=3.12
Requires-Dist: click>=8.1.8,<9
Project-URL: Homepage, {homepage}
Project-URL: Issues, https://github.com/iurimadeira/fangorn/issues

Fangorn
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("fangorn/__init__.py", payload)
        archive.writestr("fangorn_cli-0.1.0.dist-info/METADATA", metadata)
        archive.writestr("fangorn_cli-0.1.0.dist-info/licenses/LICENSE", "MIT\n")
        archive.writestr(
            "fangorn_cli-0.1.0.dist-info/licenses/THIRD_PARTY_NOTICES.md",
            "Click: BSD-3-Clause\n",
        )


def test_publication_gate_accepts_the_public_source_tree() -> None:
    result = run_publication_gate()

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Publication checks passed: source tree\n"
    assert result.stderr == ""
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)["project"]
    assert project["name"] == "fangorn-cli"
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "uv tool install fangorn-cli" in readme
    assert "pipx install fangorn-cli" in readme


@pytest.mark.parametrize(
    "relative_name",
    [
        "dist/sensitive.pem",
        "nested/build/sensitive.pem",
        ".venv/sensitive.pem",
        ".pytest_cache/line\nsensitive.pem",
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
    subprocess.run(
        ["git", "-C", source, "add", "-f", "--", relative_name],
        check=True,
        capture_output=True,
        text=True,
    )

    result = run_publication_gate(source=source)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Sensitive file type included" in result.stderr


@pytest.mark.parametrize("header", PRIVATE_KEY_HEADERS)
def test_publication_gate_rejects_private_key_headers_in_tracked_source(
    tmp_path: Path,
    header: str,
) -> None:
    source = copy_source_to_temporary_repository(tmp_path)
    payload = source / "private-key-header.txt"
    payload.write_text(f"{header}\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", source, "add", "--", payload.name],
        check=True,
        capture_output=True,
        text=True,
    )

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

    result = run_publication_gate(wheel)

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

    result = run_publication_gate(wheel)

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

    result = run_publication_gate(wheel)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "project URLs do not match" in result.stderr


def test_publication_gate_preserves_archive_io_causes(tmp_path: Path) -> None:
    malformed = tmp_path / "fangorn_cli-0.1.0-py3-none-any.whl"
    malformed.write_text("not a zip archive\n", encoding="utf-8")
    malformed_result = run_publication_gate(malformed)
    assert malformed_result.returncode != 0
    assert "Malformed wheel archive" in malformed_result.stderr

    unreadable = tmp_path / "fangorn_cli-0.1.1-py3-none-any.whl"
    write_test_wheel(unreadable)
    unreadable.chmod(0)
    try:
        unreadable_result = run_publication_gate(unreadable)
    finally:
        unreadable.chmod(0o600)
    assert unreadable_result.returncode != 0
    assert "Cannot read wheel" in unreadable_result.stderr
    assert "Permission denied" in unreadable_result.stderr
    assert "Malformed wheel" not in unreadable_result.stderr
