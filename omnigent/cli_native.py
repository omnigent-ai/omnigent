"""Native coding-agent (TUI) CLI subcommands.

Each ``omnigent <tool>`` command (``claude``, ``codex``, ``pi``, …) launches a
vendor CLI inside an Omnigent-managed terminal. They were extracted from
:mod:`omnigent.cli` so that file stops carrying ~1400 lines of near-identical
launchers and so a future registry-driven step can generate them from
``native_agents()`` rather than hand-maintaining one ``@cli.command`` per tool.

Registration is deferred: :func:`register_native_commands` is called by
``omnigent.cli`` after the ``cli`` group and its shared helpers exist. The
command bodies reach those helpers through thin proxies that resolve them on
the ``omnigent.cli`` module at call time, so this module never needs a
top-level ``import omnigent.cli`` (no import cycle) and a test's
``monkeypatch`` of an ``omnigent.cli`` helper still takes effect. Only
decorator-time state (:data:`omnigent.cli_common.RESUME_PICKER_SENTINEL`,
:data:`omnigent.cli_common.CLAUDE_STARTUP_PROFILE_ENV_VAR`) is imported at module
scope, from the leaf :mod:`omnigent.cli_common`.
"""

from __future__ import annotations

import os

import click

from omnigent._startup_profile import StartupProfiler
from omnigent.cli_common import (
    CLAUDE_STARTUP_PROFILE_ENV_VAR as _CLAUDE_STARTUP_PROFILE_ENV_VAR,
)
from omnigent.cli_common import (
    RESUME_PICKER_SENTINEL as _RESUME_PICKER_SENTINEL,
)
from omnigent.cli_common import (
    reject_native_on_windows as _reject_native_on_windows,
)


def register_native_commands(cli: click.Group) -> None:
    """Define and register the native coding-agent subcommands on *cli*.

    Called once from :mod:`omnigent.cli` at import time. The shared runtime
    helpers are looked up on the ``omnigent.cli`` module *at call time* (via
    thin proxies below), not bound now — so importing this module never imports
    ``omnigent.cli`` (no cycle) and tests that ``monkeypatch`` a helper as an
    ``omnigent.cli`` attribute still take effect inside these commands.

    :param cli: The root ``omnigent`` Click group to attach commands to.
    """
    import omnigent.cli as _cli

    # Resolve each shared helper on the live module per call so a test's
    # monkeypatch of ``omnigent.cli.<helper>`` is honored. Binding the function
    # objects once here would capture the pre-patch originals.
    def _build_kiro_launch_args(*a, **k):  # type: ignore[no-untyped-def]
        return _cli._build_kiro_launch_args(*a, **k)

    def _ensure_backend(*a, **k):  # type: ignore[no-untyped-def]
        return _cli._ensure_backend(*a, **k)

    def _load_effective_config(*a, **k):  # type: ignore[no-untyped-def]
        return _cli._load_effective_config(*a, **k)

    def _reject_reserved_kiro_resume_args(*a, **k):  # type: ignore[no-untyped-def]
        return _cli._reject_reserved_kiro_resume_args(*a, **k)

    def _resolve_auto_open_conversation_from_config(*a, **k):  # type: ignore[no-untyped-def]
        return _cli._resolve_auto_open_conversation_from_config(*a, **k)

    def _resolve_harness_startup_args(*a, **k):  # type: ignore[no-untyped-def]
        return _cli._resolve_harness_startup_args(*a, **k)

    def _split_resume_value(*a, **k):  # type: ignore[no-untyped-def]
        return _cli._split_resume_value(*a, **k)

    @cli.command(
        context_settings={
            "ignore_unknown_options": True,
            "allow_extra_args": True,
        }
    )
    @click.option(
        "--server",
        default=None,
        help=(
            "Remote omnigent URL. Starts a local runner, binds the session, "
            "launches Claude in a terminal resource, and attaches this TTY. "
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
            "opens an interactive picker scoped to claude-native sessions."
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
    @click.option(
        "--host",
        "register_host",
        is_flag=True,
        default=False,
        help=(
            "[DEPRECATED] No-op, kept for script compatibility. The host daemon is "
            "always ensured now, so this flag does nothing."
        ),
    )
    @click.option(
        "--use-native-config",
        "use_claude_config",
        is_flag=True,
        default=False,
        help=(
            "Use your existing Claude Code configuration instead of Databricks auth. "
            "When set, any configured provider is ignored and Claude "
            "authenticates via its own ``~/.claude/`` settings."
        ),
    )
    @click.option(
        "--profile-startup",
        "profile_startup",
        is_flag=True,
        default=False,
        help=(
            "Print native Claude startup timing marks to stderr. Also enabled by "
            f"{_CLAUDE_STARTUP_PROFILE_ENV_VAR}=1."
        ),
    )
    @click.option(
        "--command",
        "claude_command",
        default=None,
        metavar="CMD",
        help=(
            "[DEPRECATED] Claude Code CLI executable to run. Use the "
            "``OMNIGENT_CLAUDE_PATH`` env var or the "
            "``harness.claude-native.command`` config override instead; this "
            "flag will be removed in a future release."
        ),
    )
    @click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
    def claude(
        server: str | None,
        resume: str | None,
        session_id: str | None,
        register_host: bool,
        use_claude_config: bool,
        profile_startup: bool,
        claude_command: str | None,
        claude_args: tuple[str, ...],
    ) -> None:
        # Param docs live in comments — Click uses the docstring for --help.
        # :param server: Remote Omnigent server URL, or None for local.
        # :param resume: None, picker sentinel, or a conversation id.
        # :param session_id: Legacy ``--session`` id; mutually exclusive with ``--resume``.
        # :param use_claude_config: When True, skip ucode/Databricks auth and use
        #     existing Claude config.
        # :param profile_startup: When True, print startup timing marks.
        # :param claude_args: Pass-through args for ``claude``.
        """Launch Claude Code in an Omnigent terminal.

        \b
        Examples:
          omnigent claude
          omnigent claude --resume conv_abc123
          omnigent claude --resume                  # interactive picker
          omnigent claude --server https://<app>.databricksapps.com
        """
        _reject_native_on_windows("claude")
        startup_profiler = StartupProfiler.from_env(
            name="omnigent claude",
            env_var=_CLAUDE_STARTUP_PROFILE_ENV_VAR,
            explicit=profile_startup,
        )
        startup_profiler.mark("cli entered")

        # Apply config defaults (same as ``run`` does).
        cfg = _load_effective_config()
        if server is None:
            server = cfg.get("server")
        auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)
        startup_profiler.mark("config resolved")

        # Validate option combinations BEFORE any side effects (daemon
        # spawn, server discovery). Calling _ensure_backend first would
        # mean a bad arg pair waits the full local-server-discover
        # timeout (60s in CI) before surfacing the UsageError, which
        # the test_claude_command_session_and_resume_mutually_exclusive
        # regression caught in CI.
        del register_host
        choice = _split_resume_value(resume)
        if session_id is not None and (choice.picker or choice.conversation_id is not None):
            raise click.UsageError(
                "--session and --resume are mutually exclusive; "
                "prefer --resume (--session is deprecated).",
            )
        startup_profiler.mark("arguments validated")

        # Ensure the host daemon (local when ``--server`` is omitted/empty,
        # remote otherwise) and resolve the concrete Omnigent server URL. The daemon
        # owns the runner; the CLI only connects. ``--host`` is now redundant
        # (the daemon is always ensured) and kept only as a no-op for scripts.
        startup_profiler.mark("ensuring backend")
        server = _ensure_backend(server)
        startup_profiler.mark("backend ready", detail=f"server={server}")

        resolved_session_id = (
            choice.conversation_id if choice.conversation_id is not None else session_id
        )

        from omnigent.claude_native import run_claude_native
        from omnigent.harness_startup_config import resolve_harness_command

        startup_profiler.mark("native module imported")

        if claude_command:
            click.echo(
                "omnigent: `claude --command` is deprecated; set OMNIGENT_CLAUDE_PATH "
                "or harness.claude-native.command instead. The --command flag will "
                "be removed in a future release.",
                err=True,
            )
        resolved_command = resolve_harness_command(
            "claude-native",
            default="claude",
            explicit=claude_command,
            cfg=cfg,
        )
        run_claude_native(
            server=server,
            session_id=resolved_session_id,
            resume_picker=choice.picker,
            claude_args=_resolve_harness_startup_args(cfg, "claude-native", claude_args),
            use_claude_config=use_claude_config,
            auto_open_conversation=auto_open_conversation,
            startup_profiler=startup_profiler,
            command=resolved_command,
        )

    @cli.command(
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
            "daemon-spawned runner to launch Codex, and attaches this TTY. "
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
            "opens an interactive picker scoped to codex-native sessions."
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
    @click.option("--model", default=None, help="Codex model to use for the native thread.")
    @click.option(
        "-p",
        "--prompt",
        default=None,
        help="Send this as the first message after the Codex TUI starts.",
    )
    @click.argument("codex_args", nargs=-1, type=click.UNPROCESSED)
    def codex(
        server: str | None,
        resume: str | None,
        session_id: str | None,
        model: str | None,
        prompt: str | None,
        codex_args: tuple[str, ...],
    ) -> None:
        # Param docs live in comments — Click uses the docstring for --help.
        # :param server: Remote Omnigent server URL, or None for local.
        # :param resume: None, picker sentinel, or a conversation id.
        # :param session_id: Legacy ``--session`` id; mutually exclusive with ``--resume``.
        # :param model: Codex model id.
        # :param prompt: Optional first prompt.
        # :param codex_args: Pass-through args for ``codex`` before ``resume``.
        """Launch Codex TUI in an Omnigent terminal.

        \b
        Examples:
          omnigent codex
          omnigent codex --resume conv_abc123
          omnigent codex --resume                  # interactive picker
          omnigent codex --server https://<app>.databricksapps.com
        """
        _reject_native_on_windows("codex")
        choice = _split_resume_value(resume)
        if session_id is not None and (choice.picker or choice.conversation_id is not None):
            raise click.UsageError(
                "--session and --resume are mutually exclusive; "
                "prefer --resume (--session is deprecated).",
            )

        from omnigent.codex_native import run_codex_native
        from omnigent.harness_startup_config import resolve_harness_command

        cfg = _load_effective_config()
        if server is None:
            server = cfg.get("server")
        if model is None:
            model = cfg.get("model")
        auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)

        # Ensure the host daemon (local when ``--server`` is omitted/empty,
        # remote otherwise) and resolve the concrete Omnigent server URL. Codex follows
        # the same ownership model as attach/run/claude: the daemon-spawned runner
        # owns the app-server and TUI; the CLI attaches to the tmux terminal.
        server = _ensure_backend(server)

        resolved_session_id = (
            choice.conversation_id if choice.conversation_id is not None else session_id
        )

        resolved_command = resolve_harness_command(
            "codex-native",
            default="codex",
            explicit=None,
            cfg=cfg,
        )
        run_codex_native(
            server=server,
            session_id=resolved_session_id,
            resume_picker=choice.picker,
            codex_args=_resolve_harness_startup_args(cfg, "codex-native", codex_args),
            model=model,
            prompt=prompt,
            auto_open_conversation=auto_open_conversation,
            command=resolved_command,
        )

    @cli.command(
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
            "daemon-spawned runner to launch OpenCode, and attaches this TTY. "
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
            "opens an interactive picker scoped to opencode-native sessions."
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
    @click.option("--model", default=None, help="OpenCode model to use for the native session.")
    @click.argument("opencode_args", nargs=-1, type=click.UNPROCESSED)
    def opencode(
        server: str | None,
        resume: str | None,
        session_id: str | None,
        model: str | None,
        opencode_args: tuple[str, ...],
    ) -> None:
        # :param server: Remote Omnigent server URL, or None for local.
        # :param resume: None, picker sentinel, or a conversation id.
        # :param session_id: Legacy ``--session`` id; mutually exclusive with ``--resume``.
        # :param model: OpenCode model id pinned on the wrapper spec.
        # :param opencode_args: Pass-through args persisted for the ``opencode attach`` TUI.
        # NOTE: no ``--command`` flag — override the opencode binary via
        # ``OMNIGENT_OPENCODE_PATH`` or ``harness.opencode-native.command`` config.
        # (opencode-native resolves its binary on the runner side; if a spec/env
        # path to thread a client override through is added later, this stays
        # consistent with the other native commands' env/config override model.)
        """Launch OpenCode TUI in an Omnigent terminal.

        \b
        Examples:
          omnigent opencode
          omnigent opencode --resume conv_abc123
          omnigent opencode --resume                  # interactive picker
          omnigent opencode --server https://<app>.databricksapps.com
        """
        from omnigent.opencode_native import run_opencode_native

        cfg = _load_effective_config()
        if server is None:
            server = cfg.get("server")
        if model is None:
            # Prefer the OpenCode-specific default (set in `omni setup` → OpenCode →
            # "Set default model"); fall back to the shared `model` key for back-compat.
            model = cfg.get("opencode_model") or cfg.get("model")
        auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)

        # Validate option combinations before any side effects (see the codex
        # command): _ensure_backend can spawn the daemon and take the full
        # local-server-discover timeout, which would mask a bad arg pair as an
        # outage instead of a usage error.
        choice = _split_resume_value(resume)
        if session_id is not None and (choice.picker or choice.conversation_id is not None):
            raise click.UsageError(
                "--session and --resume are mutually exclusive; "
                "prefer --resume (--session is deprecated).",
            )

        # Ensure the host daemon (local when ``--server`` is omitted/empty, remote
        # otherwise); the daemon-spawned runner owns ``opencode serve`` + the TUI,
        # and this CLI attaches to the tmux terminal.
        server = _ensure_backend(server)
        resolved_session_id = (
            choice.conversation_id if choice.conversation_id is not None else session_id
        )
        run_opencode_native(
            server=server,
            session_id=resolved_session_id,
            resume_picker=choice.picker,
            opencode_args=_resolve_harness_startup_args(cfg, "opencode-native", opencode_args),
            model=model,
            auto_open_conversation=auto_open_conversation,
        )

    @cli.command(
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
            "daemon-spawned runner to launch Pi, and attaches this TTY. "
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
            "opens an interactive picker scoped to pi-native sessions."
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
    @click.argument("pi_args", nargs=-1, type=click.UNPROCESSED)
    def pi(
        server: str | None,
        resume: str | None,
        session_id: str | None,
        pi_args: tuple[str, ...],
    ) -> None:
        """Launch Pi TUI in an Omnigent terminal.

        \b
        Examples:
          omnigent pi
          omnigent pi --resume conv_abc123
          omnigent pi --resume                    # interactive picker
          omnigent pi --model local-deepseek/deepseek-v4-flash
        """
        choice = _split_resume_value(resume)
        if session_id is not None and (choice.picker or choice.conversation_id is not None):
            raise click.UsageError(
                "--session and --resume are mutually exclusive; "
                "prefer --resume (--session is deprecated).",
            )

        from omnigent.harness_startup_config import resolve_harness_command
        from omnigent.pi_native import run_pi_native

        cfg = _load_effective_config()
        # Thread ``harness.pi-native.command`` config into the runner via the
        # canonical ``OMNIGENT_PI_PATH`` env var (set before ``_ensure_backend``
        # so a locally-spawned daemon inherits it; a remote ``--server`` runner
        # reads its own host env, so set the var there). No ``--command`` flag —
        # override via ``OMNIGENT_PI_PATH`` or config.
        _resolved = resolve_harness_command("pi-native", default="", explicit=None, cfg=cfg)
        if _resolved:
            os.environ["OMNIGENT_PI_PATH"] = _resolved
        if server is None:
            server = cfg.get("server")
        auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)

        server = _ensure_backend(server)
        resolved_session_id = (
            choice.conversation_id if choice.conversation_id is not None else session_id
        )

        run_pi_native(
            server=server,
            session_id=resolved_session_id,
            resume_picker=choice.picker,
            pi_args=_resolve_harness_startup_args(cfg, "pi-native", pi_args),
            auto_open_conversation=auto_open_conversation,
        )

    @cli.command(
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
            "daemon-spawned runner to launch the Cursor TUI, and attaches this TTY. "
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
            "opens an interactive picker scoped to cursor-native sessions."
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
    @click.option(
        "--mode",
        "mode",
        default=None,
        type=click.Choice(["plan", "ask"]),
        help=(
            "Start cursor-agent in the given execution mode. "
            "``plan``: read-only/planning (analyze, propose plans, no edits). "
            "``ask``: Q&A style for explanations and questions (read-only)."
        ),
    )
    @click.option(
        "--model",
        default=None,
        help="Cursor model to use for the native TUI (e.g. gpt-5.2, claude-4.6-sonnet-medium).",
    )
    @click.argument("cursor_args", nargs=-1, type=click.UNPROCESSED)
    def cursor(
        server: str | None,
        resume: str | None,
        session_id: str | None,
        mode: str | None,
        model: str | None,
        cursor_args: tuple[str, ...],
    ) -> None:
        # Param docs live in comments — Click uses the docstring for --help.
        # :param model: Cursor model id passed to cursor-agent as ``--model``.
        """Launch the Cursor TUI in an Omnigent terminal.

        \b
        Examples:
          omnigent cursor
          omnigent cursor --model gpt-5.2
          omnigent cursor --resume conv_abc123
          omnigent cursor --resume                 # interactive picker
          omnigent cursor --mode plan              # start in plan (read-only) mode
          omnigent cursor --mode ask               # start in ask (Q&A) mode
        """
        _reject_native_on_windows("cursor")
        choice = _split_resume_value(resume)
        if session_id is not None and (choice.picker or choice.conversation_id is not None):
            raise click.UsageError(
                "--session and --resume are mutually exclusive; "
                "prefer --resume (--session is deprecated).",
            )

        from omnigent.cursor_native import run_cursor_native
        from omnigent.harness_startup_config import resolve_harness_command

        cfg = _load_effective_config()
        # Thread ``--command`` / ``harness.cursor-native.command`` config into the
        # runner via the canonical ``OMNIGENT_CURSOR_PATH`` env var (set before
        # ``_ensure_backend`` so a locally-spawned daemon inherits it; a remote
        # ``--server`` runner reads its own host env, so set the var there).
        _resolved = resolve_harness_command("cursor-native", default="", explicit=None, cfg=cfg)
        if _resolved:
            os.environ["OMNIGENT_CURSOR_PATH"] = _resolved
        if server is None:
            server = cfg.get("server")
        # Deliberately no ``cfg.get("model")`` fallback (unlike ``codex``): the
        # global config model is a Claude/Codex catalog id, not a cursor-agent
        # model id, and pinning it would break the cursor TUI launch. Cursor's
        # model is explicit-only here; persistent selection rides the web /model.
        auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)

        server = _ensure_backend(server)
        resolved_session_id = (
            choice.conversation_id if choice.conversation_id is not None else session_id
        )

        run_cursor_native(
            server=server,
            session_id=resolved_session_id,
            resume_picker=choice.picker,
            cursor_args=_resolve_harness_startup_args(cfg, "cursor-native", cursor_args),
            model=model,
            auto_open_conversation=auto_open_conversation,
            mode=mode,
        )

    @cli.command(
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
            "daemon-spawned runner to launch the Kiro TUI, and attaches this TTY. "
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
            "opens an interactive picker scoped to kiro-native sessions."
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
    @click.option("--model", default=None, help="Kiro model to use for the native chat.")
    @click.option("--effort", default=None, help="Kiro effort level to use for the native chat.")
    @click.option(
        "--agent", "kiro_agent", default=None, help="Kiro agent to use for the native chat."
    )
    @click.option(
        "--trust-tools",
        "trust_tools",
        multiple=True,
        metavar="TOOL",
        help="Trust a specific Kiro tool. May be passed multiple times.",
    )
    @click.option(
        "--trust-all-tools",
        is_flag=True,
        default=False,
        help="Explicitly trust all Kiro tools for this local launch.",
    )
    @click.option(
        "-p",
        "--prompt",
        default=None,
        help="Send this as the initial Kiro chat input when the TUI starts.",
    )
    @click.argument("kiro_args", nargs=-1, type=click.UNPROCESSED)
    def kiro(
        server: str | None,
        resume: str | None,
        session_id: str | None,
        model: str | None,
        effort: str | None,
        kiro_agent: str | None,
        trust_tools: tuple[str, ...],
        trust_all_tools: bool,
        prompt: str | None,
        kiro_args: tuple[str, ...],
    ) -> None:
        """Launch the Kiro TUI in an Omnigent terminal.

        \b
        Examples:
          omnigent kiro
          omnigent kiro --resume conv_abc123
          omnigent kiro --resume                  # interactive picker
          omnigent kiro --model auto -p "review this repo"
        """
        choice = _split_resume_value(resume)
        if session_id is not None and (choice.picker or choice.conversation_id is not None):
            raise click.UsageError(
                "--session and --resume are mutually exclusive; "
                "prefer --resume (--session is deprecated).",
            )
        _reject_reserved_kiro_resume_args(kiro_args)

        from omnigent.harness_startup_config import resolve_harness_command
        from omnigent.kiro_native import run_kiro_native

        cfg = _load_effective_config()
        # Thread ``--command`` / ``harness.kiro-native.command`` config into the
        # runner via the canonical ``OMNIGENT_KIRO_PATH`` env var (set before
        # ``_ensure_backend`` so a locally-spawned daemon inherits it; a remote
        # ``--server`` runner reads its own host env, so set the var there).
        _resolved = resolve_harness_command("kiro-native", default="", explicit=None, cfg=cfg)
        if _resolved:
            os.environ["OMNIGENT_KIRO_PATH"] = _resolved
        if server is None:
            server = cfg.get("server")
        if model is None:
            model = cfg.get("model")
        auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)
        launch_args = _build_kiro_launch_args(
            effort=effort,
            kiro_agent=kiro_agent,
            trust_tools=trust_tools,
            trust_all_tools=trust_all_tools,
            passthrough_args=_resolve_harness_startup_args(cfg, "kiro-native", kiro_args),
        )

        server = _ensure_backend(server)
        resolved_session_id = (
            choice.conversation_id if choice.conversation_id is not None else session_id
        )

        run_kiro_native(
            server=server,
            session_id=resolved_session_id,
            resume_picker=choice.picker,
            kiro_args=launch_args,
            model=model,
            prompt=prompt,
            auto_open_conversation=auto_open_conversation,
        )

    @cli.command(
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
            "daemon-spawned runner to launch the Goose TUI, and attaches this TTY. "
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
            "opens an interactive picker scoped to goose-native sessions."
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
    @click.argument("goose_args", nargs=-1, type=click.UNPROCESSED)
    def goose(
        server: str | None,
        resume: str | None,
        session_id: str | None,
        goose_args: tuple[str, ...],
    ) -> None:
        """Launch the Goose TUI in an Omnigent terminal.

        \b
        Examples:
          omnigent goose
          omnigent goose --resume conv_abc123
          omnigent goose --resume                 # interactive picker
        """
        choice = _split_resume_value(resume)
        if session_id is not None and (choice.picker or choice.conversation_id is not None):
            raise click.UsageError(
                "--session and --resume are mutually exclusive; "
                "prefer --resume (--session is deprecated).",
            )

        from omnigent.goose_native import run_goose_native
        from omnigent.harness_startup_config import resolve_harness_command

        cfg = _load_effective_config()
        # Thread ``--command`` / ``harness.goose-native.command`` config into the
        # runner via the canonical ``OMNIGENT_GOOSE_PATH`` env var (set before
        # ``_ensure_backend`` so a locally-spawned daemon inherits it; a remote
        # ``--server`` runner reads its own host env, so set the var there).
        _resolved = resolve_harness_command("goose-native", default="", explicit=None, cfg=cfg)
        if _resolved:
            os.environ["OMNIGENT_GOOSE_PATH"] = _resolved
        if server is None:
            server = cfg.get("server")
        auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)

        server = _ensure_backend(server)
        resolved_session_id = (
            choice.conversation_id if choice.conversation_id is not None else session_id
        )

        run_goose_native(
            server=server,
            session_id=resolved_session_id,
            resume_picker=choice.picker,
            goose_args=_resolve_harness_startup_args(cfg, "goose-native", goose_args),
            auto_open_conversation=auto_open_conversation,
        )

    @cli.command(
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
            "daemon-spawned runner to launch the Hermes TUI, and attaches this TTY. "
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
            "opens an interactive picker scoped to hermes-native sessions."
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
    @click.argument("hermes_args", nargs=-1, type=click.UNPROCESSED)
    def hermes(
        server: str | None,
        resume: str | None,
        session_id: str | None,
        hermes_args: tuple[str, ...],
    ) -> None:
        """Launch the Hermes TUI in an Omnigent terminal.

        \b
        Examples:
          omnigent hermes
          omnigent hermes --resume conv_abc123
          omnigent hermes --resume                 # interactive picker
        """
        choice = _split_resume_value(resume)
        if session_id is not None and (choice.picker or choice.conversation_id is not None):
            raise click.UsageError(
                "--session and --resume are mutually exclusive; "
                "prefer --resume (--session is deprecated).",
            )

        from omnigent.harness_startup_config import resolve_harness_command
        from omnigent.hermes_native import run_hermes_native

        cfg = _load_effective_config()
        # Thread ``--command`` / ``harness.hermes-native.command`` config into the
        # runner via the canonical ``OMNIGENT_HERMES_PATH`` env var (set before
        # ``_ensure_backend`` so a locally-spawned daemon inherits it; a remote
        # ``--server`` runner reads its own host env, so set the var there).
        _resolved = resolve_harness_command("hermes-native", default="", explicit=None, cfg=cfg)
        if _resolved:
            os.environ["OMNIGENT_HERMES_PATH"] = _resolved
        if server is None:
            server = cfg.get("server")
        auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)

        server = _ensure_backend(server)
        resolved_session_id = (
            choice.conversation_id if choice.conversation_id is not None else session_id
        )

        run_hermes_native(
            server=server,
            session_id=resolved_session_id,
            resume_picker=choice.picker,
            hermes_args=_resolve_harness_startup_args(cfg, "hermes-native", hermes_args),
            auto_open_conversation=auto_open_conversation,
        )

    @cli.command(
        context_settings={
            "ignore_unknown_options": True,
            "allow_extra_args": True,
        }
    )
    @click.option(
        "--server",
        default=None,
        help=(
            "Remote omnigent URL. Ensures the host daemon, binds a runner, "
            "launches Antigravity (agy) in a terminal resource, and attaches "
            'this TTY. Pass --server "" to auto-spawn a persistent local '
            "server in the background and use that instead of a remote one."
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
            "opens an interactive picker scoped to antigravity-native sessions."
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
    @click.option("--model", default=None, help="Antigravity (agy) model to use for the session.")
    @click.argument("antigravity_args", nargs=-1, type=click.UNPROCESSED)
    def antigravity(
        server: str | None,
        resume: str | None,
        session_id: str | None,
        model: str | None,
        antigravity_args: tuple[str, ...],
    ) -> None:
        """Launch the Antigravity (agy) TUI in an Omnigent terminal.

        \b
        Examples:
          omnigent antigravity
          omnigent antigravity --resume conv_abc123
          omnigent antigravity --resume                  # interactive picker
          omnigent antigravity --server https://<app>.databricksapps.com
        """
        # Validate option combinations BEFORE any side effects (daemon spawn,
        # server discovery) -- see the same comment in the claude command.
        choice = _split_resume_value(resume)
        if session_id is not None and (choice.picker or choice.conversation_id is not None):
            raise click.UsageError(
                "--session and --resume are mutually exclusive; "
                "prefer --resume (--session is deprecated).",
            )

        from omnigent.antigravity_native import run_antigravity_native
        from omnigent.harness_startup_config import resolve_harness_command

        cfg = _load_effective_config()
        if server is None:
            server = cfg.get("server")
        if model is None:
            model = cfg.get("model")
        auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)

        server = _ensure_backend(server)
        resolved_session_id = (
            choice.conversation_id if choice.conversation_id is not None else session_id
        )

        # permission_mode is left None here (parity with the claude/codex/pi CLI
        # launchers): the attended terminal launch lets agy's own request-review
        # prompt govern each tool, and an unattended/headless launch auto-bypasses
        # inside run_antigravity_native. It is plumbed through build_agy_launch so a
        # future caller CAN set it, but this human CLI path exposes no permission
        # flag and never needs one.
        resolved_command = resolve_harness_command(
            "antigravity-native",
            default="",
            explicit=None,
            cfg=cfg,
        )
        run_antigravity_native(
            server=server,
            session_id=resolved_session_id,
            resume_picker=choice.picker,
            antigravity_args=_resolve_harness_startup_args(
                cfg, "antigravity-native", antigravity_args
            ),
            model=model,
            auto_open_conversation=auto_open_conversation,
            command=resolved_command or None,
        )

    @cli.command(
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
            "daemon-spawned runner to launch the qwen TUI, and attaches this TTY. "
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
            "opens an interactive picker scoped to qwen-native sessions."
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
        """Launch the qwen (Qwen Code) TUI in an Omnigent terminal.

        \b
        Examples:
          omnigent qwen
          omnigent qwen --resume conv_abc123
          omnigent qwen --resume                  # interactive picker
        """
        choice = _split_resume_value(resume)
        if session_id is not None and (choice.picker or choice.conversation_id is not None):
            raise click.UsageError(
                "--session and --resume are mutually exclusive; "
                "prefer --resume (--session is deprecated).",
            )

        from omnigent.harness_startup_config import resolve_harness_command
        from omnigent.qwen_native import run_qwen_native

        cfg = _load_effective_config()
        # Thread ``--command`` / ``harness.qwen-native.command`` config into the
        # runner via the canonical ``OMNIGENT_QWEN_PATH`` env var (set before
        # ``_ensure_backend`` so a locally-spawned daemon inherits it; a remote
        # ``--server`` runner reads its own host env, so set the var there).
        _resolved = resolve_harness_command("qwen-native", default="", explicit=None, cfg=cfg)
        if _resolved:
            os.environ["OMNIGENT_QWEN_PATH"] = _resolved
        if server is None:
            server = cfg.get("server")
        auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)

        server = _ensure_backend(server)
        resolved_session_id = (
            choice.conversation_id if choice.conversation_id is not None else session_id
        )

        run_qwen_native(
            server=server,
            session_id=resolved_session_id,
            resume_picker=choice.picker,
            qwen_args=_resolve_harness_startup_args(cfg, "qwen-native", qwen_args),
            auto_open_conversation=auto_open_conversation,
        )

    @cli.command(
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
            "daemon-spawned runner to launch the Kimi TUI, and attaches this TTY. "
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
            "opens an interactive picker scoped to kimi-native sessions."
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
    @click.argument("kimi_args", nargs=-1, type=click.UNPROCESSED)
    def kimi(
        server: str | None,
        resume: str | None,
        session_id: str | None,
        kimi_args: tuple[str, ...],
    ) -> None:
        """Launch the Kimi Code TUI in an Omnigent terminal.

        Boots Moonshot AI's interactive ``kimi`` TUI
        (https://github.com/MoonshotAI/Kimi-Code) in a runner-owned terminal and
        attaches your TTY — the native experience, embedded in the Omnigent web
        UI. No Omnigent provider config is needed: kimi authenticates against its
        own backend (``kimi login`` for OAuth, or a Moonshot API key).

        For the headless SDK harness (per-turn ``kimi -p`` behind the Omnigent
        REPL) use ``omnigent run --harness kimi`` instead.

        \b
        Examples:
          omnigent kimi
          omnigent kimi --resume conv_abc123
          omnigent kimi --resume                   # interactive picker
        """
        choice = _split_resume_value(resume)
        if session_id is not None and (choice.picker or choice.conversation_id is not None):
            raise click.UsageError(
                "--session and --resume are mutually exclusive; "
                "prefer --resume (--session is deprecated).",
            )

        from omnigent.harness_startup_config import resolve_harness_command
        from omnigent.kimi_native import run_kimi_native

        cfg = _load_effective_config()
        # Thread ``--command`` / ``harness.kimi-native.command`` config into the
        # runner via the canonical ``OMNIGENT_KIMI_PATH`` env var (set before
        # ``_ensure_backend`` so a locally-spawned daemon inherits it; a remote
        # ``--server`` runner reads its own host env, so set the var there).
        _resolved = resolve_harness_command("kimi-native", default="", explicit=None, cfg=cfg)
        if _resolved:
            os.environ["OMNIGENT_KIMI_PATH"] = _resolved
        if server is None:
            server = cfg.get("server")
        auto_open_conversation = _resolve_auto_open_conversation_from_config(cfg)

        server = _ensure_backend(server)
        resolved_session_id = (
            choice.conversation_id if choice.conversation_id is not None else session_id
        )

        run_kimi_native(
            server=server,
            session_id=resolved_session_id,
            resume_picker=choice.picker,
            kimi_args=_resolve_harness_startup_args(cfg, "kimi-native", kimi_args),
            auto_open_conversation=auto_open_conversation,
        )
