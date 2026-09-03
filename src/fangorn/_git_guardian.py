from __future__ import annotations

import os
import signal
import sys
import time
from contextlib import suppress


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
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return 0
        except PermissionError:
            pass
        time.sleep(0.1)


if __name__ == "__main__":
    with suppress(BrokenPipeError):
        raise SystemExit(main())
