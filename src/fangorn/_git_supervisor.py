from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path


def main() -> int:
    control = int(sys.argv[1])
    status = int(sys.argv[2])
    int(sys.argv[3])
    child = subprocess.Popen(  # noqa: S603 -- caller supplies Fangorn's fixed Git argv
        sys.argv[4:], start_new_session=True
    )
    os.write(status, f"{child.pid}\n".encode("ascii"))
    os.close(status)
    finish = False
    while child.poll() is None:
        readable, _, _ = select.select((control,), (), (), 0.01)
        if not readable:
            continue
        command = os.read(control, 1)
        if not command or command == b"c":
            _drain(child)
            return child.returncode
        if command == b"f":
            finish = True
        if finish:
            continue
    _drain(child)
    return child.returncode


def _drain(child: subprocess.Popen[bytes]) -> None:
    child.poll()
    if not _process_group_running(child.pid):
        child.wait()
        return
    with suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGTERM)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        child.poll()
        if not _process_group_running(child.pid):
            child.wait()
            return
        time.sleep(0.01)
    with suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGKILL)
    child.wait()


def _process_group_running(process_group: int) -> bool:
    proc = Path("/proc")
    if proc.is_dir():
        parsed = False
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (
                    (entry / "stat")
                    .read_text(encoding="ascii")
                    .rpartition(")")[2]
                    .split()
                )
                parsed = True
                if int(fields[2]) == process_group and fields[0] != "Z":
                    return True
            except (IndexError, OSError, ValueError):
                continue
        if parsed:
            return False
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
