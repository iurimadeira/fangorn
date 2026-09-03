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
    liveness = int(sys.argv[3])
    os.fstat(liveness)
    inherited = tuple(int(value) for value in sys.argv[4].split(",") if value)
    working_directory = int(sys.argv[5])
    finish_on_owner_exit = sys.argv[6] == "finish"
    child = subprocess.Popen(  # noqa: S603 -- caller supplies Fangorn's fixed Git argv
        sys.argv[7:],
        start_new_session=True,
        pass_fds=inherited,
        preexec_fn=(
            (lambda: os.fchdir(working_directory)) if working_directory >= 0 else None
        ),
    )
    try:
        os.write(status, f"{child.pid}\n".encode("ascii"))
    except BrokenPipeError:
        pass
    finally:
        os.close(status)
    while _child_running(child):
        readable, _, _ = select.select((control,), (), (), 0.01)
        if not readable:
            continue
        command = os.read(control, 1)
        if not command:
            (_finish if finish_on_owner_exit else _drain)(child)
            return child.returncode
        if command == b"c":
            _drain(child)
            return child.returncode
        if command == b"f":
            _finish(child)
            return child.returncode
    _drain(child)
    return child.returncode


def _finish(child: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 2
    while _child_running(child) and time.monotonic() < deadline:
        time.sleep(0.01)
    _drain(child)


def _drain(child: subprocess.Popen[bytes]) -> None:
    if not _process_group_running(child.pid):
        child.wait()
        return
    with suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGTERM)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not _process_group_running(child.pid):
            child.wait()
            return
        time.sleep(0.01)
    with suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGKILL)
    child.wait()


def _child_running(child: subprocess.Popen[bytes]) -> bool:
    return (
        os.waitid(
            os.P_PID,
            child.pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
        is None
    )


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
