from __future__ import annotations

import ctypes
import errno
import os
import selectors
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

UNPROVEN_GROUP_TERMINATION = 256
PROC_PIDTBSDINFO = 3
SZOMB = 5


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = (
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    )


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
    with selectors.DefaultSelector() as selector:
        selector.register(control, selectors.EVENT_READ)
        for descriptor in captures:
            selector.register(descriptor, selectors.EVENT_READ)
        while True:
            if time.monotonic() >= deadline:
                _close_captures(captures, selector)
                return _limit_result(
                    _drain(child, process_group), "Git operation exceeded one hour"
                )
            if not _child_running(child):
                break
            readable = [key.fd for key, _events in selector.select(0.01)]
            if _forward_captures(readable, captures, output_limit, selector):
                _close_captures(captures, selector)
                return _limit_result(
                    _drain(child, process_group),
                    "Git diagnostic output exceeded 8 MiB",
                )
            if control not in readable:
                continue
            command_byte = os.read(control, 1)
            if not command_byte:
                stopped = (_finish if finish_on_owner_exit else _drain)(
                    child, process_group
                )
                if failure := _completion_failure(
                    stopped, captures, output_limit, deadline, selector
                ):
                    return failure
                return child.returncode
            if command_byte == b"c":
                stopped = _drain(child, process_group)
                if failure := _completion_failure(
                    stopped, captures, output_limit, deadline, selector
                ):
                    return failure
                return child.returncode
            if command_byte == b"f":
                stopped = _finish(child, process_group)
                if failure := _completion_failure(
                    stopped, captures, output_limit, deadline, selector
                ):
                    return failure
                return child.returncode
        stopped = _drain(child, process_group)
        if failure := _completion_failure(
            stopped, captures, output_limit, deadline, selector
        ):
            return failure
        if time.monotonic() >= deadline:
            _replace_output("Git operation exceeded one hour")
            return 124
        return child.returncode


def _limit_result(stopped: bool, message: str) -> int:
    if not stopped:
        _replace_output("Git process-group termination could not be confirmed")
        return UNPROVEN_GROUP_TERMINATION
    _replace_output(message)
    return 124


def _forward_captures(
    readable: list[int],
    captures: dict[int, list[int]],
    limit: int,
    selector: selectors.BaseSelector,
) -> bool:
    exceeded = False
    for descriptor in set(readable) & captures.keys():
        chunk = os.read(descriptor, 65536)
        if not chunk:
            selector.unregister(descriptor)
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


def _finish_captures(
    captures: dict[int, list[int]],
    limit: int,
    deadline: float,
    selector: selectors.BaseSelector | None = None,
) -> str:
    exceeded = False
    owned_selector = selector is None
    if selector is None:
        selector = selectors.DefaultSelector()
        for descriptor in captures:
            selector.register(descriptor, selectors.EVENT_READ)
    try:
        while captures:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _close_captures(captures, selector)
                return "timeout"
            readable = [key.fd for key, _events in selector.select(remaining)]
            if not readable:
                _close_captures(captures, selector)
                return "timeout"
            exceeded |= _forward_captures(readable, captures, limit, selector)
        return "exceeded" if exceeded else "ok"
    finally:
        if owned_selector:
            selector.close()


def _completion_failure(
    stopped: bool,
    captures: dict[int, list[int]],
    output_limit: int,
    deadline: float,
    selector: selectors.BaseSelector | None = None,
) -> int | None:
    if not stopped:
        _close_captures(captures, selector)
        _replace_output("Git process-group termination could not be confirmed")
        return UNPROVEN_GROUP_TERMINATION
    capture = _finish_captures(
        captures, output_limit, min(deadline, time.monotonic() + 2), selector
    )
    if capture == "ok":
        return None
    _replace_output(
        "Git diagnostic output exceeded 8 MiB"
        if capture == "exceeded"
        else "Git diagnostic capture did not close"
    )
    return 124


def _close_captures(
    captures: dict[int, list[int]], selector: selectors.BaseSelector | None = None
) -> None:
    for descriptor in captures:
        if selector is not None:
            selector.unregister(descriptor)
        os.close(descriptor)
    captures.clear()


def _replace_output(message: str) -> None:
    for descriptor in (1, 2):
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
    os.write(2, f"{message}\n".encode())


def _finish(child: subprocess.Popen[bytes], process_group: int) -> bool:
    deadline = time.monotonic() + 2
    while _child_running(child) and time.monotonic() < deadline:
        time.sleep(0.01)
    return _drain(child, process_group)


def _drain(child: subprocess.Popen[bytes], process_group: int) -> bool:
    with suppress(ProcessLookupError):
        os.killpg(process_group, signal.SIGTERM)
    deadline = time.monotonic() + 2
    if not _wait_for_group_state(process_group, deadline=deadline):
        child.wait()
        return True
    with suppress(ProcessLookupError):
        os.killpg(process_group, signal.SIGKILL)
    deadline = time.monotonic() + 2
    running = _wait_for_group_state(process_group, deadline=deadline)
    try:
        child.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return False
    return not running


def _wait_for_group_state(process_group: int, *, deadline: float | None = None) -> bool:
    if deadline is None:
        deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            if not _process_group_running(
                process_group,
                ignore_pid=process_group,
                timeout=max(0.01, min(1, deadline - time.monotonic())),
            ):
                return False
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    return True


def _child_running(child: subprocess.Popen[bytes]) -> bool:
    if hasattr(os, "waitid"):
        return (
            os.waitid(
                os.P_PID,
                child.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
            is None
        )
    if sys.platform == "darwin":
        return _darwin_child_running(child.pid)
    raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))


def _darwin_child_running(pid: int) -> bool:
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    proc_pidinfo = library.proc_pidinfo
    proc_pidinfo.argtypes = (
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    )
    proc_pidinfo.restype = ctypes.c_int
    info = _ProcBsdInfo()
    size = ctypes.sizeof(info)
    ctypes.set_errno(0)
    result = proc_pidinfo(
        pid,
        PROC_PIDTBSDINFO,
        0,
        ctypes.byref(info),
        size,
    )
    if result != size:
        code = ctypes.get_errno()
        if result <= 0 and code:
            raise OSError(code, os.strerror(code))
        raise OSError(errno.EIO, "Incomplete Darwin process information")
    if info.pbi_pid != pid:
        raise OSError(errno.EIO, "Mismatched Darwin process information")
    return int(info.pbi_status) != SZOMB


def _process_group_running(
    process_group: int, *, ignore_pid: int | None = None, timeout: float = 1
) -> bool:
    deadline = time.monotonic() + timeout
    proc = Path("/proc")
    if proc.is_dir():
        parsed = False
        complete = True
        for entry in proc.iterdir():
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("/proc process-group scan", timeout)
            if not entry.name.isdigit():
                continue
            try:
                fields = (
                    (entry / "stat")
                    .read_text(encoding="ascii")
                    .rpartition(")")[2]
                    .split()
                )
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired("/proc process-group scan", timeout)
                parsed = True
                if (
                    int(entry.name) != ignore_pid
                    and int(fields[2]) == process_group
                    and fields[0] != "Z"
                ):
                    return True
            except FileNotFoundError:
                pass
            except (IndexError, OSError, ValueError):
                complete = False
        if parsed and complete:
            return False
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired("process-group probe", timeout)
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,pgid=,state="],
        check=True,
        capture_output=True,
        env={"LANG": "C", "PATH": "/usr/bin:/bin"},
        start_new_session=True,
        text=True,
        timeout=remaining,
    )
    running = False
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
            raise OSError("Cannot parse process-group state")
        if (
            int(fields[0]) != ignore_pid
            and int(fields[1]) == process_group
            and not fields[2].startswith("Z")
        ):
            running = True
    return running


if __name__ == "__main__":
    raise SystemExit(main())
