"""Background daemon entry point for auto-launched host processes.

Spawned by ``_ensure_host_daemon`` in ``cli.py`` when ``run`` /
``claude`` / ``codex`` register this machine as a host. Runs the same
:class:`HostProcess` loop as ``omnigent host``.

Two modes:

- ``--server <url>``: connect to an existing (remote or local) Omnigent server.
- ``--local``: this daemon owns a local Omnigent server — start (or reuse) a
  persistent background ``omnigent server`` on loopback and connect to
  it. The CLI discovers the resulting URL via the local-server pidfile.
  With ``--adopt-server`` the daemon was requested through an explicit
  loopback URL of that server, so it reuses the running server as-is
  instead of restarting it on config drift.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from dataclasses import replace


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
    parser.add_argument(
        "--adopt-server",
        action="store_true",
        help="With --local: reuse the running local server as-is, never restart it.",
    )
    args = parser.parse_args()

    from omnigent.process_logging import configure_process_logging

    log_path = configure_process_logging("host", force=True)

    from omnigent.host.crash_reporting import install_host_crash_hooks, set_host_exit_context

    install_host_crash_hooks(enable_faulthandler=True)

    if args.local == bool(args.server):
        # Both or neither — the CLI always passes exactly one; fail loud.
        parser.error("exactly one of --server <url> or --local is required")
    if args.adopt_server and not args.local:
        parser.error("--adopt-server requires --local")

    from omnigent.host.daemon_lifecycle import (
        DAEMON_CONFIG_SIG_ENV_VAR,
        DaemonLifecycleLock,
        HostDaemonRecord,
        normalize_daemon_target,
        write_daemon_record,
    )

    daemon_target = normalize_daemon_target(None if args.local else args.server)
    set_host_exit_context(daemon_target=daemon_target)
    lifecycle_lock = DaemonLifecycleLock.for_target(daemon_target)
    claim = lifecycle_lock.try_acquire()
    if claim is False:
        logging.getLogger(__name__).info(
            "Another host daemon already claimed target %s; exiting", daemon_target
        )
        return

    try:
        from omnigent.host.identity import load_or_create_host_identity

        identity = load_or_create_host_identity()
        mode = "local" if args.local else "server"
        record = HostDaemonRecord(
            pid=os.getpid(),
            target=daemon_target,
            mode=mode,
            server_url=None if args.local else daemon_target,
            log_path=str(log_path),
            started_at=int(time.time()),
            host_id=identity.host_id,
            config_sig=os.environ.get(DAEMON_CONFIG_SIG_ENV_VAR) or None,
            adopted=args.adopt_server,
        )
        write_daemon_record(record, update_legacy_pidfile=True)

        if args.local:
            # The daemon owns the local server: start/reuse it, then connect.
            # Publishing the served URL keeps this record addressable by that
            # URL once the server pidfile is gone.
            from omnigent.host.local_server import ensure_local_omnigent_server

            server_url = ensure_local_omnigent_server(
                replace_on_config_drift=not args.adopt_server
            ).url
            write_daemon_record(replace(record, resolved_server_url=server_url))
        else:
            server_url = args.server

        from omnigent.host.connect import run_host_process

        run_host_process(
            server_url=server_url,
            daemon_target=daemon_target,
            lifecycle_lock=lifecycle_lock,
        )
    finally:
        lifecycle_lock.release()


if __name__ == "__main__":
    main()
