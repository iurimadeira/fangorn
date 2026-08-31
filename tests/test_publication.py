from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_publication_gate_accepts_the_public_source_tree() -> None:
    result = subprocess.run(
        [
            sys.executable,
            PROJECT_ROOT / "scripts" / "check_publication.py",
            "--source",
            PROJECT_ROOT,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Publication checks passed: source tree\n"
    assert result.stderr == ""
