"""Process entry point used by launchd and systemd host services."""

from __future__ import annotations

import argparse

import click

from omnigent.host import HOST_FATAL_EXIT_CODE


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
