"""Process entry point used by launchd and systemd host services."""

from __future__ import annotations

import argparse
import time

import click

from omnigent.host import HOST_FATAL_EXIT_CODE

# How often a service host waiting behind another daemon checks whether it still runs.
_STANDBY_POLL_S = 60.0


def _wait_while_workspace_is_served(server: str) -> None:
    """Wait while another live daemon already serves *server*'s workspace.

    The host would be refused and exit, and the service manager would restart
    it every few seconds. Waiting keeps the other daemon as the only one and
    lets this service take over once it stops, up to a minute later.

    :param server: Remote Omnigent server URL, e.g.
        ``"https://example.databricks.com/omnigent"``.
    """
    from omnigent.cli import _resolve_host_server, _same_workspace_daemon

    resolved = _resolve_host_server(server)
    announced = False
    while (sibling := _same_workspace_daemon(resolved)) is not None:
        if not announced:
            click.echo(
                f"Host daemon pid {sibling.pid} already serves this workspace through "
                f"{sibling.target}; waiting until it stops.",
                err=True,
            )
            announced = True
        time.sleep(_STANDBY_POLL_S)


def main() -> int:
    """Run the foreground host command and normalize permanent failures."""
    parser = argparse.ArgumentParser(description="Omnigent host service")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--server", help="Remote Omnigent server URL.")
    mode.add_argument("--local", action="store_true", help="Run a local Omnigent server.")
    args = parser.parse_args()

    from omnigent.cli import cli

    server = "" if args.local else args.server
    try:
        if server:
            _wait_while_workspace_is_served(server)
        cli.main(
            args=["host", "--server", server, "--non-interactive"],
            prog_name="omnigent",
            standalone_mode=False,
        )
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.Abort:
        click.echo("Aborted!", err=True)
        return 1
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        # A permanent auth/config failure should leave the service enabled but
        # stopped instead of entering a supervisor restart loop.
        return 0 if code == HOST_FATAL_EXIT_CODE else code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
