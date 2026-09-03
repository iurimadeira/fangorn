from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
from contextlib import suppress


def main() -> int:
    control = int(sys.argv[1])
    finish_on_parent_exit = sys.argv[2] == "finish"
    child = subprocess.Popen(  # noqa: S603 -- caller supplies Fangorn's fixed Git argv
        sys.argv[3:], start_new_session=True
    )
    while child.poll() is None:
        if control < 0:
            return child.wait()
        readable, _, _ = select.select((control,), (), (), 0.01)
        if readable and not os.read(control, 1):
            os.close(control)
            control = -1
            if finish_on_parent_exit:
                continue
            _terminate(child)
            break
    return child.wait()


def _terminate(child: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)


if __name__ == "__main__":
    raise SystemExit(main())
