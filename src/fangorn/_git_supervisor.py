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
    anchor_control = int(sys.argv[5])
    os.fstat(liveness)
    process_group = int(sys.argv[6])
    inherited = tuple(int(value) for value in sys.argv[7].split(",") if value)
    working_directory = int(sys.argv[8])
    finish_on_owner_exit = sys.argv[9] == "finish"
    timeout = int(sys.argv[10])
    output_limit = int(sys.argv[11])
    try:
        try:
            result = _supervise(
                control,
                status,
                liveness,
                process_group,
                inherited,
                working_directory,
                finish_on_owner_exit,
                timeout,
                output_limit,
                sys.argv[12:],
            )
        except OSError as error:
            with suppress(OSError):
                os.write(status, f"!{error.errno}\n".encode("ascii"))
                os.close(status)
            return 127
        with suppress(BrokenPipeError):
            os.write(completion, f"{result}\n".encode("ascii"))
        os.close(completion)
        completion = -1
        return result
    finally:
        with suppress(OSError):
            os.close(anchor_control)
        if completion >= 0:
            with suppress(OSError):
                os.close(completion)


def _supervise(
    control: int,
    status: int,
    liveness: int,
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
        pass_fds=(liveness, *inherited),
        process_group=process_group,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
    if child.stdout is None or child.stderr is None:
        raise RuntimeError("Git capture pipes are unavailable")
    captures = {
        child.stdout.fileno(): [1, 0],
        child.stderr.fileno(): [2, 0],
    }
    deadline = time.monotonic() + timeout
    while True:
        if time.monotonic() >= deadline:
            _close_captures(captures)
            _drain(child, process_group)
            _replace_output("Git operation exceeded one hour")
            return 124
        if not _child_running(child):
            break
        readable, _, _ = select.select((control, *captures), (), (), 0.01)
        if _forward_captures(readable, captures, output_limit):
            _close_captures(captures)
            _drain(child, process_group)
            _replace_output("Git diagnostic output exceeded 8 MiB")
            return 124
        if not readable:
            continue
        if control not in readable:
            continue
        command_byte = os.read(control, 1)
        if not command_byte:
            (_finish if finish_on_owner_exit else _drain)(child, process_group)
            if _finish_captures(captures, output_limit):
                _replace_output("Git diagnostic output exceeded 8 MiB")
                return 124
            return child.returncode
        if command_byte == b"c":
            _drain(child, process_group)
            if _finish_captures(captures, output_limit):
                _replace_output("Git diagnostic output exceeded 8 MiB")
                return 124
            return child.returncode
        if command_byte == b"f":
            _finish(child, process_group)
            if _finish_captures(captures, output_limit):
                _replace_output("Git diagnostic output exceeded 8 MiB")
                return 124
            return child.returncode
    _drain(child, process_group)
    if _finish_captures(captures, output_limit):
        _replace_output("Git diagnostic output exceeded 8 MiB")
        return 124
    if time.monotonic() >= deadline:
        _replace_output("Git operation exceeded one hour")
        return 124
    return child.returncode


def _forward_captures(
    readable: list[int], captures: dict[int, list[int]], limit: int
) -> bool:
    exceeded = False
    for descriptor in set(readable) & captures.keys():
        chunk = os.read(descriptor, 65536)
        if not chunk:
            del captures[descriptor]
            continue
        destination, written = captures[descriptor]
        remaining = max(0, limit - written)
        retained = chunk[:remaining]
        while retained:
            count = os.write(destination, retained)
            retained = retained[count:]
        captures[descriptor][1] += min(len(chunk), remaining)
        exceeded |= len(chunk) > remaining
    return exceeded


def _finish_captures(captures: dict[int, list[int]], limit: int) -> bool:
    exceeded = False
    while captures:
        readable, _, _ = select.select(tuple(captures), (), ())
        exceeded |= _forward_captures(readable, captures, limit)
    return exceeded


def _close_captures(captures: dict[int, list[int]]) -> None:
    for descriptor in captures:
        os.close(descriptor)
    captures.clear()


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
        timeout=1,
    )
    running = False
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
            raise OSError("Cannot parse process-group state")
        if int(fields[1]) == process_group and not fields[2].startswith("Z"):
            running = True
    return running


if __name__ == "__main__":
    raise SystemExit(main())
