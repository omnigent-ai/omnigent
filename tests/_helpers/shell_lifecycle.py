"""Commands that leave detached output holders for lifecycle assertions."""

from __future__ import annotations

import sys
from pathlib import Path

_DESCENDANT = """
import signal, sys
from pathlib import Path
print("descendant-ready", flush=True)
print("descendant-stderr", file=sys.stderr, flush=True)
Path(sys.argv[1]).write_text("ready")
signal.pause()
"""

_COMMAND = """
import signal, subprocess, sys, time
from pathlib import Path
subprocess.Popen(
    [sys.executable, "-I", "-S", "-c", sys.argv[3], sys.argv[1]], start_new_session=True
)
ready = Path(sys.argv[1])
deadline = time.monotonic() + float(sys.argv[5])
while not ready.exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("Descendant did not start")
    time.sleep(0.01)
if sys.argv[2] == "complete":
    print("command-complete", flush=True)
    raise SystemExit(int(sys.argv[4]))
signal.pause()
"""


def detached_output_command(
    ready: Path, ending: str, *, exit_code: int, ready_timeout: int
) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-S",
        "-c",
        _COMMAND,
        str(ready),
        ending,
        _DESCENDANT,
        str(exit_code),
        str(ready_timeout),
    ]
