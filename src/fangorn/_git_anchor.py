from __future__ import annotations

import os
import select
import signal
import sys
import time


def main() -> int:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    control = int(sys.argv[1])
    liveness = int(sys.argv[2])
    timeout = int(sys.argv[3])
    os.fstat(liveness)
    if os.read(control, 1) != b"a":
        return 1
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        readable, _, _ = select.select((control,), (), (), remaining)
        if readable and not os.read(control, 1):
            break
    os.killpg(os.getpgrp(), signal.SIGKILL)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
