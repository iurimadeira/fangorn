from __future__ import annotations

import os
import subprocess
from pathlib import Path


def git_environment(root: Path) -> dict[str, str]:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(root / ".git-fixture-home"),
            "XDG_CONFIG_HOME": str(root / ".git-fixture-config"),
        }
    )
    return environment


def initialize_repository(path: Path) -> None:
    path.mkdir(exist_ok=True)
    template = path.parent / ".git-fixture-template"
    template.mkdir(exist_ok=True)
    subprocess.run(
        [
            "git",
            "init",
            "--initial-branch=main",
            f"--template={template}",
            path,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=git_environment(path.parent),
    )
    git(path, "config", "user.name", "Fangorn Test")
    git(path, "config", "user.email", "fangorn@example.invalid")


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", repository, *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=git_environment(repository.parent),
    )
    return result.stdout.strip()
