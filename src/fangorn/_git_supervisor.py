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
    process_group = int(sys.argv[5])
    inherited = tuple(int(value) for value in sys.argv[6].split(",") if value)
    working_directory = int(sys.argv[7])
    finish_on_owner_exit = sys.argv[8] == "finish"
    timeout = int(sys.argv[9])
    output_limit = int(sys.argv[10])
    try:
        result = _supervise(
            control,
            status,
            process_group,
            inherited,
            working_directory,
            finish_on_owner_exit,
            timeout,
            output_limit,
            sys.argv[11:],
        )
        with suppress(BrokenPipeError):
            os.write(completion, f"{result}\n".encode("ascii"))
        return result
    finally:
        os.close(completion)


def _supervise(
    control: int,
    status: int,
    process_group: int,
    inherited: tuple[int, ...],
    working_directory: int,
    finish_on_owner_exit: bool,
    timeout: int,
    output_limit: int,
    command: list[str],
) -> int:
    child = subprocess.Popen(  # noqa: S603 -- caller supplies Fangorn's fixed Git argv
        command,
        pass_fds=inherited,
        process_group=process_group,
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
    deadline = time.monotonic() + timeout
    while True:
        if time.monotonic() >= deadline:
            _drain(child, process_group)
            _replace_output("Git operation exceeded one hour")
            return 124
        if max(os.fstat(1).st_size, os.fstat(2).st_size) > output_limit:
            _drain(child, process_group)
            _replace_output("Git diagnostic output exceeded 8 MiB")
            return 124
        if not _child_running(child):
            break
        readable, _, _ = select.select((control,), (), (), 0.01)
        if not readable:
            continue
        command_byte = os.read(control, 1)
        if not command_byte:
            (_finish if finish_on_owner_exit else _drain)(child, process_group)
            return child.returncode
        if command_byte == b"c":
            _drain(child, process_group)
            return child.returncode
        if command_byte == b"f":
            _finish(child, process_group)
            return child.returncode
    _drain(child, process_group)
    return child.returncode


def _replace_output(message: str) -> None:
    for descriptor in (1, 2):
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
    os.write(2, f"{message}\n".encode())


def _finish(child: subprocess.Popen[bytes], process_group: int) -> None:
    deadline = time.monotonic() + 2
    while _child_running(child) and time.monotonic() < deadline:
        time.sleep(0.01)
    _drain(child, process_group)


def _drain(child: subprocess.Popen[bytes], process_group: int) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process_group, signal.SIGTERM)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not _wait_for_group_state(process_group):
            child.wait()
            return
        time.sleep(0.1)
    with suppress(ProcessLookupError):
        os.killpg(process_group, signal.SIGKILL)
    while _wait_for_group_state(process_group):
        time.sleep(0.1)
    child.wait()


def _wait_for_group_state(process_group: int) -> bool:
    while True:
        try:
            return _process_group_running(process_group)
        except (OSError, subprocess.SubprocessError):
            time.sleep(0.1)


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
        complete = True
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
            except FileNotFoundError:
                pass
            except (IndexError, OSError, ValueError):
                complete = False
        if parsed and complete:
            return False
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,pgid=,state="],
        check=True,
        capture_output=True,
        env={"LANG": "C", "PATH": "/usr/bin:/bin"},
        start_new_session=True,
        text=True,
    )
    return any(
        len(fields := line.split()) == 3
        and fields[0].isdigit()
        and fields[1].isdigit()
        and int(fields[1]) == process_group
        and not fields[2].startswith("Z")
        for line in result.stdout.splitlines()
    )


if __name__ == "__main__":
    raise SystemExit(main())
