from __future__ import annotations

import fcntl
import os
import signal
import stat
import sys
from contextlib import suppress
from pathlib import Path


def main() -> int:
    descriptor = int(sys.argv[1])
    marker = Path(sys.argv[2])
    ready = int(sys.argv[3])
    for sent in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sent, signal.SIG_IGN)
    signal.pthread_sigmask(
        signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    )
    opened = os.fstat(descriptor)
    os.write(ready, b"r\n")
    os.close(ready)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        current = marker.stat(follow_symlinks=False)
        if (
            stat.S_ISREG(current.st_mode)
            and opened.st_dev == current.st_dev
            and opened.st_ino == current.st_ino
        ):
            marker.unlink()
    except FileNotFoundError:
        pass
    finally:
        os.close(descriptor)
    return 0


if __name__ == "__main__":
    with suppress(BrokenPipeError):
        raise SystemExit(main())
