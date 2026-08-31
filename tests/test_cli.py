from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def fangorn_executable() -> Path:
    executable = Path(sys.executable).with_name("fangorn")
    assert executable.is_file(), "fangorn console script is not installed"
    return executable


def test_help_exposes_bootstrap_commands() -> None:
    result = subprocess.run(
        [fangorn_executable(), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Worktree-native workspace families" in result.stdout
    assert "adopt" in result.stdout
    assert "info" in result.stdout
    assert "list" in result.stdout
