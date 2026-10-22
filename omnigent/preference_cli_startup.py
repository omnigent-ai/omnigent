"""Apply shared intent once per CLI launch; inspection and resume remain local."""

from __future__ import annotations

import asyncio

import click

from omnigent.config import ResolvedConfig, resolve_effective_config
from omnigent.preference_startup import PreferenceLaunch, prepare_server_preferences

_NEW_SESSION_COMMANDS = frozenset(
    {
        "run",
        "claude",
        "codex",
        "pi",
        "opencode",
        "kimi",
        "qwen",
        "goose",
        "hermes",
        "cursor",
        "kiro",
        "agy",
        "antigravity",
        "copilot",
        "polly",
        "debby",
        "devin",
    }
)


def resolve_cli_preferences(
    global_config: dict[str, object],
    project_config: dict[str, object],
) -> ResolvedConfig:
    context = click.get_current_context(silent=True)
    legacy = resolve_effective_config(global_config, project_config)
    if context is None or context.info_name not in _NEW_SESSION_COMMANDS:
        return legacy
    if any(
        context.params.get(key)
        for key in ("resume", "resume_latest", "session_id", "fork_session_id")
    ):
        return legacy
    if context.meta.get("temporary_preferences"):
        return legacy
    server = context.params.get("server") or global_config.get("server")
    if not isinstance(server, str) or not server.startswith(("http://", "https://")):
        return legacy
    cache_key = f"preferences:{server}"
    if cache_key not in context.meta:
        try:
            launch = asyncio.run(prepare_server_preferences(server))
        except Exception as exc:
            raise click.ClickException(
                f"{exc}\nRetry, or use `omnigent --temporary-preferences COMMAND` for this launch."
            ) from exc
        context.meta[cache_key] = launch
        if launch is not None and launch.warning:
            click.echo(launch.warning, err=True)
    launch = context.meta[cache_key]
    if launch is None:
        return legacy
    assert isinstance(launch, PreferenceLaunch)
    return launch.resolve(global_config, project_config)


def temporary_preferences_option(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if value:
        ctx.meta["temporary_preferences"] = True
