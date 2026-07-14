"""Background daemon entry point for auto-launched host processes.

Spawned by ``_ensure_host_daemon`` in ``cli.py`` when ``run`` /
``claude`` / ``codex`` register this machine as a host. Runs the same
:class:`HostProcess` loop as ``omnigent host``.

Two modes:

- ``--server <url>``: connect to an existing (remote or local) Omnigent server.
- ``--local``: this daemon owns a local Omnigent server — start (or reuse) a
  persistent background ``omnigent server`` on loopback and connect to
  it. The CLI discovers the resulting URL via the local-server pidfile.
"""

from __future__ import annotations

import argparse


def main() -> None:
    """Parse args and run the host process.

    Exactly one of ``--server <url>`` or ``--local`` must be given. In
    ``--local`` mode the daemon starts/reuses the background local AP
    server itself and connects to that.

    :returns: None.
    :raises SystemExit: If neither / both of ``--server`` and ``--local``
        are provided.
    """
    parser = argparse.ArgumentParser(
        description="Background host daemon",
    )
    parser.add_argument(
        "--server",
        default=None,
        help="AP server URL to connect to (remote or local).",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Start (or reuse) a local Omnigent server and connect to it.",
    )
    args = parser.parse_args()

    from omnigent.process_logging import configure_process_logging

    configure_process_logging("host", force=True)

    if args.local == bool(args.server):
        # Both or neither — the CLI always passes exactly one; fail loud.
        parser.error("exactly one of --server <url> or --local is required")

    if args.local:
        # The daemon owns the local server: start/reuse it, then connect.
        from omnigent.host.local_server import ensure_local_omnigent_server

        server_url = ensure_local_omnigent_server().url
    else:
        server_url = args.server

    from omnigent.host.connect import run_host_process

    run_host_process(server_url=server_url)


if __name__ == "__main__":
    main()
