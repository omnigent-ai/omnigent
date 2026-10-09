"""Process entry point used by launchd and systemd host services."""

from __future__ import annotations

import argparse

from omnigent.host import HOST_FATAL_EXIT_CODE
from omnigent.host.supervisor import run_supervisor

_INTENTIONAL_EXIT_CODES = {0, 130, 143, HOST_FATAL_EXIT_CODE}


def main() -> int:
    """Run the supervised host service and normalize deliberate exits."""
    parser = argparse.ArgumentParser(description="Omnigent host service")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--server", help="Remote Omnigent server URL.")
    mode.add_argument("--local", action="store_true", help="Run a local Omnigent server.")
    args = parser.parse_args()

    server = "" if args.local else args.server
    code = run_supervisor(server)
    # A permanent auth/config failure and an operator stop should leave the
    # service enabled but stopped instead of entering a restart loop.
    return 0 if code in _INTENTIONAL_EXIT_CODES else code


if __name__ == "__main__":
    raise SystemExit(main())
