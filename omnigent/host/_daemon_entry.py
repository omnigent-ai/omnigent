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
import logging
import os
import subprocess
import sys
import time


def _spawn_replacement(*, local: bool, server: str | None) -> bool:
    """Launch a fresh copy of this daemon process and detach it.

    Rebuilds argv from the parsed mode flags so the replacement uses the same
    launch style as the original CLI spawn, and mirrors
    ``_spawn_host_daemon_process`` in ``cli.py``: a real log file (not
    ``/dev/null``) so a crash in the replacement is diagnosable, and the
    platform-appropriate detach kwargs (``_proc.spawn_kwargs()``) rather than
    a POSIX-only ``start_new_session``.

    :param local: Whether the daemon was started in ``--local`` mode.
    :param server: Server URL in ``--server`` mode; ``None`` in local mode.
    :returns: ``True`` if the replacement was spawned; ``False`` on a logged
        failure, so the caller can exit non-zero and let a supervised host's
        service manager retry rather than leaving no daemon running at all.
    """
    from omnigent.inner import _proc
    from omnigent.process_logging import (
        PROCESS_LOG_FILE_ENV_VAR,
        child_logging_popen_kwargs,
        open_process_log_file,
    )

    mode_args = ["--local"] if local else ["--server", server or ""]
    args = [sys.executable, "-P", "-m", "omnigent.host._daemon_entry", *mode_args]
    env = {k: v for k, v in os.environ.items() if k != PROCESS_LOG_FILE_ENV_VAR}
    log_path, log_fh = open_process_log_file("host")
    env[PROCESS_LOG_FILE_ENV_VAR] = str(log_path)
    try:
        with child_logging_popen_kwargs(env) as logging_kwargs:
            subprocess.Popen(
                args,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                **_proc.spawn_kwargs(),
                **logging_kwargs,
            )
    except OSError:
        logging.getLogger(__name__).warning(
            "Scheduled restart: failed to spawn replacement daemon", exc_info=True
        )
        return False
    finally:
        log_fh.close()
    return True


def main() -> int:
    """Parse args and run the host process.

    Exactly one of ``--server <url>`` or ``--local`` must be given. In
    ``--local`` mode the daemon starts/reuses the background local AP
    server itself and connects to that.

    :returns: Process exit code: ``0`` normally, ``1`` when a scheduled
        restart could not spawn its replacement (so the failure is visible
        instead of silently leaving no daemon running).
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

    log_path = configure_process_logging("host", force=True)

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
    lifecycle_lock = DaemonLifecycleLock.for_target(daemon_target)
    claim = lifecycle_lock.try_acquire()
    if claim is False:
        logging.getLogger(__name__).info(
            "Another host daemon already claimed target %s; exiting", daemon_target
        )
        return 0

    restart_requested = False
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
            config_sig=os.environ.get(DAEMON_CONFIG_SIG_ENV_VAR),
        )
        write_daemon_record(record, update_legacy_pidfile=True)

        if args.local:
            # The daemon owns the local server: start/reuse it, then connect.
            from omnigent.host.local_server import ensure_local_omnigent_server

            server_url = ensure_local_omnigent_server().url
        else:
            server_url = args.server

        from omnigent.host.connect import run_host_process

        restart_requested = run_host_process(
            server_url=server_url,
            daemon_target=daemon_target,
            lifecycle_lock=lifecycle_lock,
        )
    finally:
        # Release the lifecycle lock before spawning a replacement so the
        # replacement's try_acquire() succeeds (it flocks the same record file).
        lifecycle_lock.release()

    if restart_requested and not _spawn_replacement(local=args.local, server=args.server):
        # No replacement is running: surface the failure through the exit
        # code rather than leaving the machine with no host daemon at all.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
