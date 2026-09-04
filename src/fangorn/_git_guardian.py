from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

PROBE_FAILURE_LIMIT = 20
QUIESCENCE_UNKNOWN = b"quiescence-unknown\n"


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
    delay = 0.01
    failures = 0
    scan_group = False
    cheap_probes = 0
    periodic_scan = False
    while True:
        try:
            if not scan_group:
                try:
                    os.kill(process_group, 0)
                except PermissionError:
                    pass
                except ProcessLookupError:
                    scan_group = True
                cheap_probes += 1
                periodic_scan = periodic_scan or cheap_probes >= 2
            if (scan_group or periodic_scan) and not _process_group_running(
                process_group
            ):
                return 0
            failures = 0
        except (OSError, subprocess.SubprocessError):
            failures += 1
            if failures >= PROBE_FAILURE_LIMIT and _persist_unknown(liveness):
                return 1
        time.sleep(delay)
        delay = min(0.25, delay * 2)


def _persist_unknown(descriptor: int) -> bool:
    try:
        os.ftruncate(descriptor, 0)
        written = 0
        while written < len(QUIESCENCE_UNKNOWN):
            count = os.pwrite(descriptor, QUIESCENCE_UNKNOWN[written:], written)
            if count <= 0:
                raise OSError("short write")
            written += count
        os.fsync(descriptor)
    except OSError:
        return False
    return True


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
