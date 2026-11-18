"""Keep a process-group leader alive until a command's output pipes close.

Executed by absolute path using the host Python; keep imports stdlib-only.
"""

from __future__ import annotations

import subprocess
import sys


def main() -> int:
    """Relay a command's output and status, including pipes inherited by children."""
    try:
        result = subprocess.run(sys.argv[1:], capture_output=True, check=False)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 127
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


if __name__ == "__main__":
    sys.exit(main())
