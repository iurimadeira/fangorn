from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path


def main() -> int:
    process_group = int(sys.argv[1])
    liveness = int(sys.argv[2])
    ready = int(sys.argv[3])
    for sent in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sent, signal.SIG_IGN)
    signal.pthread_sigmask(
        signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    )
    os.fstat(liveness)
    os.write(ready, b"r\n")
    os.close(ready)
    while True:
        try:
            if not _process_group_running(process_group):
                return 0
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(0.1)


def _process_group_running(process_group: int) -> bool:
    timeout = 1.0
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
                if int(fields[2]) == process_group and fields[0] != "Z":
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
        ["/bin/ps", "-axo", "pgid=,state="],
        check=True,
        capture_output=True,
        env={"LANG": "C", "PATH": "/usr/bin:/bin"},
        start_new_session=True,
        text=True,
        timeout=remaining,
    )
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[0].isdigit():
            raise OSError("Cannot parse process-group state")
        if int(fields[0]) == process_group and not fields[1].startswith("Z"):
            return True
    return False


if __name__ == "__main__":
    with suppress(BrokenPipeError):
        raise SystemExit(main())
