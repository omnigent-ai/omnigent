"""Goose harness — ACP-driven, layered on the generic ACP executor.

Block's Goose (``goose acp``) runs through Omnigent's generic ACP executor, so
this package holds only what is genuinely Goose's: its tool-name dialect
(:mod:`.toolnames`), the extension that declares it
(:data:`GOOSE_ACP_EXTENSION`), the launch config that describes the CLI
(:func:`goose_agent_config`), and the harness wrap that injects both
(:mod:`.harness`). Nothing in the generic ACP path names Goose.

**Composition root.** This module assembles the extension from its parts, so a
new Goose-specific capability is a new sibling module plus one field here, and
the generic executor learns about it through
:class:`~omnigent.inner.acp_extension.AcpExtension` rather than a Goose import.

**Behavior vs. launch.** The split matters for where a change goes. Goose's
*behavior* (how to read a frame) composes in via the extension. Goose's *launch*
quirks are values on :class:`~omnigent.inner.acp_executor.AcpAgentConfig` —
forced approval mode, its ``GOOSE_`` env family, its sandbox roots, its
``--with-builtin`` argv. Unlike :mod:`omnigent.inner.devin`, whose wrap can
delegate straight to :func:`omnigent.inner.acp_harness.create_app` because a
catalog row fully describes it, Goose builds its own config from
``HARNESS_GOOSE_*`` — the four quirks below have no ``HARNESS_ACP_*`` spelling.

**Liftability.** The layout mirrors a community harness plugin
(``omnigent/community/harness/<name>/{plugin.py,inner/}``, per
https://omnigent.ai/docs/build/harnesses/community): a package of vendor code
plus a ``create_app()``. Moving Goose out of core is that move plus an entry
point, with the same two constraints :mod:`omnigent.inner.devin` documents — a
plugin may not override a builtin harness name, so the move deletes Goose's
builtin registry rows in the same change; and the plugin contract's public
surface is :class:`~omnigent.inner.executor.Executor` +
:class:`~omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter`, so an
out-of-core Goose either imports the ACP executor from core (a private module
today) or carries its own client, as ``omnigent-rovo`` does.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path

from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor
from omnigent.inner.acp_extension import AcpExtension
from omnigent.inner.datamodel import OSEnvSpec
from omnigent.inner.goose.toolnames import GooseToolNameSource

#: Goose builtin extensions loaded over ACP. ``developer`` is the core coding
#: toolset (shell + text editor); without an extension Goose has no tools to act
#: with. Overridable per session via ``HARNESS_GOOSE_BUILTINS``.
DEFAULT_BUILTINS: tuple[str, ...] = ("developer",)

#: Goose's approval mode, forced into the spawn env. Goose defaults to Auto,
#: which never sends ``session/request_permission``, leaving the TOOL_CALL policy
#: gate in :meth:`AcpExecutor._decide_permission` with nothing to gate. Pinned so
#: sensitive tool calls reach Omnigent policy before they run; an ambient
#: ``GOOSE_MODE=auto`` must not be able to switch enforcement off. ``approve``
#: (every call) is deliberately not used: a policy ALLOW still falls through to
#: human elicitation, so it would prompt on every tool.
_GOOSE_MODE_ENV = "GOOSE_MODE"
_GOOSE_APPROVAL_MODE = "smart_approve"

#: Everything the generic ACP executor needs to behave as Goose. Injected by
#: :func:`omnigent.inner.goose.harness.create_app`.
GOOSE_ACP_EXTENSION = AcpExtension(
    name="goose",
    tool_name_sources=(GooseToolNameSource(),),
)


def goose_provider_env(provider: str | None, model: str | None) -> dict[str, str]:
    """Build Goose's ``GOOSE_PROVIDER`` / ``GOOSE_MODEL`` overrides.

    Goose resolves its provider + credential from its own config
    (``goose configure`` → keyring / ``~/.config/goose/config.yaml``); these env
    vars only *override* the provider/model when the spec named one. Goose has no
    ``session/new`` model field, so the override has to ride in the spawn env.

    :param provider: Optional Goose provider id, e.g. ``"anthropic"``.
    :param model: Optional Goose model id, e.g. ``"claude-haiku-4-5"``.
    :returns: The override env, empty when neither was named.
    """
    env: dict[str, str] = {}
    if provider:
        env["GOOSE_PROVIDER"] = provider
    if model:
        env["GOOSE_MODEL"] = model
    return env


def goose_agent_config(
    *,
    goose_path: str = "goose",
    model: str | None = None,
    provider: str | None = None,
    builtins: Sequence[str] | None = None,
) -> AcpAgentConfig:
    """Describe Goose's launch to the generic ACP executor.

    Every Goose quirk that is a *value* rather than a behavior lives here, so the
    inherited wire code needs no Goose branches. Each one breaks something
    specific if dropped: no ``acp`` subcommand and it speaks the wrong protocol;
    no ``--with-builtin`` and Goose has no tools; no ``GOOSE_MODE`` and the
    TOOL_CALL gate never fires; no ``GOOSE_`` prefix and Goose cannot read its own
    configuration; no config/state roots and it cannot start inside a sandbox.

    :param goose_path: Path to the goose CLI binary; ``"goose"`` searches PATH.
    :param model: Optional ``GOOSE_MODEL`` override.
    :param provider: Optional ``GOOSE_PROVIDER`` override.
    :param builtins: Goose builtin extensions to load; ``None`` uses
        :data:`DEFAULT_BUILTINS`.
    :returns: The :class:`AcpAgentConfig` describing ``goose acp``.
    """
    loaded = tuple(builtins) if builtins is not None else DEFAULT_BUILTINS
    argv = [goose_path, "acp"]
    for builtin in loaded:
        argv.extend(["--with-builtin", builtin])

    config_dir = Path.home() / ".config" / "goose"
    state_dir = Path.home() / ".local" / "share" / "goose"

    return AcpAgentConfig(
        command=shlex.join(argv),
        name="Goose",
        model=model,
        # Goose assigns the session id in its ``session/new`` reply, and has no
        # ``session/new`` model field, so the model override rides in the env.
        session_id_mode="server",
        send_model_in_session_new=False,
        # Goose owns its whole ``GOOSE_*`` family as its configuration surface.
        env_allow_prefixes=("GOOSE_",),
        spawn_env={
            _GOOSE_MODE_ENV: _GOOSE_APPROVAL_MODE,
            **goose_provider_env(provider, model),
        },
        sandbox_read_roots=(config_dir,),
        sandbox_write_roots=(config_dir, state_dir),
    )


def build_goose_executor(
    *,
    cwd: str | None = None,
    os_env: OSEnvSpec | None = None,
    model: str | None = None,
    provider: str | None = None,
    goose_path: str | None = None,
    builtins: Sequence[str] | None = None,
) -> AcpExecutor:
    """Build the generic ACP executor configured and extended as Goose.

    The composition root's one callable: config plus extension, no subclass. Used
    by :mod:`.harness` and by the live e2e suite so both drive the same assembly.

    :param cwd: Working directory for the goose subprocess; ``None`` inherits.
    :param os_env: Environment / sandbox spec. When its ``sandbox`` is not
        ``"none"`` the whole ``goose`` process tree is wrapped in the platform
        sandbox, with Goose's config and state dirs added as roots so it can
        still start.
    :param model: Optional ``GOOSE_MODEL`` override.
    :param provider: Optional ``GOOSE_PROVIDER`` override.
    :param goose_path: Path to the goose CLI binary; ``None`` searches PATH.
    :param builtins: Goose builtin extensions to load.
    :returns: A ready :class:`AcpExecutor` speaking Goose's dialect.
    """
    return AcpExecutor(
        config=goose_agent_config(
            goose_path=goose_path or "goose",
            model=model,
            provider=provider,
            builtins=builtins,
        ),
        cwd=cwd,
        os_env=os_env,
        extension=GOOSE_ACP_EXTENSION,
    )


__all__ = [
    "DEFAULT_BUILTINS",
    "GOOSE_ACP_EXTENSION",
    "GooseToolNameSource",
    "build_goose_executor",
    "goose_agent_config",
    "goose_provider_env",
]
