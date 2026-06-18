"""Native Qwen Code wrapper for the Omnigent CLI."""

from __future__ import annotations

import click

_RESUME_PICKER_SENTINEL = object()


@click.command(
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    }
)
@click.option(
    "--server",
    default=None,
    help=(
        "Remote omnigent URL. Ensures the host daemon, asks the "
        "daemon-spawned runner to launch Qwen Code, and attaches this TTY. "
        'Pass --server "" to auto-spawn a persistent local server in the '
        "background and use that instead of a remote one."
    ),
)
@click.option(
    "-r",
    "--resume",
    "resume",
    is_flag=False,
    flag_value=_RESUME_PICKER_SENTINEL,
    default=None,
    help=(
        "Resume a prior Omnigent conversation. With a conversation id "
        "(e.g. ``--resume conv_abc123``) attaches directly; with no value "
        "opens an interactive picker scoped to qwen sessions."
    ),
)
@click.option(
    "--session",
    "session_id",
    metavar="SESSION_ID",
    default=None,
    hidden=True,
    help="Deprecated alias for ``--resume <id>``; kept for one release.",
)
@click.argument("qwen_args", nargs=-1, type=click.UNPROCESSED)
def qwen(
    server: str | None,
    resume: str | None,
    session_id: str | None,
    qwen_args: tuple[str, ...],
) -> None:
    """Launch Qwen Code in an Omnigent terminal.

    \b
    Examples:
      omnigent qwen
      omnigent qwen --resume conv_abc123
      omnigent qwen --resume                    # interactive picker
      omnigent qwen --model qwen/qwen-2.5-coder
    """
    choice = _split_resume_value(resume)
    if session_id is not None and (choice.picker or choice.conversation_id is not None):
        raise click.UsageError(
            "--session and --resume are mutually exclusive; "
            "prefer --resume (--session is deprecated).",
        )

    from omnigent.cli import _load_effective_config
    from omnigent.runner.main import run

    cfg = _load_effective_config()
    if server is None:
        server = cfg.get("server")

    # Build the args to pass to run.main - same pattern as kimi_native.py
    run_args = ["--harness", "qwen"]
    if resume:
        run_args.append("--resume")
        if choice.conversation_id:
            run_args.append(choice.conversation_id)
    if qwen_args:
        run_args.extend(qwen_args)

    run.main(args=run_args, standalone_mode=False)


def _split_resume_value(resume: str | None) -> tuple[bool, str | None]:
    """Parse the --resume value into (picker, conversation_id)."""
    if resume is None:
        return False, None
    if resume == "":
        # Empty string means --resume with no value → picker mode
        return True, None
    # Otherwise it's a conversation id
    return False, resume
