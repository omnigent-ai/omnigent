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
import contextlib
import logging
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnigent.host.daemon_lifecycle import DaemonLifecycleLock


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

    from omnigent.process_logging import (
        configure_process_logging,
        ensure_stdio_survives_unencodable_output,
    )

    # The daemon's stdio is its log file; on a legacy code page (Windows ANSI)
    # the connect loop's ✓/⚠ glyphs would raise UnicodeEncodeError and drop the tunnel.
    ensure_stdio_survives_unencodable_output()
    log_path = configure_process_logging("host", force=True)

    from omnigent.host.crash_reporting import install_host_crash_hooks, set_host_exit_context

    install_host_crash_hooks(enable_faulthandler=True)

    if args.local == bool(args.server):
        # Both or neither — the CLI always passes exactly one; fail loud.
        parser.error("exactly one of --server <url> or --local is required")

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

    workspace_lock: DaemonLifecycleLock | None = None
    try:
        from omnigent.host.identity import load_or_create_host_identity

        identity = load_or_create_host_identity()
        mode = "local" if args.local else "server"
        workspace_id = None
        if not args.local:
            workspace_id, workspace_lock = _claim_workspace(
                server_url=args.server, daemon_target=daemon_target, host_id=identity.host_id
            )
            if workspace_id is not None and workspace_lock is None:
                _drop_own_record(lifecycle_lock)
                return
        record = HostDaemonRecord(
            pid=os.getpid(),
            target=daemon_target,
            mode=mode,
            server_url=None if args.local else daemon_target,
            log_path=str(log_path),
            started_at=int(time.time()),
            host_id=identity.host_id,
            config_sig=os.environ.get(DAEMON_CONFIG_SIG_ENV_VAR),
            workspace_id=workspace_id,
        )
        write_daemon_record(record, update_legacy_pidfile=True)

        if args.local:
            # The daemon owns the local server: start/reuse it, then connect.
            from omnigent.host.local_server import ensure_local_omnigent_server

            server_url = ensure_local_omnigent_server().url
        else:
            server_url = args.server

        from omnigent.host.connect import run_host_process

        run_host_process(
            server_url=server_url,
            daemon_target=daemon_target,
            lifecycle_lock=lifecycle_lock,
        )
    finally:
        if workspace_lock is not None:
            workspace_lock.release()
        lifecycle_lock.release()


def _claim_workspace(
    *, server_url: str, daemon_target: str, host_id: str
) -> tuple[str | None, DaemonLifecycleLock | None]:
    """Admit this daemon as the only one for its (host id, workspace).

    Another URL can reach the same workspace. A second daemon with this host
    id there would keep taking over this one's server connection, so the
    later daemon yields to the one already serving the workspace.

    :param server_url: Server URL this daemon connects to.
    :param daemon_target: Normalized registry target of this daemon.
    :param host_id: Host id this daemon advertises.
    :returns: ``(workspace_id, lock)``: the held workspace lock, ``(id, None)``
        when this daemon must yield, or ``(None, None)`` when the workspace is
        unknown and no check applies.
    """
    from omnigent.host.daemon_lifecycle import (
        WORKSPACE_DISCOVERY_BUDGET_S,
        WORKSPACE_PROBE_TIMEOUT_S,
        DaemonLifecycleLock,
        find_same_workspace_daemon,
        probe_workspace_id,
        read_daemon_records,
        record_owner_is_live,
        resolve_daemon_workspace_id,
    )

    logger = logging.getLogger(__name__)
    # One budget for every probe here, so several older daemons with unknown
    # workspaces can't push this daemon past the launcher's claim deadline.
    deadline = time.monotonic() + WORKSPACE_DISCOVERY_BUDGET_S

    def _probe(url: str, *, org_id: str | None = None) -> str | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        return probe_workspace_id(
            url, org_id=org_id, timeout_s=min(remaining, WORKSPACE_PROBE_TIMEOUT_S)
        )

    workspace_id = resolve_daemon_workspace_id(server_url, probe=_probe)
    if workspace_id is None:
        return None, None
    # The lock is close-on-exec: a daily-restart exec drops it until this runs
    # again, and a daemon on another URL that claims it first keeps the workspace.
    lock = DaemonLifecycleLock.for_workspace(host_id, workspace_id)
    acquired = lock.try_acquire()
    if acquired is False:
        logger.info(
            "Another host daemon already serves workspace %s for this host; %s exits",
            workspace_id,
            daemon_target,
        )
        return workspace_id, None
    try:
        # Yield to a live pre-lock daemon; without flock, to any live record.
        older = find_same_workspace_daemon(
            read_daemon_records(),
            server_url=server_url,
            host_id=host_id,
            is_live=record_owner_is_live,
            workspace_id=workspace_id,
            # Older daemons record no workspace id, and a sign-in through a
            # Databricks CLI profile leaves no login record to read it from.
            probe=_probe,
            legacy_only=acquired is True,
        )
    except BaseException:
        lock.release()
        raise
    if older is not None:
        logger.info(
            "Host daemon pid %s already serves workspace %s through %s; %s exits",
            older.pid,
            workspace_id,
            older.target,
            daemon_target,
        )
        lock.release()
        return workspace_id, None
    return workspace_id, lock


def _drop_own_record(lifecycle_lock: DaemonLifecycleLock) -> None:
    """Remove this process's own record and pidfile; the record may predate a re-exec."""
    from omnigent.host.daemon_lifecycle import daemon_record_path, read_daemon_record
    from omnigent.process_logging import data_dir

    path = daemon_record_path(lifecycle_lock.target)
    with contextlib.suppress(OSError):
        record = None if path.stat().st_size == 0 else read_daemon_record(path)
        if record is None or record.pid == os.getpid():
            path.unlink()
    pidfile = data_dir() / "host.pid"
    with contextlib.suppress(OSError, ValueError, IndexError):
        if int(pidfile.read_text().split()[0]) == os.getpid():
            pidfile.unlink()


if __name__ == "__main__":
    main()
