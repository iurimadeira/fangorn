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
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    control = int(sys.argv[1])
    status = int(sys.argv[2])
    completion = int(sys.argv[3])
    liveness = int(sys.argv[4])
    os.fstat(liveness)
    inherited = tuple(int(value) for value in sys.argv[5].split(",") if value)
    working_directory = int(sys.argv[6])
    finish_on_owner_exit = sys.argv[7] == "finish"
    try:
        result = _supervise(
            control,
            status,
            inherited,
            working_directory,
            finish_on_owner_exit,
            sys.argv[8:],
        )
        with suppress(BrokenPipeError):
            os.write(completion, f"{result}\n".encode("ascii"))
        return result
    finally:
        os.close(completion)


def _supervise(
    control: int,
    status: int,
    inherited: tuple[int, ...],
    working_directory: int,
    finish_on_owner_exit: bool,
    command: list[str],
) -> int:
    child = subprocess.Popen(  # noqa: S603 -- caller supplies Fangorn's fixed Git argv
        command,
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
        command_byte = os.read(control, 1)
        if not command_byte:
            (_finish if finish_on_owner_exit else _drain)(child)
            return child.returncode
        if command_byte == b"c":
            _drain(child)
            return child.returncode
        if command_byte == b"f":
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
    process_group = os.getpgrp()
    members = _process_group_members(process_group)
    if not members:
        child.wait()
        return
    _signal_processes(members, signal.SIGTERM)
    terminated = members
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        members = _process_group_members(process_group)
        if not members:
            child.wait()
            return
        _signal_processes(members - terminated, signal.SIGTERM)
        terminated |= members
        time.sleep(0.01)
    while members := _process_group_members(process_group):
        _signal_processes(members, signal.SIGKILL)
        time.sleep(0.01)
    child.wait()


def _signal_processes(processes: set[int], requested_signal: signal.Signals) -> None:
    for process in processes:
        with suppress(ProcessLookupError):
            os.kill(process, requested_signal)


def _child_running(child: subprocess.Popen[bytes]) -> bool:
    return (
        os.waitid(
            os.P_PID,
            child.pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
        is None
    )


def _process_group_members(process_group: int) -> set[int]:
    proc = Path("/proc")
    if proc.is_dir():
        parsed = False
        members: set[int] = set()
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
                    members.add(int(entry.name))
            except (IndexError, OSError, ValueError):
                continue
        if parsed:
            members.discard(os.getpid())
            return members
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,pgid=,state="],
        check=True,
        capture_output=True,
        env={"LANG": "C", "PATH": "/usr/bin:/bin"},
        start_new_session=True,
        text=True,
    )
    members = {
        int(fields[0])
        for line in result.stdout.splitlines()
        if len(fields := line.split()) == 3
        and fields[0].isdigit()
        and fields[1].isdigit()
        and int(fields[1]) == process_group
        and not fields[2].startswith("Z")
    }
    members.discard(os.getpid())
    return members


def _process_group_running(process_group: int) -> bool:
    return bool(_process_group_members(process_group))


if __name__ == "__main__":
    raise SystemExit(main())
