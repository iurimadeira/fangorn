from __future__ import annotations

import re
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_readme_documents_public_facade_without_legacy_adopt_flow() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert "fangorn.workspaces.Workspaces" in readme
    assert "fangorn adopt" not in readme
    for marker in (
        "root headless Workspaces",
        "Python 3.12 or newer",
        "Create starts by default",
        "`--no-start` provisions",
        "Equivalent retries return the same Workspace ID",
        "recoverable fenced leases",
    ):
        assert marker in readme


def test_local_quality_configuration_matches_the_public_contract() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as file:
        pyproject = tomllib.load(file)

    dev_dependencies = pyproject["dependency-groups"]["dev"]
    pytest_options = pyproject["tool"]["pytest"]["ini_options"]
    ruff_lint = pyproject["tool"]["ruff"]["lint"]
    mypy = pyproject["tool"]["mypy"]
    coverage_run = pyproject["tool"]["coverage"]["run"]

    assert "coverage>=7.10,<8" in dev_dependencies
    assert "--strict-config" in pytest_options["addopts"]
    assert "--strict-markers" in pytest_options["addopts"]
    assert "S" in ruff_lint["select"]
    assert ruff_lint["per-file-ignores"] == {"tests/**": ["S101"]}
    assert mypy["files"] == ["src", "scripts", "tests"]
    assert mypy["warn_unreachable"] is True
    assert coverage_run == {
        "branch": True,
        "data_file": ".coverage-data/.coverage",
        "parallel": True,
        "patch": ["subprocess"],
        "relative_files": True,
        "source": ["src", "scripts"],
    }


def test_ci_exposes_one_stable_aggregate_result() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert (
        "group: ${{ github.workflow }}-"
        "${{ github.event.pull_request.number || github.run_id }}" in workflow
    )
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in workflow
    assert re.search(r"^  quality:\n(?:.*\n)*?    timeout-minutes: 15$", workflow, re.M)
    assert re.search(
        r"^  compatibility:\n(?:.*\n)*?    timeout-minutes: 15$", workflow, re.M
    )
    assert re.search(r"^  ci:\n(?:.*\n)*?    name: CI$", workflow, re.M)
    assert "needs: [quality, compatibility]" in workflow
    assert workflow.count("timeout-minutes: 15") == 3
    assert "if: ${{ always() }}" in workflow
    assert "[[ \"${{ needs.quality.result }}\" == 'success' ]]" in workflow
    assert "[[ \"${{ needs.compatibility.result }}\" == 'success' ]]" in workflow


def test_ci_runs_the_required_quality_and_compatibility_commands() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    for command in (
        "uv run coverage erase",
        "uv run coverage run --branch -m pytest",
        "uv run coverage combine",
        "uv run coverage report --fail-under=85.0",
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run mypy src scripts tests",
        "uv build",
        "uv run python scripts/check_publication.py --source . dist/*",
    ):
        assert command in workflow

    assert '- os: ubuntu-latest\n            python-version: "3.13"' in workflow
    assert '- os: ubuntu-latest\n            python-version: "3.14"' in workflow
    for version in ("3.12", "3.13", "3.14"):
        matrix_entry = f'- os: macos-latest\n            python-version: "{version}"'
        assert matrix_entry in workflow


def test_ci_pins_every_third_party_action_to_a_full_sha() -> None:
    workflow = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    revisions = re.findall(r"uses:\s+[^@\s]+@([^\s]+)", workflow)

    assert revisions
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in revisions)
