"""Rich-based REPL for omnigent — built on the UI SDK framework.

The public API is ``run_repl(client, agent_name, tool_handler)``.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import inspect
import json
import logging
import os
import pathlib
import re
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, TextIO

from omnigent_client import (
    BlockContext,
    ElicitationRequestCtx,
    OmnigentClient,
    OmnigentError,
    ReasoningBlock,
    ResponseEndBlock,
    ResponseStartBlock,
    Session,
    StreamHooks,
    ToolExecution,
    ToolGroup,
    ToolHandler,
    ToolResultBlock,
    format_tool_args_brief,
)
from omnigent_ui_sdk import (
    DEFAULT_USER_CONFIG,
    OverlayTarget,
    PendingAttachment,
    RichBlockFormatter,
    TerminalHost,
    TerminalTheme,
    UserConfigError,
    load_user_config,
    save_user_config,
    update_user_config,
)

# ``FormattedItem`` is the SDK formatter's per-method return type
# (``Rich.RenderableType | StreamingText | StreamReplace``). The
# top-level package doesn't re-export it today, so import from the
# internal ``_formatter`` module — keeping the import explicit
# rather than retyping every formatter override as ``list[Any]``.
# When the SDK adds an explicit re-export this should switch to
# ``from omnigent_ui_sdk import FormattedItem``.
from omnigent_ui_sdk.terminal._completer import FileMentionCompleter
from omnigent_ui_sdk.terminal._formatter import FormattedItem
from omnigent_ui_sdk.terminal._theme import LIGHT_THEME, get_theme
from prompt_toolkit.completion import CompleteEvent, Completer, Completion, merge_completers
from prompt_toolkit.document import Document
from rich.console import RenderableType
from rich.text import Text

from omnigent.spec.types import SkillSpec

if TYPE_CHECKING:
    from omnigent.server.schemas import SessionStatusEvent

_log = logging.getLogger(__name__)


def _is_recoverable_sse_transport_error(exc: BaseException) -> bool:
    """Return ``True`` when *exc* is a transient SSE transport interruption.

    The REPL's persistent ``_stream_pump`` auto-reconnects on every
    exception. Some of those reconnects are normal background events:
    the peer closes a long-running chunked response (load-balancer
    idle-timeout, server restart, network blip), and the next
    subscription picks up the session on the server side without any
    user-visible impact. Logging those at WARNING level alarms users
    even though nothing is wrong, and confuses the
    transient transport interruption with the genuinely-bad provider
    error (orphaned ``function_call_output`` after compression) that
    actually kills a turn. Classifying the transport errors here lets
    us demote those to INFO while keeping a clear WARNING for anything
    we don't recognise.

    :param exc: The exception caught by ``_stream_pump``.
    :returns: ``True`` when *exc* (or any wrapped cause) is a known
        recoverable httpx / httpcore transport error.
    """
    # Import lazily — ``httpx`` is a project dependency, but keeping
    # the import local avoids forcing it on minimal test environments
    # that import this module without exercising the SSE pump.
    try:
        import httpcore
        import httpx
    except ImportError:
        return False
    recoverable_types: tuple[type[BaseException], ...] = (
        httpx.RemoteProtocolError,
        httpx.ReadError,
        httpx.ReadTimeout,
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpcore.RemoteProtocolError,
        httpcore.ReadError,
        httpcore.ReadTimeout,
        httpcore.ConnectError,
        httpcore.ConnectTimeout,
    )
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, recoverable_types):
            return True
        next_exc = current.__cause__ or current.__context__
        current = next_exc if isinstance(next_exc, BaseException) else None
    return False


# Type aliases for the slash-command dispatch contract. Every
# ``_cmd_*`` handler binds these in the same order; centralizing
# the alias keeps a future signature change to a single edit.
SlashCommandHandler = Callable[
    [str, Session, OmnigentClient, TerminalHost, RichBlockFormatter],
    Awaitable[None],
]


class _SessionSnapshot(Protocol):
    """
    Minimal snapshot shape returned by ``client.sessions``.

    :param agent_id: Durable agent id, e.g. ``"ag_abc123"``.
    :param agent_name: Human-readable name of the bound agent,
        e.g. ``"polly"``. Changes when the session is switched
        in place to a different agent; ``None`` when the server
        couldn't resolve the agent row (or an old server omits it).
    :param runner_id: Bound runner id, e.g. ``"runner_abc123"``,
        or ``None`` before binding.
    :param reasoning_effort: Session-level reasoning effort,
        e.g. ``"high"``, or ``None`` for the agent default.
    :param llm_model: LLM model identifier from the agent spec,
        e.g. ``"anthropic/claude-sonnet-4-6"``, or ``None`` when
        unavailable.
    :param context_window: Context window size in tokens looked up
        server-side, e.g. ``200_000``, or ``None`` when unknown.
    :param last_total_tokens: Provider-reported total tokens (input +
        output) from the most recently completed task, e.g. ``45231``,
        or ``None`` when no task has completed yet. Used to seed the
        context-ring on resume without waiting for the first response.
    """

    agent_id: str
    agent_name: str | None
    runner_id: str | None
    reasoning_effort: str | None
    llm_model: str | None
    context_window: int | None
    last_total_tokens: int | None


# Key-binding hints rendered on the welcome panel's second line.
# Single source of truth so every ``fmt.welcome(...)`` call — the
# initial banner, ``/new``, ``/switch`` — shows the same set. The
# ``Ctrl+O`` overlay is registered in :func:`run_repl`; ``/help``
# is a slash command (no keyboard binding because Ctrl+H aliases
# Backspace on essentially every terminal and F1 gets swallowed
# by iTerm2/Warp/tmux). Updating the hint without updating the
# binding would desync the user's expectation from what actually
# fires.
# NOTE: keep this list short enough that the bottom toolbar
# (``{model · state} … hints … state: sleeping``) fits the e2e PTY
# width (120 cols). Adding an entry here can wrap the toolbar and
# split the ``state: sleeping`` sync marker the e2e harness waits on
# (tests/e2e/omnigent/_pexpect_harness.py). /quit discoverability is
# served by the grouped ``/help`` output instead.
WELCOME_HINTS = ["/help help", "Ctrl+O debug", "Ctrl+T show tools", "Esc cancel", "Ctrl+C exit"]

# Per-request item count for ``client.sessions.list_items``
# pagination. Matches the server's
# ``ix_conversation_items_conversation_id_position`` cursor cap
# (see ``server/API.md`` § conversation items — asking for more
# returns HTTP 422). Used by
# :func:`_list_all_conversation_items` to walk every item in a
# conversation across as many pages as needed; the user-reported
# 2026-04-30 "17 of 20 terminals in Ctrl+O sidebar" symptom
# reproduced when the parent conversation had 217 items and the
# overlay's single-page fetch silently dropped everything past
# position 99.
_LIST_ITEMS_PAGE_SIZE = 100

# Sub-agent tree (state badge + ``↓`` menu). The depth cap mirrors web's
# ``MAX_TREE_DEPTH`` so the CLI tree matches the web Agents rail; the poll
# cadence refreshes deeper levels (the SSE stream only carries the active
# session's direct children) while sub-agents are active.
_MAX_SUBAGENT_TREE_DEPTH = 3
_SUBAGENT_POLL_SECONDS = 2.0


def _load_startup_theme() -> TerminalTheme:
    """Return the persisted startup theme, or run the interactive picker.

    On first launch (no persisted theme in ``~/.omnigent/config.yaml``),
    shows an interactive arrow-key theme picker before the REPL starts.
    The picker uses OSC 11 detection to pre-select dark or light based on
    the terminal's actual background, then persists the user's choice.

    On subsequent launches, returns the persisted theme directly.

    User config is a convenience preference, not required REPL state. If the
    config is corrupt or unreadable, startup should still succeed with the
    default theme; the user can repair or overwrite the file later with
    ``/theme``.
    """

    try:
        persisted_theme = load_user_config().theme
    except (UserConfigError, ValueError):
        return LIGHT_THEME

    if persisted_theme is not None:
        # Theme was previously saved — use it directly.
        try:
            return get_theme(persisted_theme)
        except ValueError:
            return LIGHT_THEME

    # First launch: no theme persisted yet. Show the interactive picker.
    from omnigent.repl._theme_picker import startup_theme_picker

    return startup_theme_picker()


# Raw ANSI dim/reset for the per-family creds line appended beneath the
# banner box (the box itself is a pre-formatted ANSI string; routing this
# one extra line through the box keeps the whole header a single stdout
# write — see the boot sequence's rationale for not using ``host.output``).
_ANSI_DIM = "\033[2m"
_ANSI_RESET = "\033[0m"


@dataclass(frozen=True)
class _StartupHeader:
    """Resolved data for the Claude-Code-style startup header box.

    Built by :func:`_build_startup_header` and consumed by
    :func:`_render_startup_banner_ansi`.

    :param folder: The working directory in ``~``-relative form, e.g.
        ``"~/omnigent"``.
    :param description: A one-line agent summary (first sentence of the
        spec ``description``, length-capped), e.g. ``"multi-agent coding
        orchestrator"``; ``None`` when the spec declares none.
    :param model_label: The resolved model id for the launch harness,
        e.g. ``"claude-sonnet-4-6"``; ``None`` when no model is pinned
        (a subscription / Databricks profile picks it at run time).
    :param credential: The launch harness's credential as glyph + label,
        e.g. ``"🧱 Databricks (my-ws)"`` — a subscription renders
        glyphless as ``"Subscription"`` (see :func:`_header_glyph`);
        ``None`` when none resolves (e.g. a remote-URL target with no
        local harness).
    :param creds_line: The per-family creds disclosure shown beneath the
        box for multi-vendor agents, e.g. ``"Claude → Subscription
        ·   Codex → Subscription"``; ``None`` for single-family
        agents (the box's credential row already says it).
    """

    folder: str
    description: str | None
    model_label: str | None
    credential: str | None
    creds_line: str | None


def _display_cwd() -> str:
    """Return the current working directory in ``~``-relative form.

    :returns: The cwd with ``$HOME`` collapsed to ``~`` (e.g.
        ``"~/omnigent"``), or the absolute path when it is not
        under the home directory.
    """
    import os

    cwd = os.getcwd()
    home = os.path.expanduser("~")
    if cwd == home:
        return "~"
    if cwd.startswith(home + os.sep):
        return "~" + cwd[len(home) :]
    return cwd


def _summarize_description(description: str | None) -> str | None:
    """Return a compact one-line summary of an agent's description.

    Collapses whitespace (spec descriptions are often YAML folded
    scalars carrying newlines), takes the first sentence, and caps the
    length so the header box stays compact.

    :param description: The raw spec ``description``, e.g. polly's
        ``"Multi-agent coding orchestrator. polly never …"``; ``None``
        when absent.
    :returns: A trimmed one-liner, e.g. ``"Multi-agent coding
        orchestrator"``, or ``None`` when *description* is empty.
    """
    import re

    if not description:
        return None
    text = re.sub(r"\s+", " ", description).strip()
    if not text:
        return None
    first = text.split(". ")[0].rstrip(".")
    max_len = 60
    if len(first) > max_len:
        first = first[: max_len - 1].rstrip() + "…"
    return first


def _header_glyph(kind: str) -> str:
    """Kind glyph for the startup header's credential labels.

    The header drops the subscription ADMISSION TICKETS glyph — its red
    rendering is too loud for the banner box — while every other kind
    keeps its :func:`kind_glyph`. CLI surfaces (``omnigent setup``, the
    ``/model`` readout) keep the ticket.

    :param kind: The provider kind, e.g. ``"subscription"`` or ``"key"``.
    :returns: The kind's glyph (e.g. ``"🔑"``), or ``""`` for the
        subscription kind.
    """
    from omnigent.onboarding.configure_models import kind_glyph
    from omnigent.onboarding.provider_config import SUBSCRIPTION_KIND

    return "" if kind == SUBSCRIPTION_KIND else kind_glyph(kind)


def _build_startup_header(
    harness: str | None,
    agent_description: str | None,
    used_families: list[str] | None,
) -> _StartupHeader:
    """Resolve the data for the startup header box + creds line.

    Reads the merged provider config to name the launch harness's model
    + credential and, for a multi-vendor agent (more than one family
    across its harnesses + sub-agents), each used family's configured
    credential. This function does no exception handling — a failure is
    the caller's cue to fall back to the plain banner.

    :param harness: The launch harness, e.g. ``"claude-sdk"``; ``None``
        for a remote-URL target with no local harness (then only folder
        + description are populated, no credential).
    :param agent_description: The agent spec's ``description`` (raw),
        e.g. polly's multi-line summary; ``None`` when absent.
    :param used_families: Harness surfaces the agent's harnesses (incl.
        sub-agents) consume, e.g. ``["anthropic", "openai", "pi"]`` for
        polly (a pi brain spawning claude/codex sub-agents); a list of
        length > 1 produces the per-surface creds line. ``None`` / a
        single surface omits it.
    :returns: The resolved :class:`_StartupHeader`.
    """
    from omnigent.onboarding.configure_models import (
        credential_label,
        family_label,
    )
    from omnigent.onboarding.detected import effective_config_with_detected
    from omnigent.onboarding.provider_config import (
        describe_active_credential,
        first_available_provider,
        load_config,
        surface_default_provider,
    )

    config = effective_config_with_detected(load_config())

    model_label: str | None = None
    credential: str | None = None
    if harness is not None:
        cred = describe_active_credential(config, harness)
        if cred is not None:
            model_label = cred.model
            cred_name = credential_label(cred.kind, cred.provider_name)
            credential = f"{_header_glyph(cred.kind)} {cred_name}".strip()

    creds_line: str | None = None
    families = used_families or []
    if len(families) > 1:
        parts: list[str] = []
        for fam in families:
            # Effective per-surface default — for the pi surface this is
            # what the pi harness would actually route through (explicit
            # pi scope, else the cross-family fallback).
            entry = surface_default_provider(config, fam)
            if entry is None:
                # No default for this surface — but a launch falls back to the
                # first credential that can serve it (the same
                # first_available_provider the runtime spawn-env builders use).
                # Name it so the header tells the truth: no default was chosen,
                # yet the head WILL launch through this one.
                fallback = first_available_provider(config, fam)
                if fallback is None:
                    label = "not configured"
                else:
                    cred_text = credential_label(
                        fallback.kind,
                        fallback.name,
                        profile=fallback.profile,
                        display_name=fallback.display_name,
                    )
                    label = (
                        f"no default → will use {_header_glyph(fallback.kind)} {cred_text}"
                    ).strip()
            else:
                cred_text = credential_label(
                    entry.kind,
                    entry.name,
                    profile=entry.profile,
                    display_name=entry.display_name,
                )
                label = f"{_header_glyph(entry.kind)} {cred_text}".strip()
            parts.append(f"{family_label(fam)} → {label}")
        creds_line = "   ·   ".join(parts)

    return _StartupHeader(
        folder=_display_cwd(),
        description=_summarize_description(agent_description),
        model_label=model_label,
        credential=credential,
        creds_line=creds_line,
    )


def _render_startup_banner_ansi(
    ui_name: str,
    *,
    server_url: str | None = None,
    server_version: str | None = None,
    header: _StartupHeader | None = None,
) -> str:
    """
    Build the ANSI-styled startup banner shown when the REPL boots.

    Renders the mascot art + accent-bordered box, using the SDK's
    starfish magenta-pink brand color (``#F43BA6``) so the box border,
    mascot, prompt marker, and bottom toolbar all read as one accent.

    When *header* is supplied, the box becomes a Claude-Code-style header:
    the agent name (bold) plus dim rows for the one-line summary, the
    model + credential, the working folder, and the server URL (shown for
    any target, loopback included) with the installed version appended
    inline as ``"<url>  ·  server <ver>"``; a per-family creds line is
    appended beneath the box for multi-vendor agents. When *header* is
    ``None`` the box keeps its minimal form — just the name, with the
    server URL taking the single info row when the host is non-loopback
    (keybinding hints live in the bottom toolbar, so the hint row is
    omitted).

    :param ui_name: Humanized agent label shown bold at the top of the box.
    :param server_url: Base URL the REPL is connected to. In the header
        box it's shown for any target (including a local
        ``http://127.0.0.1:<port>`` dev server); the minimal banner still
        surfaces it only when the host is non-loopback. ``None`` skips it.
    :param server_version: Installed server version (e.g. ``"0.3.0.dev0"``)
        from a best-effort ``GET /v1/info`` probe, rendered inline on the
        server-URL row (or its own row if there's no URL). ``None`` (probe
        failed /
        not attempted) skips the row. Only consulted on the *header* path.
    :param header: Resolved header data (folder / model / credential /
        summary / creds line) from :func:`_build_startup_header`, or
        ``None`` for the minimal banner.
    :returns: ANSI-styled string ready to be written to stdout.
    """
    from omnigent.conversation_browser import display_server_url, is_workspace_hosted_url
    from omnigent.inner.banner import BannerLine, startup_banner_strings

    remote = _is_remote_server_url(server_url)
    # User-facing form of the URL: a Databricks workspace-hosted server is
    # connected to on its ``/api/2.0/omnigent`` API mount, but the banner
    # should show the recognizable workspace ``/omnigent`` URL. Non-Databricks
    # URLs pass through unchanged. The probe still uses the real base URL via
    # the client; only the displayed string is mapped.
    display_url = display_server_url(server_url) if server_url else server_url
    # Suppress the version on Databricks workspace mounts — a workspace build
    # has no meaningful version string to show (authoritative gate; the call
    # site also skips the probe there, but this guarantees it never renders
    # regardless of caller).
    version = (
        None
        if (server_url is not None and is_workspace_hosted_url(server_url))
        else server_version
    )

    if header is None:
        banner = startup_banner_strings(
            ui_name,
            hint_line=display_url if remote else "",
            art_color="#F43BA6",
        )
        return banner.ansi

    info_lines: list[BannerLine] = []
    if header.description:
        info_lines.append(BannerLine(header.description, dim=True))
    # Model + credential on one row: "<model>  ·  <glyph credential>".
    # Either part may be absent (a subscription with no pinned model shows
    # just the credential; a remote target with no local harness shows
    # neither, so the row is skipped).
    if header.model_label and header.credential:
        info_lines.append(BannerLine(f"{header.model_label}  ·  {header.credential}", dim=True))
    elif header.credential:
        info_lines.append(BannerLine(header.credential, dim=True))
    elif header.model_label:
        info_lines.append(BannerLine(header.model_label, dim=True))
    info_lines.append(BannerLine(header.folder, dim=True))
    # Server URL + installed version on one row: "<url>  ·  server <ver>".
    # The URL is shown for ANY server target, loopback included — a local
    # dev server (``http://127.0.0.1:<port>``) is meaningful context here,
    # so "which server am I on / what version is it" reads as one line. The
    # version comes from a best-effort ``GET /v1/info`` probe; when it's
    # unresolved (slow / old server) only the URL shows, and when there's no
    # URL at all the version stands on its own row.
    if display_url is not None:
        url_row = display_url
        if version:
            url_row = f"{display_url}  ·  server {version}"
        info_lines.append(BannerLine(url_row, dim=True))
    elif version:
        info_lines.append(BannerLine(f"server {server_version}", dim=True))

    banner = startup_banner_strings(ui_name, info_lines=info_lines, art_color="#F43BA6")
    if header.creds_line:
        # A playful lead-in + the per-vendor creds line, both dim, beneath the
        # box. The creds line renders only for a multi-vendor agent, which
        # always means it spawns sub-agents of another vendor — so inviting the
        # user to "spawn" them is accurate. Indented to the REPL's left margin.
        lead = f"Try asking {ui_name} to spawn the following sub-agents!"
        return (
            f"{banner.ansi}\n\n"
            f"  {_ANSI_DIM}{lead}{_ANSI_RESET}\n"
            f"  {_ANSI_DIM}{header.creds_line}{_ANSI_RESET}"
        )
    return banner.ansi


async def _fetch_server_version(client: OmnigentClient) -> str | None:
    """Best-effort server version for the header row, with a legacy fallback.

    Tries ``GET /v1/info`` → ``server_version`` first, then falls back to
    the long-standing ``GET /api/version`` → ``version`` endpoint. The
    fallback matters for older servers (e.g. a staging deployment that
    predates ``server_version`` landing in ``/v1/info``): ``/api/version``
    has reported the same installed version for far longer, so the row
    still fills in instead of waiting for that server to redeploy. Both
    return the identical ``importlib.metadata`` version, so the fallback is
    not a different value, just an older surface.

    Routed through the REPL's already-connected :class:`OmnigentClient` so
    the probe carries the SAME auth (bearer / cookie), base URL, and TLS /
    custom-CA configuration the REPL is already using. These endpoints are
    NOT universally unauthed — a hosted deployment (behind OIDC / accounts /
    a Databricks front door) gates them like any other route, so a bare
    credential-less GET would 401 and the version would silently never show
    on exactly the remote servers where the URL row is displayed. Reusing
    the authenticated client makes the probe answer there.

    Because the client's ``httpx.AsyncClient`` is awaited directly (not run
    on a thread), this never blocks the event loop. Each request is bounded
    *per phase* (connect / read / write each 1.0s) so the worst case a
    healthy-but-slow or unreachable server can add to the previously-instant
    banner stays small — the connect phase, the dominant cost for an
    unreachable host, fails within a second (and a dead host fails the first
    request, so the fallback adds no latency there). Any failure —
    unreachable, slow, 401/4xx/5xx, non-JSON, or a server too old to report
    either field — returns ``None`` and the banner simply omits the version
    row. A welcome-banner detail must never block or fail REPL boot, so this
    swallows every error.

    :param client: The connected client the REPL drives; its authenticated
        ``_http`` and ``_base_url`` are reused for the probe.
    :returns: The installed server version string (e.g. ``"0.3.0.dev0"``),
        or ``None`` when it can't be resolved.
    """
    import httpx

    timeout = httpx.Timeout(1.0)
    # (endpoint, response key) pairs tried in order: the richer capabilities
    # probe first, then the legacy version endpoint older servers still have.
    for path, key in (("/v1/info", "server_version"), ("/api/version", "version")):
        try:
            resp = await client._http.get(f"{client._base_url}{path}", timeout=timeout)
            version = resp.json().get(key)
        except Exception:  # noqa: BLE001 — startup-UI boundary: never block boot on a banner detail
            return None
        if isinstance(version, str) and version:
            return version
    return None


def _is_remote_server_url(url: str | None) -> bool:
    """True if *url* points at a host other than loopback.

    A local ``omnigent run`` spawns its own Omnigent server on
    ``http://127.0.0.1:<port>``; surfacing that URL in the
    welcome banner adds noise without information. A user
    running with ``--server <url>`` is talking to a different
    process — possibly on another machine — and showing the URL
    is meaningful context.

    :param url: Base URL string, e.g. ``"http://127.0.0.1:6767"``
        or ``"https://example.databricks.com"``. ``None`` returns
        ``False``.
    :returns: ``True`` when *url* parses to a non-loopback host.
    """
    if not url:
        return False
    import ipaddress
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if not host or host == "localhost":
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Hostname (not an IP literal) — treat as remote.
        return True


def _humanize_agent_name(agent_name: str) -> str:
    """
    Convert an agent's wire name (from the YAML's ``name:``
    field) to the spaces-not-separators form shown in the
    welcome banner.

    Centralized so every banner-rendering site agrees — the
    initial ``run_repl`` banner, the ``/new`` reset banner,
    the ``/switch`` redraw banner, and the
    "Resumed conversation …" line all stay consistent.
    Without this, ``resume_test`` (the wire form) would
    render in some banners and ``resume test`` (humanized)
    in others, producing the visible mismatch reported on
    the user-facing welcome panel.

    :param agent_name: Agent's registered name, e.g.
        ``"resume_test"`` or ``"my-agent"``.
    :returns: The display form, e.g. ``"resume test"`` or
        ``"my agent"``.
    """
    return agent_name.replace("-", " ").replace("_", " ")


class TimedFormatter(RichBlockFormatter):  # type: ignore[misc]
    """Shows final elapsed time after response completes."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._start_time: float | None = None

    def format_response_start(self, block: ResponseStartBlock) -> list[FormattedItem]:
        self._start_time = block.ctx.timestamp
        return super().format_response_start(block)

    def format_response_end(self, block: ResponseEndBlock) -> list[FormattedItem]:
        items = super().format_response_end(block)
        if self._start_time is not None:
            elapsed = block.ctx.timestamp - self._start_time
            items.append(Text.from_markup(f"   [{self.muted}]{elapsed:.1f}s[/{self.muted}]"))
            self._start_time = None
        return items


class _ApprovalVerdict(enum.Enum):
    """
    How the user answered a policy approval prompt.

    Three-way rather than boolean so the REPL can distinguish
    "approve just this one" from "approve and stop asking for
    the rest of this session". Mirrors the Claude Code model
    (y / A / n); same muscle memory transfers.

    - ``APPROVE_ONCE`` — allow this one request only.
    - ``APPROVE_ALWAYS`` — allow this request AND remember the
      decision for the rest of the REPL session. Future asks
      from the same policy at the same phase auto-approve
      without prompting.
    - ``REFUSE`` — refuse this request. Fail-closed default
      per POLICIES.md §13; anything not explicitly approve is
      a refusal.
    """

    APPROVE_ONCE = "approve_once"
    APPROVE_ALWAYS = "approve_always"
    REFUSE = "refuse"


# Input-token vocabulary for each verdict, case-insensitive.
# Both short and long forms accepted so muscle memory from
# other tools (aider's "y", claude-code's "yes") carries over.
# Anything outside these sets is a REFUSE — fail-closed per
# POLICIES.md §13.
_APPROVE_ONCE_TOKENS: frozenset[str] = frozenset({"y", "yes", "approve", "ok"})
_APPROVE_ALWAYS_TOKENS: frozenset[str] = frozenset(
    {"a", "always", "yes always", "approve always"},
)


def _parse_approval_input(text: str) -> _ApprovalVerdict:
    """
    Classify a line of user input as one of the three verdicts.

    Case-insensitive, whitespace-stripped. ALWAYS tokens are
    checked before ONCE tokens so the lone letter ``a`` is
    treated as "always" rather than ambiguously falling
    through to the refuse default.

    :param text: Raw user input from the main REPL prompt.
    :returns: The parsed verdict.
    """
    normalized = text.strip().lower()
    if normalized in _APPROVE_ALWAYS_TOKENS:
        return _ApprovalVerdict.APPROVE_ALWAYS
    if normalized in _APPROVE_ONCE_TOKENS:
        return _ApprovalVerdict.APPROVE_ONCE
    return _ApprovalVerdict.REFUSE


class _ApprovalState:
    """
    Per-REPL holder for pending approvals and the session
    auto-approve cache.

    Owning an object (rather than module globals) keeps
    multiple REPL sessions in the same process isolated —
    tests can spin up two :func:`run_repl` invocations and
    their state doesn't collide.

    Two pieces of state:

    1. The currently-pending approval :class:`asyncio.Future`
       (``None`` when no ASK is in flight). The hook creates
       it via :meth:`begin`; the main input loop resolves it
       via :meth:`resolve`. Using a future avoids the stdin /
       ``patch_stdout`` fight that a direct ``input()`` call
       produced.
    2. The session auto-approve cache: a set of
       ``(policy_name, phase)`` pairs the user said "always"
       to. Future ASKs matching one of these entries skip the
       prompt and auto-approve. Scoped to this REPL run —
       restart wipes the cache.
    """

    def __init__(self) -> None:
        """Start with no pending approval and an empty cache."""
        self._future: asyncio.Future[bool] | None = None
        # Current ASK's identity — captured on ``begin`` so
        # ``resolve_verdict`` can stash the pair on an
        # APPROVE_ALWAYS without the caller having to re-pass
        # ctx fields.
        self._current_policy: str | None = None
        self._current_phase: str | None = None
        # When True, the current approval is URL-mode-only and
        # keyboard input (y/a/n) should be rejected.
        self._url_mode: bool = False
        # (policy_name, phase) → "approve always" cache.
        # ``phase`` comes from the server as a string
        # (``"request"``, ``"tool_call"``, ...) so storing the
        # pair as-is avoids any re-parsing overhead.
        self._always: set[tuple[str, str]] = set()

    @property
    def pending(self) -> bool:
        """:returns: ``True`` iff an approval is awaiting a verdict."""
        return self._future is not None and not self._future.done()

    def is_pre_approved(self, policy_name: str, phase: str) -> bool:
        """
        Look up an earlier "always" decision.

        Called by the approval hook BEFORE rendering anything —
        a pre-approved ASK must produce no UI noise. The cache
        key is specifically ``(policy_name, phase)``; different
        policies or different phases still prompt even if the
        user approved a related one.

        :param policy_name: Deciding policy's name from the
            :class:`ElicitationRequestCtx`.
        :param phase: Phase string from the ctx (``"request"`` /
            ``"tool_call"`` / etc.).
        :returns: ``True`` iff the user previously answered
            "always" for this policy+phase pair.
        """
        return (policy_name, phase) in self._always

    def remember_always(self, policy_name: str, phase: str) -> None:
        """
        Cache an "approve always" decision for the rest of the
        session.

        Idempotent — adding a duplicate entry is a no-op. The
        cache is NEVER persisted to disk; closing ``omnigent chat``
        clears it, so the next session starts from a clean
        slate. That matches what users expect from
        session-scoped approvals in other tools.

        :param policy_name: Deciding policy's name.
        :param phase: Phase string.
        """
        self._always.add((policy_name, phase))

    def begin(
        self, policy_name: str, phase: str, *, url_mode: bool = False
    ) -> asyncio.Future[bool]:
        """
        Start a new approval — create the future the hook awaits.

        Records the identity of the ASK so
        :meth:`resolve_verdict` can cache an "always" decision
        against the right ``(policy_name, phase)`` pair
        without the caller having to re-pass them.

        If a previous approval's future is still open (the user
        never answered before a new ASK arrived), refuse the
        old one fail-closed and replace it. In practice the
        server only has one parked workflow per REPL at a
        time, so this is defense-in-depth.

        :param policy_name: Deciding policy's name from the
            :class:`ElicitationRequestCtx`.
        :param phase: Phase string from the ctx.
        :returns: The future to await. Resolves to ``True`` on
            approve (one or always) and ``False`` on refuse.
        """
        if self._future is not None and not self._future.done():
            self._future.set_result(False)
        self._current_policy = policy_name
        self._current_phase = phase
        self._url_mode = url_mode
        self._future = asyncio.get_running_loop().create_future()
        return self._future

    def resolve_verdict(self, verdict: _ApprovalVerdict) -> bool:
        """
        Resolve a pending approval with a three-way verdict.

        On :attr:`_ApprovalVerdict.APPROVE_ALWAYS`, caches
        ``(current_policy, current_phase)`` so subsequent
        ASKs for that pair auto-approve without prompting.
        On any other verdict, the cache is untouched.

        :param verdict: The user's answer.
        :returns: ``True`` iff a pending approval existed and
            was resolved. ``False`` when there was nothing to
            resolve (the caller should route input normally).
        """
        if self._future is None or self._future.done():
            return False
        approved = verdict != _ApprovalVerdict.REFUSE
        if (
            verdict == _ApprovalVerdict.APPROVE_ALWAYS
            and self._current_policy is not None
            and self._current_phase is not None
        ):
            self.remember_always(self._current_policy, self._current_phase)
        self._future.set_result(approved)
        self._future = None
        self._current_policy = None
        self._current_phase = None
        return True

    def cancel(self) -> None:
        """
        Cancel any pending approval — refuse fail-closed.

        Called on REPL teardown or when the user ``/cancel``s
        an in-progress response to avoid leaking an unresolved
        future. Does NOT clear the "always" cache — that
        persists for the REPL session.
        """
        if self._future is not None and not self._future.done():
            self._future.set_result(False)
        self._future = None
        self._current_policy = None
        self._current_phase = None


class _FieldInputState:
    """Collect free-form field values one at a time via the main input loop.

    Same future-based pattern as :class:`_ApprovalState` — no direct
    ``input()`` calls so ``prompt_toolkit``'s ``patch_stdout`` is
    never disrupted.
    """

    def __init__(self) -> None:
        self._future: asyncio.Future[str] | None = None
        self._field_name: str | None = None
        # Set by ``cancel`` so the field-collection loop can tell an
        # abort (Esc on the turn) apart from an empty submit and stop
        # prompting rather than advancing to — and re-prompting for —
        # the next field after the turn is already gone.
        self._aborted: bool = False

    @property
    def pending(self) -> bool:
        return self._future is not None and not self._future.done()

    @property
    def field_name(self) -> str | None:
        return self._field_name

    @property
    def aborted(self) -> bool:
        """:returns: ``True`` if collection was cancelled mid-prompt."""
        return self._aborted

    def begin(self, field_name: str) -> asyncio.Future[str]:
        # A fresh prompt is never pre-aborted. Cleared here (not in the
        # collection loop) so the flag spans exactly one begin/await
        # cycle: the loop reads it the instant the await returns, and
        # the only await between fields IS this ``begin``.
        self._aborted = False
        if self._future is not None and not self._future.done():
            self._future.set_result("")
        self._field_name = field_name
        self._future = asyncio.get_running_loop().create_future()
        return self._future

    def resolve(self, text: str) -> bool:
        if self._future is None or self._future.done():
            return False
        self._future.set_result(text)
        self._future = None
        self._field_name = None
        return True

    def cancel(self) -> None:
        self._aborted = True
        if self._future is not None and not self._future.done():
            self._future.set_result("")
        self._future = None
        self._field_name = None


def _build_elicitation_content_from_schema(
    schema: dict[str, object],
) -> dict[str, object] | None:
    """
    Delegate to the shared schema auto-fill utility.

    See :func:`omnigent.tools._elicitation_schema.build_accept_content_from_schema`
    for the full algorithm and docstring.

    :param schema: The ``requestedSchema`` dict from the
        elicitation event. May be empty ``{}``.
    :returns: A flat ``{field: value}`` dict, or ``None``.
    """
    from omnigent.tools._elicitation_schema import build_accept_content_from_schema

    return build_accept_content_from_schema(schema)  # type: ignore[arg-type]


def _make_elicitation_prompt(
    host: TerminalHost,
    fmt: RichBlockFormatter,
    state: _ApprovalState,
    server_url: str | None = None,
) -> Callable[[ElicitationRequestCtx], Awaitable[bool]]:
    """
    Build the ``on_elicitation_request`` hook for the REPL.

    When the server emits an MCP-shape elicitation
    (``response.elicitation_request`` SSE event — today the
    primary producer is the policy ASK flow), the SDK routes
    it to this hook. Two paths:

    - Pre-approved: the user previously said "always" for this
      ``(policy_name, phase)`` pair. Skip all UI, auto-accept.
      Print a short muted line so the transcript records that
      an auto-accept fired — silent auto-acceptance would be
      security-hostile (user forgets they once said "always").
    - Fresh elicitation: render the preview, offer three
      options (``y`` / ``a`` / ``n``), await a future resolved
      by the main input loop.

    This hook does NOT touch stdin or call :func:`input` —
    under the REPL's active ``prompt_toolkit`` session, any
    direct stdin read fights ``patch_stdout`` and produces
    the "characters disappear / auto-delete" jank. Reusing
    the main input loop means typing the verdict works
    exactly like typing any other message. See POLICIES.md
    §7 + ``designs/SERVER_HARNESS_CONTRACT.md`` §"Universal
    API additions".

    The bool return is collapsed to MCP ``action`` by the SDK:
    ``True`` → ``"accept"``, ``False`` → ``"decline"``. The
    REPL's three-way verdict (once / always / refuse) maps to
    bool the same way — "always" still accepts the current
    elicitation; the difference is purely the session-cache
    write.

    :param host: The active :class:`TerminalHost` whose
        output channel we render the request on.
    :param fmt: Formatter whose accent / muted styles we
        reuse for visual consistency with the rest of the
        REPL.
    :param state: Shared :class:`_ApprovalState` that couples
        this hook to the main input loop and holds the
        session auto-approve cache.
    :returns: Async callable suitable for
        :attr:`StreamHooks.on_elicitation_request`.
    """

    async def _on_elicitation_request(ctx: ElicitationRequestCtx) -> bool:
        """
        Render the elicitation and await the main loop's verdict.

        :param ctx: Parsed elicitation carrying the message
            (combined reason from deciding policies), deciding
            policy name, phase, and a truncated preview of the
            gated content.
        :returns: ``True`` on user accept (one or always);
            ``False`` otherwise.
        """
        if state.is_pre_approved(ctx.policy_name, ctx.phase):
            # Audit line — don't be silent when auto-approving,
            # the user might have forgotten they flipped it on.
            host.output(
                Text.from_markup(
                    f"   [{fmt.muted}]auto-approved · "
                    f"{ctx.policy_name} · {ctx.phase}[/{fmt.muted}]",
                ),
            )
            return True

        host.output(
            Text.from_markup(
                f"\n [{fmt.warning}]⚠ approval required · {ctx.phase}[/{fmt.warning}]",
            ),
        )
        host.output(
            Text.from_markup(
                f"   [{fmt.muted}]policy: {ctx.policy_name}[/{fmt.muted}]",
            ),
        )
        if ctx.message:
            host.output(
                Text.from_markup(
                    f"   [{fmt.muted}]reason: {ctx.message}[/{fmt.muted}]",
                ),
            )
        if ctx.content_preview:
            preview = ctx.content_preview
            if len(preview) > 200:
                preview = preview[:200] + "…"
            host.output(
                Text.from_markup(
                    f"   [{fmt.muted}]preview:[/{fmt.muted}] {preview}",
                ),
            )
        _is_external_url = (
            ctx.mode == "url"
            and isinstance(ctx.url, str)
            and not ctx.url.startswith("/approve/")
            and server_url
        )
        if _is_external_url:
            # External URL (OAuth, MCP server, etc.) — show the link,
            # block keyboard approval.
            full_url = f"{server_url.rstrip('/')}{ctx.url}"
            host.output(
                Text.from_markup(f"   [{fmt.accent}]approve:[/{fmt.accent}]"),
            )
            host.output(Text(full_url))
        else:
            # Our own URL or form mode — use keyboard y/a/n.
            host.output(
                Text.from_markup(
                    f"   [{fmt.accent}]y = approve once, "
                    f"a = approve always (this session), "
                    f"n = refuse[/{fmt.accent}]",
                ),
            )
        future = state.begin(ctx.policy_name, ctx.phase, url_mode=bool(_is_external_url))
        return await future

    return _on_elicitation_request


def _elicitation_resolve_session_id(sdk_event: object, fallback_session_id: str) -> str:
    """
    Pick the session a parked elicitation's verdict must be POSTed to.

    A sub-agent's approval prompt is mirrored into its ancestors' streams
    so the human watching the parent chat sees it, but the elicitation
    Future lives on the *child* session that parked on it. The mirrored
    event carries the child id in ``target_session_id``; when present the
    verdict must go there, otherwise resolving against the ancestor stream
    404s and the sub-agent stays blocked. Own-session elicitations leave
    ``target_session_id`` unset and fall back to the stream's session.

    :param sdk_event: The translated
        :class:`omnigent_client._events.ElicitationRequest`; its
        ``target_session_id`` is read when set.
    :param fallback_session_id: The session the event was received on,
        e.g. ``"conv_parent123"``. Used when the elicitation is not a
        mirrored child prompt.
    :returns: The session id to resolve against, e.g. ``"conv_child123"``
        for a mirrored prompt or ``fallback_session_id`` otherwise.
    """
    return getattr(sdk_event, "target_session_id", None) or fallback_session_id


def _server_event_to_sdk_event(event: object) -> object | None:
    """Translate a server-shape ``ServerStreamEvent`` into an SDK-shape event.

    :class:`SessionsChat` yields validated server-side Pydantic
    events; the REPL renderer consumes the SDK-shape dataclasses in
    :mod:`omnigent_client._events`. Returns ``None`` for variants
    the renderer doesn't consume (forward-compatible skip).
    """
    from omnigent_client._events import (
        CompactionCompleted,
        CompactionInProgress,
        ElicitationRequest,
        ReasoningDelta,
        ReasoningStarted,
        ReasoningSummaryDelta,
        ResponseCancelled,
        ResponseCompleted,
        ResponseCreated,
        ResponseFailed,
        ResponseIncomplete,
        ResponseInProgress,
        ResponseQueued,
        TextDelta,
    )
    from omnigent_client._events import (
        ErrorEvent as SDKErrorEvent,
    )
    from omnigent_client._types import ErrorInfo
    from omnigent_client._types import Response as SDKResponse

    from omnigent.server.schemas import (
        CancelledEvent,
        ClientTaskCancelEvent,
        CompactionCompletedEvent,
        CompactionInProgressEvent,
        CompletedEvent,
        CreatedEvent,
        ElicitationRequestEvent,
        ErrorEvent,
        FailedEvent,
        IncompleteEvent,
        InProgressEvent,
        OutputItemDoneEvent,
        OutputTextDeltaEvent,
        QueuedEvent,
        ReasoningStartedEvent,
        ReasoningSummaryTextDeltaEvent,
        ReasoningTextDeltaEvent,
    )

    def _resp(env: object) -> SDKResponse:
        raw = env.response.model_dump()  # type: ignore[attr-defined]
        return SDKResponse.from_dict(raw)

    if isinstance(event, CreatedEvent):
        return ResponseCreated(response=_resp(event))
    if isinstance(event, QueuedEvent):
        return ResponseQueued(response=_resp(event))
    if isinstance(event, InProgressEvent):
        return ResponseInProgress(response=_resp(event))
    if isinstance(event, CompletedEvent):
        return ResponseCompleted(response=_resp(event))
    if isinstance(event, FailedEvent):
        return ResponseFailed(response=_resp(event))
    if isinstance(event, CancelledEvent):
        return ResponseCancelled(response=_resp(event))
    if isinstance(event, IncompleteEvent):
        resp = _resp(event)
        reason = resp.incomplete_details.reason if resp.incomplete_details else ""
        return ResponseIncomplete(response=resp, reason=reason)
    if isinstance(event, OutputTextDeltaEvent):
        return TextDelta(delta=event.delta)
    if isinstance(event, ReasoningStartedEvent):
        return ReasoningStarted()
    if isinstance(event, ReasoningTextDeltaEvent):
        return ReasoningDelta(delta=event.delta)
    if isinstance(event, ReasoningSummaryTextDeltaEvent):
        return ReasoningSummaryDelta(delta=event.delta)
    if isinstance(event, ElicitationRequestEvent):
        params = event.params
        return ElicitationRequest(
            elicitation_id=event.elicitation_id,
            message=params.message,
            requested_schema=params.requestedSchema or {},
            mode=params.mode,
            phase=params.phase or "",
            policy_name=params.policy_name or "",
            content_preview=params.content_preview or "",
            url=params.url,
            # Mirrored sub-agent prompts carry the child session id so the
            # verdict is POSTed back to the child that parked on it, not
            # the ancestor stream the event was relayed onto.
            target_session_id=params.target_session_id,
        )
    if isinstance(event, CompactionInProgressEvent):
        return CompactionInProgress()
    if isinstance(event, CompactionCompletedEvent):
        return CompactionCompleted()
    if isinstance(event, ErrorEvent):
        return SDKErrorEvent(
            source=event.source,
            tool_name=event.tool_name,
            error=ErrorInfo(
                code=event.error.code,
                message=event.error.message,
            ),
        )
    # OutputItemDoneEvent and ClientTaskCancelEvent are returned
    # as-is (not translated to SDK events) — the adapter handles
    # them directly for client-side tool execution.
    if isinstance(event, (OutputItemDoneEvent, ClientTaskCancelEvent)):
        return event
    return None


class _SessionsChatReplAdapter:
    """
    Sessions-API adapter for the REPL.

    Drives all server I/O through ``/v1/sessions``. A persistent
    SSE stream pump pushes every event through an ``_on_event``
    callback (set by :func:`run_repl`) that renders directly to
    the terminal. ``send()`` just POSTs the user message and
    waits for the turn-terminal event.

    Duck-compatible with the legacy :class:`Session` surface
    (``send``, ``cancel``, ``model``, ``current_response_id``,
    ``is_streaming``, ``reset``, ``resume_from_response``,
    ``set_reasoning_effort``, ``reasoning_effort``,
    ``set_model_override``, ``model_override``).
    """

    def __init__(
        self,
        client: OmnigentClient,
        agent_name: str,
        tool_callables: dict[str, object] | None = None,
        hooks: StreamHooks | None = None,
        session_id: str | None = None,
        session_bundle: bytes | None = None,
        session_bundle_filename: str = "agent.tar.gz",
        runner_id: str | None = None,
        runner_recover: Callable[[], str] | None = None,
        on_session_start: Callable[[str], None] | None = None,
        harness: str | None = None,
        attach_only: bool = False,
        field_input_state: _FieldInputState | None = None,
        host: TerminalHost | None = None,
        fmt: RichBlockFormatter | None = None,
    ) -> None:
        """
        Wire the adapter; do NOT issue any HTTP calls.

        :param client: The :class:`OmnigentClient` used to
            build the chat helper on first :meth:`send`.
        :param agent_name: Human-readable agent name for
            display.
        :param tool_callables: Optional name → callable mapping
            for client-side tools. When present, the adapter
            detects ``action_required`` tool call events in the
            stream and executes them locally.
        :param hooks: Optional lifecycle hooks. The
            ``on_elicitation_request`` hook is invoked when the
            server emits an elicitation event.
        :param session_id: When set, attach to this existing
            session instead of creating a new one on first
            :meth:`send`. Used by ``--continue`` / ``--resume``
            resume. ``None`` (default) creates a fresh session.
        :param session_bundle: Gzipped agent tarball bytes used to
            create a fresh session, e.g. bytes sent as the
            multipart ``bundle`` part. Required when
            ``session_id`` is ``None``.
        :param session_bundle_filename: Filename for the multipart
            upload, e.g. ``"agent.tar.gz"``.
        :param runner_id: Registered runner id to bind before the
            first turn, e.g. ``"runner_0123456789abcdef"``.
        :param runner_recover: Optional callback that returns the
            currently online runner id, restarting the local runner
            first if needed.
        :param on_session_start: Optional callback invoked once
            after a session id is known, e.g. ``lambda id: ...``.
        :param harness: The launch harness (e.g. ``"codex"``), known
            locally from the spec / ``--harness`` flag. Seeds the
            ``/model`` readout's harness so it's correct *before* the
            first turn binds the session (the snapshot's ``harness``
            then confirms/refreshes it). ``None`` for URL targets where
            the harness is only known after the snapshot.
        :param attach_only: When ``True``, run as a pure co-drive client:
            never bind/recover a runner (turns post to the session's
            existing host-bound runner). Used by ``omnigent attach``.
            ``False`` (default) is the runner-owning ``run`` path.
        :param field_input_state: Shared state for collecting schema
            field values interactively via the main input loop.
        :param host: Terminal output channel for rendering field prompts.
        :param fmt: Formatter for styling field prompts.
        """
        self._client = client
        self._agent_id: str | None = None
        self._agent_name = agent_name
        self._tool_callables = tool_callables
        self._hooks = hooks or StreamHooks()
        self._field_input_state = field_input_state
        self._host = host
        self._fmt = fmt
        self._session_id: str | None = session_id
        self._session_bundle = session_bundle
        self._session_bundle_filename = session_bundle_filename
        self._runner_id = runner_id
        self._runner_recover = runner_recover
        # Attach/co-drive mode: this client does NOT own a runner. It posts
        # turns to the session's already-bound runner (the host's), exactly
        # like the web UI co-drive, and never PATCHes the runner binding —
        # binding is owner-only, and re-binding would be a no-op even for the
        # owner. ``attach_only`` short-circuits all runner bind/recover logic.
        self._attach_only = attach_only
        # Set while observing another session read-only (e.g. diving into a
        # running sub-agent via :meth:`view_session`). Suppresses every
        # runner-bind PATCH — including the periodic ``_runner_recover_watch``
        # watchdog — so observing a sub-agent never hijacks its runner or
        # disturbs the owned session's binding.
        self._readonly_view = False
        # Set while CO-DRIVING a sub-agent interactively from the ↓ selector:
        # the displayed session is a child the user is chatting with, sends are
        # POSTed to the CHILD's existing runner (co-drive, like the web UI), and
        # the runner binding is NOT moved. Tracked SEPARATELY from
        # ``_readonly_view`` (which stays ``True`` in this mode so the tree root
        # stays frozen on the parent and no bind PATCH fires) so that enabling
        # sends never re-roots the selector — Left-arrow still returns to the
        # parent/root after a chat. See :meth:`view_session`.
        self._interactive_child = False
        self._on_session_start = on_session_start
        self._session_start_notified = False
        self._bound_runner_id: str | None = None
        # Push-based event callback. The pump calls this for every
        # event — always, regardless of whether send() is active.
        # Set by run_repl() to the rendering callback.
        self._on_event: Callable[[object], None] | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._recover_task: asyncio.Task[None] | None = None
        self._recover_lock = asyncio.Lock()
        self._bind_lock = asyncio.Lock()
        # Serializes _ensure_session so concurrent callers don't
        # race to create duplicate sessions.
        self._ensure_session_lock = asyncio.Lock()
        self._last_runner_recovery_error_key: tuple[str, str, str, str] | None = None
        self._current_response_id: str | None = None
        self._is_streaming: bool = False
        self._reasoning_effort: str | None = None
        # Session-local /model override. This is an LLM model
        # override (not an agent switch) applied by this adapter when
        # dispatching future turns through the sessions route.
        self._model_override: str | None = None
        self._llm_model: str | None = None
        self._harness: str | None = harness
        self._context_window: int | None = None
        self._last_total_tokens: int | None = None
        self._pending_local_tasks: dict[str, asyncio.Task[None]] = {}
        # FIFO counter: local sends are already echoed by ``on_input``,
        # so their ``session.input.consumed`` events are suppressed.
        self._pending_local_user_sends: int = 0
        # Locally invoked skill commands are echoed immediately by
        # the command handler; suppress the matching visible
        # ``slash_command`` item when it arrives on the live stream.
        self._pending_local_skill_slash_commands: list[tuple[str, str]] = []

    async def _recover_runner_if_needed(self) -> None:
        """
        Refresh the local runner id from the recovery callback.

        The callback owns process supervision. The adapter only
        observes the returned runner id and clears its cached binding
        when that id changes so the next ``PATCH /v1/sessions/{id}``
        writes the new affinity.

        :returns: None.
        """
        if self._runner_recover is None:
            return
        async with self._recover_lock:
            runner_id = await asyncio.to_thread(self._runner_recover)
            if runner_id != self._runner_id:
                self._runner_id = runner_id
                self._bound_runner_id = None

    async def _runner_recover_watch(self) -> None:
        """
        Keep a resumed session bound to a live local runner.

        The CLI recovery callback owns process supervision. This
        watchdog only invokes it periodically while the REPL is open,
        then reuses the same last-write-wins session PATCH used by
        create and resume.

        :returns: None.
        """
        _dbg = bool(os.environ.get("OMNIGENT_SESSIONS_ADAPTER_DEBUG"))
        while True:
            try:
                await asyncio.sleep(1.0)
                await self._recover_runner_if_needed()
                if self._session_id is not None:
                    await self._bind_runner_if_needed()
                self._clear_runner_recovery_error()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _log.warning("Runner recovery watchdog failed", exc_info=exc)
                self._emit_runner_recovery_error_once(exc)
                if _dbg:
                    print(
                        f"[sessions-adapter] runner recovery watchdog failed: {exc!r}",
                        file=sys.stderr,
                        flush=True,
                    )

    @property
    def session_id(self) -> str | None:
        """
        The durable session id once :meth:`send` has run at least once.

        Exposed so the REPL's debug overview and conversation-item
        fetchers can look up the session_id directly from the
        adapter without round-tripping through ``responses.get``.

        :returns: The session id, e.g. ``"conv_abc123"``, or
            ``None`` if no send has happened yet.
        """
        return self._session_id

    @property
    def model(self) -> str:
        """
        Return the agent's human-readable name.

        :returns: The agent name, e.g. ``"hello-world"``.
        """
        return self._agent_name

    @property
    def current_response_id(self) -> str | None:
        """
        Most recent ``response.created`` id observed on the SSE stream.

        Updated inside :meth:`send` as :class:`ResponseCreated`
        events fly past. ``None`` before the first turn.

        :returns: The response id, e.g. ``"resp_abc123"``, or
            ``None`` if no turn has run yet.
        """
        return self._current_response_id

    @property
    def is_streaming(self) -> bool:
        """
        Whether a turn is currently being streamed.

        :returns: ``True`` while :meth:`send` is iterating its
            async generator; ``False`` before/after.
        """
        return self._is_streaming

    @property
    def reasoning_effort(self) -> str | None:
        """
        Per-session reasoning-effort hint.

        Reads the locally cached value; the authoritative copy
        lives on the server (set via ``PATCH /v1/sessions/{id}``
        in :meth:`set_reasoning_effort`).

        :returns: The effort string (e.g. ``"high"``) or
            ``None`` if unset.
        """
        return self._reasoning_effort

    @property
    def model_override(self) -> str | None:
        """
        Current per-session LLM model override, or ``None`` for the
        agent spec default.

        This mirrors the legacy SDK helper's property so the shared
        ``/model`` slash command works in the sessions-backed REPL.
        """
        return self._model_override

    async def set_model_override(self, model: str | None) -> None:
        """
        Set or clear the session-local LLM model override.

        Before the session exists, caches the requested value locally;
        :meth:`_ensure_session` then PATCHes it onto the session row
        immediately after ``POST /v1/sessions`` returns, so the first
        event's workflow already sees ``conv.model_override`` via the
        server-side fallback. After the session exists, persists
        through ``PATCH /v1/sessions/{id}`` (matching
        :meth:`set_reasoning_effort`) so the web picker and the
        REPL stay in sync on the next snapshot read.

        :param model: New model identifier, e.g. ``"claude-opus-4-7"``,
            or ``None`` to clear to the agent default.
        :raises ValueError: If *model* is a string that is empty
            after trimming.
        """
        if model is not None:
            normalized = model.strip()
            if not normalized:
                raise ValueError("model override must be a non-empty string")
            model = normalized
        if self._session_id is None:
            self._model_override = model
            return
        session = await self._client.sessions.set_model_override(
            self._session_id,
            model_override=model,
        )
        self._model_override = session.model_override

    @property
    def llm_model(self) -> str | None:
        """
        LLM model identifier from the bound agent's spec.

        Populated from the server's ``SessionResponse.llm_model``
        field on the first successful session fetch. ``None`` until
        the session has been hydrated or when the agent has no
        explicit ``llm:`` block.

        :returns: The model identifier, e.g.
            ``"anthropic/claude-sonnet-4-6"``, or ``None``.
        """
        return self._llm_model

    @property
    def harness(self) -> str | None:
        """
        The bound agent's canonical harness, e.g. ``"openai-agents"``.

        Populated from the server's ``SessionResponse.harness`` on the first
        session fetch. The ``/model`` readout uses it to describe the active
        credential for the correct provider *family* instead of guessing
        from the model string. ``None`` until hydrated / when unavailable.

        :returns: The canonical harness name, or ``None``.
        """
        return self._harness

    @property
    def context_window(self) -> int | None:
        """
        Context window size in tokens for the bound agent's LLM.

        Populated from the server's ``SessionResponse.context_window``
        field (looked up server-side via litellm) on the first
        successful session fetch. ``None`` until the session has been
        hydrated or when the model is not found in the litellm
        registry.

        :returns: Token count, e.g. ``200_000``, or ``None``.
        """
        return self._context_window

    async def set_reasoning_effort(self, effort: str | None) -> None:
        """
        Set or clear session reasoning effort.

        Before the session exists, caches the requested value so it
        can be sent in the multipart ``POST /v1/sessions`` metadata.
        After creation/resume, persists through
        ``PATCH /v1/sessions/{id}`` and updates the cache from the
        authoritative server snapshot.

        :param effort: New effort, e.g. ``"high"``, or ``None``
            to clear.
        """
        if self._session_id is None:
            self._reasoning_effort = effort
            return
        session = await self._client.sessions.set_reasoning_effort(
            self._session_id,
            reasoning_effort=effort,
        )
        self._reasoning_effort = session.reasoning_effort

    async def compact(self) -> None:
        """
        Request explicit context compaction for the current session.

        :raises RuntimeError: If no session exists yet.
        """
        if self._session_id is None:
            raise RuntimeError("No active conversation to compact")
        await self._client.sessions.compact(self._session_id)

    def _hydrate_from_session_snapshot(self, session: _SessionSnapshot) -> None:
        """
        Copy mutable session fields from a sessions API snapshot.

        :param session: Snapshot returned by ``client.sessions``.
            It must expose ``agent_id``, ``agent_name``, ``runner_id``,
            ``reasoning_effort``, ``model_override``, ``llm_model``,
            ``harness``, ``context_window``, and ``last_total_tokens``
            attributes.
        :returns: None.
        """
        self._agent_id = session.agent_id
        # The agent name changes when the session is switched in place
        # to a different agent (web UI "Switch agent"). Don't clobber
        # the launch-time name when the snapshot omits it (old server
        # or unresolved agent row).
        if session.agent_name:
            self._agent_name = session.agent_name
        self._bound_runner_id = session.runner_id
        # Don't clobber a runner if it is revived after timeout
        if self._runner_recover is None and session.runner_id:
            self._runner_id = session.runner_id
        self._reasoning_effort = session.reasoning_effort
        self._model_override = session.model_override
        self._llm_model = session.llm_model
        # Don't clobber a launch-provided harness with a None from the
        # snapshot (e.g. an agent the server couldn't resolve a spec for).
        if session.harness is not None:
            self._harness = session.harness
        self._context_window = session.context_window
        self._last_total_tokens = session.last_total_tokens

    async def _ensure_session(self) -> str:
        """
        Lazily create the session and start the persistent stream.

        Serialized by ``_ensure_session_lock`` so that concurrent
        ``send()`` calls (rapid-fire messages before the first turn
        starts) don't each create a separate session and race on
        the runner-bind PATCH.

        Set ``OMNIGENT_SESSIONS_ADAPTER_DEBUG=1`` to trace
        construction + per-event flow on stderr.

        :returns: The durable session id, e.g. ``"conv_abc123"``.
        """
        _dbg = bool(os.environ.get("OMNIGENT_SESSIONS_ADAPTER_DEBUG"))
        await self._recover_runner_if_needed()
        if self._session_id is not None and self._stream_task is not None:
            return self._session_id
        async with self._ensure_session_lock:
            if self._session_id is not None and self._stream_task is not None:
                return self._session_id
            if self._session_id is None:
                if self._session_bundle is None:
                    raise RuntimeError(
                        "Sessions API fresh session creation requires a local agent bundle. "
                        "Start the REPL from `omnigent run <agent.yaml>` so the CLI can "
                        "upload the bundle through POST /v1/sessions."
                    )
                if _dbg:
                    print(
                        "[sessions-adapter] POST /v1/sessions multipart bundle",
                        file=sys.stderr,
                        flush=True,
                    )
                # Snapshot pre-create /model pick before hydration
                # clobbers it; PATCHed below since create() has no
                # model_override metadata field.
                pending_model_override = self._model_override
                session = await self._client.sessions.create(
                    self._session_bundle,
                    filename=self._session_bundle_filename,
                    reasoning_effort=self._reasoning_effort,
                    # Record the user's terminal cwd so the Web UI
                    # can show "running locally in <workspace>" for
                    # CLI sessions. Doesn't drive any behavior —
                    # CLI sessions don't bind to a host_id, so the
                    # ck_conversations_workspace_required_for_host
                    # constraint isn't active.
                    workspace=os.getcwd(),
                )
                self._session_id = session.id
                self._hydrate_from_session_snapshot(session)
                if pending_model_override is not None and session.model_override is None:
                    # PATCH the pre-session ``/model`` pick so the
                    # first event picks it up via conv.model_override.
                    # ``silent`` skips the tmux ``/model`` forward —
                    # the user already typed the command locally; we
                    # don't want a second copy injected into the pane.
                    try:
                        patched = await self._client.sessions.set_model_override(
                            self._session_id,
                            model_override=pending_model_override,
                            silent=True,
                        )
                        self._model_override = patched.model_override
                    except Exception:  # noqa: BLE001 — REPL boundary; log and clear
                        _log.warning(
                            "Failed to apply pending /model=%r to session %s; "
                            "clearing local cache.",
                            pending_model_override,
                            self._session_id,
                            exc_info=True,
                        )
                        self._model_override = None
                if _dbg:
                    print(
                        f"[sessions-adapter] session created id={self._session_id!r}",
                        file=sys.stderr,
                        flush=True,
                    )
            else:
                if _dbg:
                    print(
                        f"[sessions-adapter] resuming existing session id={self._session_id!r}",
                        file=sys.stderr,
                        flush=True,
                    )
                session = await self._client.sessions.get(self._session_id)
                self._hydrate_from_session_snapshot(session)
            await self._bind_runner_if_needed()
            if self._stream_task is None:
                self._stream_task = asyncio.create_task(
                    self._stream_pump(),
                    name=f"sessions-adapter-stream-{self._session_id}",
                )
            if self._runner_recover is not None and self._recover_task is None:
                self._recover_task = asyncio.create_task(
                    self._runner_recover_watch(),
                    name=f"sessions-adapter-recover-{self._session_id}",
                )
            self._notify_session_start_once()
            return self._session_id

    def _notify_session_start_once(self) -> None:
        """
        Invoke the session-start callback once after a session id is known.

        :returns: None.
        """
        if self._session_start_notified or self._session_id is None:
            return
        self._session_start_notified = True
        if self._on_session_start is not None:
            self._on_session_start(self._session_id)

    async def _bind_runner_if_needed(self) -> None:
        """
        Patch this session to the current registered runner.

        The sessions API has one dispatch precondition: a session
        must be bound to an online runner before a turn is posted.
        ``PATCH /v1/sessions/{id}`` is last-write-wins, so resume
        and recover use the same call as first bind.

        :raises RuntimeError: If the adapter has no session id or no
            runner id.
        """
        # Attach/co-drive clients never bind: they post turns to the
        # session's existing host-bound runner. Binding is owner-only
        # server-side, so a non-owner attach must not PATCH it. The same
        # holds while observing a sub-agent read-only — binding there would
        # hijack the child's runner and orphan the parent.
        if self._attach_only or self._readonly_view:
            return
        async with self._bind_lock:
            if self._session_id is None:
                raise RuntimeError("Cannot bind runner before a session exists")
            if self._runner_id is None:
                raise RuntimeError(
                    "Sessions API dispatch requires a registered runner id. "
                    "Start through `omnigent run <agent>` or pass --server so the CLI "
                    "can launch and bind a runner."
                )
            if self._bound_runner_id == self._runner_id:
                self._clear_runner_recovery_error()
                return
            _dbg = bool(os.environ.get("OMNIGENT_SESSIONS_ADAPTER_DEBUG"))
            if _dbg:
                print(
                    f"[sessions-adapter] PATCH /v1/sessions/{self._session_id} "
                    f"runner_id={self._runner_id!r}",
                    file=sys.stderr,
                    flush=True,
                )
            session = await self._client.sessions.bind_runner(
                self._session_id,
                runner_id=self._runner_id,
            )
            self._hydrate_from_session_snapshot(session)
            self._clear_runner_recovery_error()
            if _dbg:
                print(
                    f"[sessions-adapter] runner bound id={self._bound_runner_id!r}",
                    file=sys.stderr,
                    flush=True,
                )

    def _clear_runner_recovery_error(self) -> None:
        """
        Mark the runner recovery path healthy after a successful bind.

        :returns: None.
        """
        self._last_runner_recovery_error_key = None

    def _emit_runner_recovery_error_once(self, exc: Exception) -> None:
        """
        Render a runner recovery failure once per failure transition.

        Background recovery failures cannot bubble to ``send()`` or
        prompt-toolkit will leave the user at an apparently live prompt
        with a dead runner. Emitting the same typed server error event
        shape used by normal streams lets the existing REPL renderer
        show the error panel without adding a second rendering path.

        :param exc: Failure raised while relaunching or rebinding a
            runner, e.g. an SDK ``OmnigentError`` from
            ``PATCH /v1/sessions/{id}``.
        :returns: None.
        """
        if self._on_event is None:
            return

        code = str(getattr(exc, "code", "") or "")
        status_code = str(getattr(exc, "status_code", "") or "")
        key = (type(exc).__name__, code, status_code, str(exc))
        if key == self._last_runner_recovery_error_key:
            return
        self._last_runner_recovery_error_key = key

        from omnigent.server.schemas import ErrorEvent, RetryErrorDetail

        message = self._runner_recovery_error_message(exc)
        self._on_event(
            ErrorEvent(
                type="response.error",
                source="execution",
                error=RetryErrorDetail(
                    code=code or "runner_recovery_failed",
                    message=message,
                ),
            )
        )

    def _runner_recovery_error_message(self, exc: Exception) -> str:
        """
        Build the user-facing message for a recovery failure.

        Server-declared runner state errors are terminal until the
        session is rebound to an online runner. Transport and other
        unexpected failures are treated as transient because the
        watchdog and stream pump will retry with backoff.

        :param exc: Failure raised while relaunching or rebinding a
            runner.
        :returns: Message rendered in the REPL error panel.
        """
        detail = str(exc) or repr(exc)
        if self._is_terminal_runner_recovery_error(exc):
            return f"Runner recovery failed: {detail}"
        return f"Runner recovery hit a transient error and will retry: {detail}"

    def _is_terminal_runner_recovery_error(self, exc: Exception) -> bool:
        """
        Return whether a recovery failure requires user action.

        :param exc: Failure raised while relaunching or rebinding a
            runner.
        :returns: ``True`` for typed server runner-state errors,
            ``False`` for transport-style failures that should keep
            retrying.
        """
        if not isinstance(exc, OmnigentError):
            return False
        code = exc.code or ""
        return code in {"conflict", "invalid_input", "runner_unavailable"} or (
            exc.status_code is not None and 400 <= exc.status_code < 500
        )

    async def switch_to_session(self, new_session_id: str) -> str:
        """
        Re-point the adapter at a different existing session.

        Unbinds the runner from the prior session so the 1:1
        session↔runner invariant holds, cancels the SSE pump bound to
        the old session id, hydrates the new session snapshot, PATCHes
        the runner binding onto the new session, and restarts the
        pump. Called by ``/switch``.

        :param new_session_id: Conversation/session id to attach to,
            e.g. ``"conv_abc123"``.
        :returns: The new session id (echoed back).
        :raises Exception: SDK errors from ``sessions.get`` or the
            bind PATCH propagate; ``/switch`` renders them inline.
            The unbind is soft-failed on old servers (see
            :meth:`_unbind_runner_soft`).
        """
        # Switching to a top-level session re-establishes runner ownership,
        # so clear any read-only-view suppression (and interactive-child
        # co-drive) left over from diving into a sub-agent — otherwise the bind
        # below (and every later bind) no-ops and the switched-to session can
        # never dispatch a turn.
        self._readonly_view = False
        self._interactive_child = False
        old_session_id = self._session_id
        if old_session_id is not None and old_session_id != new_session_id:
            await self._unbind_runner_soft(old_session_id)
        if self._stream_task is not None:
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stream_task
            self._stream_task = None

        self._session_id = new_session_id

        session = await self._client.sessions.get(new_session_id)
        self._hydrate_from_session_snapshot(session)
        await self._bind_runner_if_needed()

        self._stream_task = asyncio.create_task(self._stream_pump())
        return new_session_id

    async def view_session(
        self, new_session_id: str, *, read_only: bool, interactive: bool = False
    ) -> str:
        """Re-point the displayed session WITHOUT moving runner bindings.

        Unlike :meth:`switch_to_session` (a top-level ``/switch`` that
        unbinds the old session's runner and PATCHes this REPL's runner onto
        the new one), this only re-points the SSE stream + displayed session
        id. It never unbinds the prior session nor binds the target — so an
        active sub-agent keeps running on its own runner, and the parent
        keeps the runner binding it needs to receive the sub-agent's result.

        Used to dive into a sub-agent's conversation from the inline menu:
        moving the *binding* there would orphan the parent (it could no longer
        wake to collect the result) and hijack the child's runner, leaving the
        sub-agent stuck "still running" with nothing delivered.

        :param new_session_id: Session to observe, e.g. ``"conv_child123"``.
        :param read_only: ``True`` while observing a sub-agent (suppresses
            all runner-bind PATCHes via ``_readonly_view``); ``False`` when
            returning to the owned top-level session so its runner-affinity
            watchdog resumes.
        :param interactive: ``True`` to CO-DRIVE the child — the user can send
            messages, which POST to the child's existing runner (like the web
            UI) with NO bind move. Only meaningful with ``read_only=True``
            (interactive implies observing a child); it stays read-only of the
            *runner binding* while lifting the plain-send guard. Returning to
            the root passes ``interactive=False``.
        :returns: The observed session id (echoed back).
        """
        # Apply the displayed-session state atomically up front (no await in
        # between), so the background root-tracking poll never observes a
        # half-applied switch (session id moved but flags not yet, or vice
        # versa) and mistakes the sub-agent for the tree root. ``_readonly_view``
        # stays the binding-suppression flag; ``_interactive_child`` separately
        # gates sends, so co-driving a child never re-roots the selector.
        self._session_id = new_session_id
        self._readonly_view = read_only
        self._interactive_child = interactive and read_only
        if self._stream_task is not None:
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stream_task
            self._stream_task = None
        session = await self._client.sessions.get(new_session_id)
        self._hydrate_from_session_snapshot(session)
        self._stream_task = asyncio.create_task(self._stream_pump())
        return new_session_id

    async def aclose(self) -> None:
        """
        Stop the background stream pump and local tool tasks.

        :returns: None.
        """
        if self._stream_task is not None:
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stream_task
            self._stream_task = None
        if self._recover_task is not None:
            self._recover_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recover_task
            self._recover_task = None
        for task in list(self._pending_local_tasks.values()):
            task.cancel()
        if self._pending_local_tasks:
            await asyncio.gather(
                *self._pending_local_tasks.values(),
                return_exceptions=True,
            )
            self._pending_local_tasks.clear()

    async def _stream_pump(self) -> None:
        """Subscribe to ``/v1/sessions/{id}/stream`` indefinitely.

        Subscribes to the session's SSE stream and pushes every
        event through ``_on_event``, which renders directly to the
        terminal (text streaming, tool call panels, lifecycle
        headers). Auto-reconnects on disconnect with backoff.

        Turn completion tracking is built into the pump itself:
        when a ``session.status`` event with status ``idle`` or
        ``failed`` arrives, ``_turn_done`` is set regardless of
        whether ``_on_event`` is wired. This ensures ``send()``
        never hangs even when the adapter is used without
        ``run_repl()`` (e.g. integration tests).

        Cancelled on REPL exit via ``_stream_task.cancel()``.
        """
        from omnigent.server.schemas import SessionStatusEvent as _StatusEv

        _dbg = bool(os.environ.get("OMNIGENT_SESSIONS_ADAPTER_DEBUG"))
        backoff = 0.5
        max_backoff = 5.0
        assert self._session_id is not None
        while True:
            try:
                if _dbg:
                    print(
                        f"[sessions-adapter] subscribing /stream {self._session_id}",
                        file=sys.stderr,
                        flush=True,
                    )
                async for event in self._client.sessions.stream(self._session_id):
                    if isinstance(event, _StatusEv) and event.status in (
                        "idle",
                        "waiting",
                        "failed",
                    ):
                        turn_done = getattr(self, "_turn_done", None)
                        if turn_done is not None:
                            turn_done.set()
                    if self._on_event is not None:
                        self._on_event(event)
                # Clean close (server sent [DONE]). Reopen.
                await asyncio.sleep(backoff)
                backoff = 0.5
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect on any error
                # Recoverable transport errors (peer closed mid-chunk,
                # read timeout, transient network error) are normal
                # background noise — the session continues server-side
                # and the next subscription picks up where we left off.
                # Emit a one-line INFO so the TUI doesn't paint a fresh
                # multi-line traceback every reconnect, and keep the
                # full postmortem behind a DEBUG sibling for engineers
                # who flip logging to DEBUG. Genuinely unexpected
                # failures still get a WARNING with the traceback.
                if _is_recoverable_sse_transport_error(exc):
                    _log.info("SSE transport interrupted, reconnecting")
                    _log.debug("recoverable SSE disconnect", exc_info=exc)
                else:
                    _log.warning("SSE stream error, reconnecting", exc_info=exc)
                if self._runner_recover is not None:
                    try:
                        await self._recover_runner_if_needed()
                        await self._bind_runner_if_needed()
                        self._clear_runner_recovery_error()
                    except Exception as recover_exc:  # noqa: BLE001
                        _log.warning(
                            "Runner recover after stream error failed",
                            exc_info=recover_exc,
                        )
                        with contextlib.suppress(Exception):
                            self._emit_runner_recovery_error_once(recover_exc)
                        if _dbg:
                            print(
                                "[sessions-adapter] runner recover after stream "
                                f"error failed: {recover_exc!r}",
                                file=sys.stderr,
                                flush=True,
                            )
                if _dbg:
                    print(
                        f"[sessions-adapter] /stream error: {exc!r}; "
                        f"reconnecting in {backoff:.1f}s",
                        file=sys.stderr,
                        flush=True,
                    )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def send(
        self,
        input: str | list[dict[str, object]],
        *,
        files: list[str] | None = None,
    ):
        """
        Post a user message. Rendering is push-based via ``_on_event``.

        The persistent stream pump delivers every event through the
        ``_on_event`` callback — there is no queue or drain loop.
        ``send()`` just POSTs the message and waits for the turn to
        complete (terminal event). All rendering happens in the
        callback, which the pump calls for every event regardless of
        whether a ``send()`` is in flight.

        :param input: User text or content blocks. Strings are
            wrapped in a single ``input_text`` block.
        :param files: Optional file paths to upload and attach.
        :yields: SDK-shape terminal events for callers that still
            iterate the session surface.
        """
        import mimetypes

        _dbg = bool(os.environ.get("OMNIGENT_SESSIONS_ADAPTER_DEBUG"))
        session_id = await self._ensure_session()
        await self._recover_runner_if_needed()
        await self._bind_runner_if_needed()

        # ── C1: File attachments ──────────────────────────────
        if files:
            if isinstance(input, str):
                content_blocks: list[dict[str, object]] = []
                if input:
                    content_blocks.append({"type": "input_text", "text": input})
            else:
                content_blocks = list(input)
            session_files = self._client.files.for_session(session_id)
            for path in files:
                uploaded = await session_files.upload(path)
                ct = mimetypes.guess_type(path)[0]
                if ct and ct.startswith("image/"):
                    content_blocks.append({"type": "input_image", "file_id": uploaded.id})
                else:
                    content_blocks.append(
                        {
                            "type": "input_file",
                            "file_id": uploaded.id,
                            "filename": pathlib.Path(path).name,
                        }
                    )
            input = content_blocks  # type: ignore[assignment]

        if isinstance(input, str):
            content: list[dict[str, object]] = [{"type": "input_text", "text": input}]
        else:
            content = list(input)
        event_payload: dict[str, object] = {
            "type": "message",
            "data": {"role": "user", "content": content},
        }
        if self._model_override is not None:
            event_payload["model_override"] = self._model_override

        # Signal that a turn is active. The _on_event callback
        # uses this to know it should handle streaming text deltas
        # and tool rendering inline rather than as history items.
        self._is_streaming = True
        self._turn_done: asyncio.Event = asyncio.Event()
        self._pending_local_user_sends += 1

        try:
            if _dbg:
                print(
                    f"[sessions-adapter] POST /events session={session_id}",
                    file=sys.stderr,
                    flush=True,
                )
            await self._client.sessions.post_event(session_id, event_payload)
            if _dbg:
                print(
                    "[sessions-adapter] POST returned; waiting for terminal event",
                    file=sys.stderr,
                    flush=True,
                )
            # Wait for the turn to complete. The stream pump sets
            # _turn_done when it sees session.status idle/failed;
            # the _on_event callback (when wired) also sets it.
            #
            # Fallback polling: httpx's ASGI transport does not
            # flush streaming body chunks eagerly, so the pump's
            # SSE subscription may not be active when the workflow
            # publishes its terminal event (no-replay pub-sub).
            # We poll the snapshot every second as a backstop.
            while not self._turn_done.is_set():
                try:
                    # Event.wait() is cancellation-safe (its finally block
                    # removes the waiter from Event._waiters), so no
                    # asyncio.shield() is needed — shield leaks orphaned
                    # Tasks on each timeout iteration.
                    await asyncio.wait_for(
                        self._turn_done.wait(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    snap = await self._client.sessions.get(session_id)
                    if snap.status in ("idle", "failed"):
                        self._turn_done.set()
            # Yield a terminal event so callers iterating send()
            # observe completion. Rendering already happened via
            # _on_event.
            from omnigent_client._events import ResponseCompleted
            from omnigent_client._types import Response as SDKResponse

            yield ResponseCompleted(
                response=SDKResponse.from_dict(
                    {
                        "id": self._current_response_id or "",
                        "status": "completed",
                        "model": self._agent_name,
                        "output": [],
                    }
                ),
            )
        finally:
            self._is_streaming = False

    async def send_skill_slash_command(
        self,
        skill_name: str,
        arguments: str,
    ) -> AsyncGenerator[object, None]:
        """
        Post a structured skill slash-command event.

        The Omnigent server persists a visible ``slash_command`` item and
        injects the skill body as a hidden ``message`` with
        ``is_meta=True``. This method deliberately does not call
        :meth:`send`, because skill commands are not user-message
        text and must not reintroduce the legacy ``load_skill``
        prompt into the transcript.

        :param skill_name: Skill name without the leading slash,
            e.g. ``"code-review"``.
        :param arguments: Raw text typed after the slash command,
            e.g. ``"review this diff"``. Empty string when none.
        :yields: SDK-shape terminal events for callers that still
            iterate the session surface.
        """
        _dbg = bool(os.environ.get("OMNIGENT_SESSIONS_ADAPTER_DEBUG"))
        session_id = await self._ensure_session()
        await self._recover_runner_if_needed()
        await self._bind_runner_if_needed()

        event_payload: dict[str, object] = {
            "type": "slash_command",
            "data": {
                "kind": "skill",
                "name": skill_name,
                "arguments": arguments,
            },
        }
        if self._model_override is not None:
            event_payload["model_override"] = self._model_override

        self._is_streaming = True
        self._turn_done: asyncio.Event = asyncio.Event()
        command_key = (skill_name, arguments)
        self._pending_local_skill_slash_commands.append(command_key)

        try:
            if _dbg:
                print(
                    f"[sessions-adapter] POST skill slash command session={session_id}",
                    file=sys.stderr,
                    flush=True,
                )
            await self._client.sessions.post_event(session_id, event_payload)
            while not self._turn_done.is_set():
                try:
                    await asyncio.wait_for(
                        self._turn_done.wait(),
                        timeout=1.0,
                    )
                except asyncio.TimeoutError:
                    snap = await self._client.sessions.get(session_id)
                    if snap.status in ("idle", "failed"):
                        self._turn_done.set()
            from omnigent_client._events import ResponseCompleted
            from omnigent_client._types import Response as SDKResponse

            yield ResponseCompleted(
                response=SDKResponse.from_dict(
                    {
                        "id": self._current_response_id or "",
                        "status": "completed",
                        "model": self._agent_name,
                        "output": [],
                    }
                ),
            )
        finally:
            with contextlib.suppress(ValueError):
                self._pending_local_skill_slash_commands.remove(command_key)
            self._is_streaming = False

    async def cancel(self):
        """
        Interrupt the running turn (if any).

        Posts an ``{"type": "interrupt"}`` event to the session,
        which bypasses the input queue and cancels the running
        task directly. Returns ``None`` rather than a
        :class:`Response` because the sessions API has no
        per-response object to return. The REPL's ``/cancel``
        command treats ``None`` as "nothing to print".

        :returns: ``None``.
        """
        if self._session_id is None:
            return
        await self._client.sessions.interrupt(self._session_id)
        return

    def _spawn_client_tool(
        self,
        session_id: str,
        call_id: str,
        name: str,
        args_str: str,
    ) -> None:
        """
        Spawn a background task to execute a client-side tool.

        Looks up the tool in ``_tool_callables``, runs it, and
        POSTs the result as a ``function_call_output`` event.

        :param session_id: Session to post the result to.
        :param call_id: The tool call's correlation id.
        :param name: Tool name, e.g. ``"search.web"``.
        :param args_str: JSON-encoded arguments string.
        """
        import inspect
        import json as _json

        callable_fn = self._tool_callables.get(name) if self._tool_callables else None  # type: ignore[union-attr]
        if callable_fn is None:
            return

        async def _run() -> None:
            try:
                args = _json.loads(args_str) if args_str else {}
            except (ValueError, TypeError):
                args = {}
            from omnigent_client._tool_handler import ToolCallInfo

            call_info = ToolCallInfo(
                name=name,
                arguments=args,
                call_id=call_id,
                agent_name="",
                response_id=self._current_response_id or "",
                iteration=0,
            )
            try:
                result = callable_fn(call_info)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:  # noqa: BLE001
                result = f"Error executing tool: {exc}"
            await self._client.sessions.post_event(
                session_id,
                {
                    "type": "function_call_output",
                    "data": {"call_id": call_id, "output": str(result)},
                },
            )

        task = asyncio.create_task(_run(), name=f"client-tool-{call_id}")
        self._pending_local_tasks[call_id] = task
        task.add_done_callback(lambda _t, _k=call_id: self._pending_local_tasks.pop(_k, None))

    async def _handle_elicitation(
        self,
        session_id: str,
        event: object,
    ) -> None:
        """
        Route an elicitation through the hook and POST the verdict.

        :param session_id: Session to post the approval to.
        :param event: The translated :class:`ElicitationRequest`.
        """
        import inspect

        from omnigent_client._tool_handler import ElicitationRequestCtx

        elicitation_id = getattr(event, "elicitation_id", "")
        hook = self._hooks.on_elicitation_request
        if hook is None:
            action = "decline"
        else:
            ctx = ElicitationRequestCtx(
                elicitation_id=elicitation_id,
                message=getattr(event, "message", ""),
                requested_schema=getattr(event, "requested_schema", {}),
                mode=getattr(event, "mode", "form"),
                phase=getattr(event, "phase", ""),
                policy_name=getattr(event, "policy_name", ""),
                content_preview=getattr(event, "content_preview", ""),
                response_id=self._current_response_id or "",
                url=getattr(event, "url", None),
            )
            try:
                result = hook(ctx)
                if inspect.isawaitable(result):
                    result = await result
                action = "accept" if result else "decline"
            except Exception:  # noqa: BLE001
                action = "decline"
        # Build the resolve payload. For accept with a requestedSchema,
        # populate ``content`` from the schema so the MCP server receives
        # the form data it expects. Simple schemas (boolean, enum) are
        # auto-filled; complex schemas that require free-form user input
        # fall back to decline with a message to use the web UI.
        resolve_payload: dict[str, object] = {"action": action}
        if action == "accept":
            schema = getattr(event, "requested_schema", None) or {}
            content = _build_elicitation_content_from_schema(schema)
            if content is not None:
                resolve_payload["content"] = content
            elif schema.get("properties"):
                # Never leave the elicitation unresolved: any failure
                # while prompting (or a user abort) declines, so the
                # agent turn doesn't hang waiting on a verdict that the
                # background task would otherwise never POST.
                try:
                    prompted = await self._prompt_schema_fields(schema)
                except Exception:  # noqa: BLE001
                    prompted = None
                if prompted is not None:
                    resolve_payload["content"] = prompted
                else:
                    resolve_payload["action"] = "decline"

        try:
            # URL-based elicitation: deliver the verdict to the
            # elicitation's dedicated resolve URL rather than as an
            # in-band ``approval`` session event. Same server-side
            # effect (both converge on ``_resolve_elicitation``).
            await self._client.sessions.resolve_elicitation(
                session_id,
                elicitation_id,
                resolve_payload,
            )
        except OmnigentError as exc:
            if exc.code == "not_found":
                # Elicitation already resolved by another client (e.g. web
                # UI approved while the terminal prompt was still open).
                # The harness already received the verdict — treat as no-op.
                return
            raise

    async def _prompt_schema_fields(
        self,
        schema: dict[str, object],
    ) -> dict[str, str | int | float | bool | list[str] | None] | None:
        """Prompt the user for each schema property via the main input loop.

        Returns the filled content dict, or ``None`` if the interactive
        state is unavailable (e.g. no ``_field_input_state`` wired up).
        """
        from rich.text import Text

        fis = self._field_input_state
        host = self._host
        fmt = self._fmt
        if fis is None or host is None or fmt is None:
            return None

        properties = schema.get("properties")
        if not properties or not isinstance(properties, dict):
            return None

        required = set(schema.get("required", []))  # type: ignore[arg-type]
        content: dict[str, str | int | float | bool | list[str] | None] = {}

        for key, prop in properties.items():
            if not isinstance(prop, dict):
                return None

            prop_type = prop.get("type", "string")
            description = prop.get("description", "")
            default = prop.get("default")
            enum_vals = prop.get("enum")
            one_of = prop.get("oneOf")

            # Build the prompt label.
            label_parts: list[str] = [f"   {key}"]
            if description:
                label_parts.append(f" — {description}")
            hint_parts: list[str] = []
            if one_of and isinstance(one_of, list):
                opts = [
                    str(o.get("const", "")) for o in one_of if isinstance(o, dict) and "const" in o
                ]
                if opts:
                    hint_parts.append("/".join(opts))
            elif enum_vals and isinstance(enum_vals, list):
                hint_parts.append("/".join(str(v) for v in enum_vals))
            elif prop_type == "boolean":
                hint_parts.append("true/false")
            else:
                hint_parts.append(str(prop_type))
            if default is not None:
                hint_parts.append(f"default: {default}")
            if key not in required:
                hint_parts.append("optional")
            hint = ", ".join(hint_parts)
            label_parts.append(f" [{hint}]")

            # Render as plain styled text, NOT markup: the label embeds
            # server-controlled schema text (description, enum values,
            # key) that ``Text.from_markup`` would parse as Rich tags —
            # a stray ``[`` silently mangles the line and an unbalanced
            # one raises ``MarkupError``, which would crash this
            # background task and hang the elicitation.
            host.output(Text("   " + "".join(label_parts), style=fmt.accent))

            # Re-prompt the same field on bad input rather than discarding
            # everything: a single typo on field N shouldn't decline the
            # whole form. The user aborts the turn with Esc (→ ``aborted``).
            while True:
                raw = await fis.begin(key)
                if fis.aborted:
                    return None

                stripped = raw.strip()
                if not stripped:
                    if default is not None:
                        content[key] = default
                    elif key in required:
                        host.output(
                            Text(
                                f"     ↳ {key} is required — enter a value (Esc cancels)",
                                style=fmt.warning,
                            ),
                        )
                        continue
                    # Optional with no default: leave unset.
                    break

                # Parse according to type.
                val: str | int | float | bool = stripped
                if prop_type == "boolean":
                    val = stripped.lower() in ("true", "1", "yes", "y")
                elif prop_type == "integer":
                    try:
                        val = int(stripped)
                    except ValueError:
                        host.output(
                            Text(
                                f"     ↳ expected a whole number, got {stripped!r}",
                                style=fmt.warning,
                            ),
                        )
                        continue
                elif prop_type == "number":
                    try:
                        val = float(stripped)
                    except ValueError:
                        host.output(
                            Text(
                                f"     ↳ expected a number, got {stripped!r}",
                                style=fmt.warning,
                            ),
                        )
                        continue

                # Validate enum constraints.
                if one_of and isinstance(one_of, list):
                    valid = [
                        o.get("const") for o in one_of if isinstance(o, dict) and "const" in o
                    ]
                    if val not in valid:
                        host.output(
                            Text(
                                f"     ↳ choose one of: {', '.join(str(v) for v in valid)}",
                                style=fmt.warning,
                            ),
                        )
                        continue
                elif enum_vals and isinstance(enum_vals, list):
                    if val not in enum_vals:
                        host.output(
                            Text(
                                f"     ↳ choose one of: {', '.join(str(v) for v in enum_vals)}",
                                style=fmt.warning,
                            ),
                        )
                        continue

                content[key] = val
                break

        return content

    async def _unbind_runner_soft(self, session_id: str) -> None:
        """
        Unbind the runner from ``session_id``; soft-fail on old servers.

        Forward-compat shim: servers without the empty-string clear
        sentinel reject ``{"runner_id": ""}`` with
        ``invalid_input: runner_id must not be empty``. We log that
        case at debug and continue, so ``/clear`` and ``/switch`` keep
        working against unpatched deployments — the 1:1 session↔runner
        invariant just isn't enforced until the server is redeployed.
        Other errors propagate.
        """
        _dbg = bool(os.environ.get("OMNIGENT_SESSIONS_ADAPTER_DEBUG"))
        try:
            await self._client.sessions.unbind_runner(session_id)
        except OmnigentError as exc:
            if exc.code == "invalid_input" and "runner_id must not be empty" in str(exc):
                if _dbg:
                    print(
                        f"[sessions-adapter] unbind_runner not supported by server "
                        f"(session {session_id!r} keeps stale runner binding until "
                        f"server is redeployed): {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                return
            raise

    def reset(self) -> None:
        """
        Legacy hook — no-op in sessions mode.

        ``_attach_to_conversation`` calls ``reset()`` *after* the
        runner bind + SSE pump are set up, so doing teardown here
        would silently break ``--resume`` / ``/switch``. ``/clear``
        and ``/new`` use :meth:`start_new_conversation` instead.
        """
        return

    async def start_new_conversation(self) -> None:
        """
        Tear down the current session so the next ``send()`` POSTs a fresh one.

        Used by ``/clear`` and ``/new``. Unbinds the runner from the
        old session (1:1 session↔runner invariant), cancels the SSE
        pump, and clears local state. Session creation stays lazy —
        the next :meth:`send` takes :meth:`_ensure_session`'s create
        branch. Idempotent when no session is established. The unbind
        is soft-failed on old servers (see :meth:`_unbind_runner_soft`).
        """
        # A fresh session is owned, not observed — clear any read-only-view
        # suppression / interactive-child co-drive from a sub-agent dive so the
        # new session can bind.
        self._readonly_view = False
        self._interactive_child = False
        old_session_id = self._session_id
        if old_session_id is not None:
            await self._unbind_runner_soft(old_session_id)
        if self._stream_task is not None:
            self._stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stream_task
            self._stream_task = None
        for task in self._pending_local_tasks.values():
            task.cancel()
        self._pending_local_tasks = {}
        self._session_id = None
        self._current_response_id = None
        self._is_streaming = False
        self._pending_local_user_sends = 0
        self._pending_local_skill_slash_commands = []
        self._bound_runner_id = None

    def resume_from_response(self, response_id: str) -> None:  # noqa: ARG002 — legacy hook accepted but ignored in sessions mode
        """
        Legacy hook — no-op in sessions mode.

        Used by the REPL to seed ``previous_response_id`` after
        discovering an external in-flight response. The sessions
        API drives off session_id only, so there is nothing to
        seed.

        :param response_id: Ignored.
        """
        return

    def switch_session(self, new_session_id: str) -> None:
        """
        Switch the adapter to a different session in-place.

        Cancels the existing SSE stream pump (if running) and
        updates ``_session_id`` so the next :meth:`_ensure_session`
        call reconnects to the new session. Used by ``/fork`` to
        continue in the forked conversation without repainting the
        transcript.

        :param new_session_id: The session id to switch to, e.g.
            ``"conv_fork_abc123"``.
        """
        if self._stream_task is not None:
            self._stream_task.cancel()
            self._stream_task = None
        self._session_id = new_session_id
        self._bound_runner_id = None  # Force re-bind on next send


@dataclass(frozen=True)
class _OutputItemRenderPlan:
    """
    How the streaming renderer should handle one ``OutputItemDone`` item.

    A single turn can interleave several assistant text blocks with
    tool calls. The streaming executor emits the prose as ``TextDelta``
    events and the tool calls as inline ``function_call`` output items,
    and it emits the assistant ``message`` output item only once, after
    all deltas — never between consecutive text blocks. So a tool call
    is the only signal that one text block ended and another is about to
    begin, and the renderer must commit the in-flight prose at that
    boundary or the formatter's paragraph buffer accumulates across
    blocks and re-renders the whole turn's prose on every later delta.

    :param flush_inflight_text: Commit any in-flight streamed assistant
        prose (via ``format_message_done``) before handling this item.
        ``True`` at every content-block boundary that interrupts
        streamed text — a tool call / output, or the assistant
        ``message`` item itself.
    :param render_item: Render ``item`` as a history entry. ``True`` for
        tool calls / outputs / slash commands and for a non-streamed
        assistant message; ``False`` for an assistant message whose
        prose already streamed as deltas (the deltas rendered it, so the
        full item would duplicate it).
    """

    flush_inflight_text: bool
    render_item: bool


# Item types rendered as inline history entries during a streaming turn.
# A ``function_call`` / ``function_call_output`` arriving mid-stream
# marks a content-block boundary that interrupts assistant prose.
_RENDERABLE_OUTPUT_ITEM_TYPES = ("function_call", "function_call_output", "slash_command")


def _plan_output_item_render(
    item_type: str | None,
    role: str | None,
    saw_text_deltas: bool,
) -> _OutputItemRenderPlan:
    """
    Decide how to handle one streamed ``OutputItemDone`` item.

    :param item_type: The item's ``type`` field, e.g. ``"function_call"``,
        ``"function_call_output"``, ``"message"``, or ``"slash_command"``.
    :param role: The item's ``role`` field for ``message`` items, e.g.
        ``"assistant"``; ``None`` for item types that have no role.
    :param saw_text_deltas: Whether any ``TextDelta`` has streamed since
        the last commit (i.e. there is in-flight assistant prose).
    :returns: The :class:`_OutputItemRenderPlan` for this item.
    """
    if item_type == "message" and role == "assistant":
        # Prose already streamed as deltas: commit the tail at the
        # boundary and skip re-rendering the full item. When no deltas
        # streamed (e.g. a non-streaming harness), render the item.
        return _OutputItemRenderPlan(
            flush_inflight_text=saw_text_deltas,
            render_item=not saw_text_deltas,
        )
    if item_type in _RENDERABLE_OUTPUT_ITEM_TYPES:
        # A concrete item interrupts streamed prose; commit the prose
        # first (no-op when nothing is in flight) so the next text block
        # starts from an empty buffer.
        return _OutputItemRenderPlan(
            flush_inflight_text=saw_text_deltas,
            render_item=True,
        )
    return _OutputItemRenderPlan(flush_inflight_text=False, render_item=False)


@dataclass
class _TurnProseTracker:
    """
    Streamed assistant prose bookkeeping for duplicate-item detection.

    The relay persists each streamed text segment at a tool-call
    boundary and publishes the persisted item as
    ``response.output_item.done`` so clients learn its store-assigned id
    (``_flush_relay_text`` in ``omnigent/server/routes/sessions.py``).
    By the time that event reaches the REPL, the tool-call item that
    triggered the flush has already committed the in-flight prose — the
    delta-based skip (``saw_text_deltas``) sees nothing in flight and
    would re-render the whole segment as a fresh "◆ agent + text" block.

    This tracker remembers the turn's streamed prose per committed
    segment so an assistant ``message`` item can be matched back (by
    byte-equal text) to prose the user already watched stream, and
    suppressed. Matching consumes the entry — multiset semantics, so a
    turn that legitimately produces two identical segments still gets
    its second copy matched by the second published item, and a
    genuinely non-streamed assistant message (no matching entry) still
    renders.

    :param segment_parts: Delta strings of the current (uncommitted)
        text segment in arrival order, e.g. ``["Got it — ", "done."]``.
    :param committed_texts: Joined text of each segment committed this
        turn, e.g. ``["Got it — done."]``.
    """

    segment_parts: list[str] = field(default_factory=list)
    committed_texts: list[str] = field(default_factory=list)

    def on_delta(self, delta: str) -> None:
        """
        Accumulate one streamed text delta into the current segment.

        :param delta: The ``response.output_text.delta`` text,
            e.g. ``"Got it — "``.
        """
        self.segment_parts.append(delta)

    def commit_segment(self) -> None:
        """
        Move the current segment into the committed list.

        Called when in-flight prose is committed at a content-block
        boundary (tool call, assistant ``message`` item). No-op when no
        deltas streamed since the last commit.
        """
        if self.segment_parts:
            self.committed_texts.append("".join(self.segment_parts))
            self.segment_parts.clear()

    def reset_turn(self) -> None:
        """
        Drop all bookkeeping at a turn boundary.

        A new turn's prose must not be matched against (or suppressed
        by) the previous turn's segments.
        """
        self.segment_parts.clear()
        self.committed_texts.clear()

    def consume_match(self, item: dict[str, object]) -> bool:
        """
        Match an assistant ``message`` item against committed prose.

        Joins the item's ``output_text`` content blocks and looks for a
        byte-equal committed segment. A match means the item is the
        relay's persisted copy of prose that already streamed, so
        rendering it would duplicate the segment. The matched entry is
        consumed.

        :param item: The ``output_item.done`` item dict, e.g.
            ``{"type": "message", "role": "assistant", "content":
            [{"type": "output_text", "text": "Got it — done."}]}``.
        :returns: ``True`` when the item's text matched (and consumed) a
            committed streamed segment; ``False`` when the item carries
            no output text or nothing matched.
        """
        content = item.get("content")
        if not isinstance(content, list):
            return False
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "output_text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        if not parts:
            return False
        joined = "".join(parts)
        try:
            self.committed_texts.remove(joined)
        except ValueError:
            return False
        return True


def _render_failed_status_error(
    fmt: RichBlockFormatter,
    host: TerminalHost,
    event: SessionStatusEvent,
) -> list[FormattedItem]:
    """
    Render the error line for a terminal ``session.status: failed`` event.

    A SETUP-phase failure (spec resolution, spawn-env build) ends the
    turn before the LLM stream starts, so no ``response.failed`` /
    ``ErrorEvent`` is ever emitted — the only signal is the terminal
    ``failed`` status. Without rendering its carried error message the
    REPL ends the turn silently: the working spinner vanishes with no
    output. This formats the message as an :class:`ErrorBlock` (the
    same red error styling used for ``response.error`` /
    ``response.failed``) and writes it to the host. Falls back to a
    generic ``"turn failed"`` message when the event carries no error
    detail so a bare ``failed`` status never crashes the renderer.

    :param fmt: The active block formatter, e.g. a
        :class:`TimedFormatter`.
    :param host: The terminal host the error line is written to.
    :param event: The terminal ``session.status`` event with
        ``status == "failed"``, e.g.
        ``SessionStatusEvent(type="session.status",
        conversation_id="conv_abc", status="failed",
        error=ErrorDetail(code="runner_error",
        message="turn setup failed: ..."))``.
    :returns: The list of formatted items written to the host (for
        debug-tape recording by the caller).
    """
    from omnigent_client import ErrorBlock

    err_message = (
        event.error.message if event.error is not None and event.error.message else "turn failed"
    )
    err_items = list(
        fmt.format_error(
            ErrorBlock(
                message=err_message,
                source="runner",
                ctx=BlockContext(agent=None, depth=0, turn=0),
            ),
        )
    )
    for err_item in err_items:
        host.output(err_item)
    return err_items


async def run_repl(
    client: OmnigentClient,
    agent_name: str,
    tool_handler: ToolHandler | None,
    *,
    initial_message: str | None = None,
    resume_conversation_id: str | None = None,
    log_dir: pathlib.Path | None = None,
    debug_events: bool = False,
    server_log_path: pathlib.Path | None = None,
    runner_log_path: pathlib.Path | None = None,
    session_bundle: bytes | None = None,
    session_bundle_filename: str = "agent.tar.gz",
    runner_id: str | None = None,
    runner_recover: Callable[[], str] | None = None,
    resume_parts: list[str] | None = None,
    ephemeral: bool = False,
    skills: list[SkillSpec] | None = None,
    server_url: str | None = None,
    on_session_start: Callable[[str], None] | None = None,
    harness: str | None = None,
    agent_description: str | None = None,
    used_families: list[str] | None = None,
    attach_only: bool = False,
) -> str | None:
    """The entire REPL — using the framework.

    :param client: Connected OmnigentClient.
    :param agent_name: Agent name (used for API calls).
    :param tool_handler: Optional client-side tool handler.
    :param initial_message: If set, auto-send this message on startup
        (e.g. a greeting prompt for onboarding).
    :param resume_conversation_id: When set, the REPL opens
        attached to this existing session instead of creating a
        fresh session. Resolved upstream from ``--continue`` /
        ``--resume <id>`` (see designs/RUN_OMNIGENT_SESSION_RESUMPTION.md).
        ``None`` opens a fresh session through ``POST /v1/sessions``.
    :param log_dir: When set, write a JSON dump of the active
        conversation to ``{log_dir}/{timestamp}-{conv_short}.json``
        on REPL exit. ``None`` (default) skips the dump. Maps to
        the CLI ``--log`` flag (and the ``~/.omnigent/logs/``
        default location); see ``omnigent.repl._session_log`` for
        the schema. The dump runs in the SAME ``async with
        OmnigentClient(...)`` scope as the REPL itself, so the
        client is still connected when we fetch the conversation +
        items. Failures are logged to stderr but do NOT propagate —
        a write error at REPL exit shouldn't take the user's
        terminal down.
    :param debug_events: When ``True``, enable the SSE-to-UI debug
        pipeline: a ``Ctrl+E`` event tape overlay, JSONL event
        logging to ``~/.omnigent/debug/``, and pipeline stage
        counters in the toolbar. Maps to ``--debug-events`` on the
        CLI.
    :param session_bundle: Gzipped agent bundle bytes used to
        create a fresh sessions-API session. Required when
        ``resume_conversation_id`` is ``None``.
    :param session_bundle_filename: Filename for the multipart
        upload, e.g. ``"agent.tar.gz"``.
    :param runner_id: Registered runner id to bind before the first
        turn, e.g. ``"runner_0123456789abcdef"``.
    :param runner_recover: Optional callback that returns the current
        runner id, restarting the local runner if it has exited.
    :param resume_parts: Pre-built argument list prefix for the
        resume hint, e.g. ``["omnigent", "run", "agent.yaml",
        "--server", "https://example.com"]``.  Built from Click's
        parsed context at CLI dispatch time so one-shot flags
        (``-p``, ``--fork``, ``-c``) are already excluded.
        ``None`` omits the resume hint on exit.
    :param ephemeral: When ``True``, suppress the resume hint on
        exit — the session data lives in a tmpdir that won't
        survive process exit, so the hint would be misleading.
    :param skills: Parsed skill list from the agent spec, e.g.
        ``[SkillSpec(name="code-review", ...)]``. Each skill is
        registered as a ``/<name>`` slash command at REPL startup.
        ``None`` (default) means no skill commands are registered.
    :param server_url: Base URL of the Omnigent server the REPL is
        connected to. Surfaced in the welcome banner when it
        points at a non-loopback host so the user can see which
        workspace they're talking to. ``None`` omits it.
    :param on_session_start: Optional callback invoked once when
        the top-level session id is known, e.g.
        ``lambda session_id: open_url(session_id)``.
    :param harness: The launch harness derived from the local spec,
        e.g. ``"claude-sdk"`` — used to name the model + credential in
        the startup header and the ``/model`` readout. ``None`` for a
        remote-URL target (no local spec).
    :param agent_description: The agent spec's ``description``, surfaced
        as the one-line summary row in the startup header, e.g.
        polly's ``"Multi-agent coding orchestrator. …"``. ``None``
        omits the summary row.
    :param used_families: Provider families the agent's harnesses (incl.
        sub-agents) consume, e.g. ``["anthropic", "openai"]`` for
        polly. A multi-family agent gets a per-family creds line under
        the startup header. ``None`` / a single family omits that line.
    :returns: The conversation id from the last active conversation,
        or ``None`` if the user exited before any conversation was
        created (e.g. immediate Ctrl-D).
    """
    # Register skill-based slash commands before the input loop
    # so they appear in autocomplete and /help from the start.
    # Track the registered names so we can clean them up on exit
    # and avoid leaking into subsequent run_repl calls.
    _registered_skill_cmds: list[str] = []
    if skills:
        _registered_skill_cmds = register_skill_commands(skills)

    ui_name = _humanize_agent_name(agent_name)
    theme = _load_startup_theme()
    fmt = TimedFormatter(show_agent_labels=True, theme=theme)
    # Pass ``WELCOME_HINTS`` for the bottom toolbar. Without
    # this the bar only showed "esc cancel · ctrl+c exit",
    # leaving ``/help`` and the Ctrl+O overlay invisible to
    # users.
    # ``window_title`` mirrors the legacy CLI's terminal-title
    # behavior (omnigent/inner/cli.py:2979 + :2984): when a user
    # has multiple agent sessions open across tabs, the tab bar
    # should show which agent is which. Without this, every tab
    # reads "Terminal" / "$SHELL" and there's no way to tell them
    # apart short of switching to each one.
    # When debug-events is on, add the Ctrl+E hint to the welcome
    # panel and toolbar so the user knows the overlay exists.
    hints = list(WELCOME_HINTS)
    if debug_events:
        hints.insert(1, "Ctrl+E events")
    host = TerminalHost(
        model_name=ui_name,
        toolbar_hints=hints,
        window_title=ui_name,
        # Live popup for registered slash commands and @-mention
        # file completion.  ``merge_completers`` yields results from
        # both completers — only one fires per keystroke because
        # their trigger characters (``/`` vs ``@``) don't overlap.
        completer=merge_completers([_SlashCommandCompleter(), FileMentionCompleter()]),
        theme=theme,
    )

    # ── Debug event pipeline (--debug-events) ──────────────────
    # Lazily initialized only when the flag is set so zero overhead
    # in the normal path. The tape and counters are closed over by
    # the instrumented renderers below.
    from omnigent.repl._event_tape import EventTape, PipelineCounters, TapeEntry

    _event_tape: EventTape | None = None
    _event_log_fh: TextIO | None = None  # file handle for JSONL log
    _event_log_path: pathlib.Path | None = None  # JSONL log path, shown in Ctrl+O
    _pipeline_counters: PipelineCounters | None = None
    if debug_events:
        _pipeline_counters = PipelineCounters()
        _event_tape = EventTape(counters=_pipeline_counters)
        host.pipeline_counters = _pipeline_counters  # type: ignore[attr-defined]

    # Ctrl+T: toggle tool-output panels in the formatter.
    def _toggle_tool_output() -> None:
        fmt.show_tool_output = not fmt.show_tool_output
        # Update the toolbar hint to reflect the new state.
        new_label = "Ctrl+T hide tools" if fmt.show_tool_output else "Ctrl+T show tools"
        for i, h in enumerate(host._toolbar_hints):
            if h.startswith("Ctrl+T "):
                host._toolbar_hints[i] = new_label
                break

    host.on_toggle_tool_output = _toggle_tool_output

    # Wire the policy-ASK seam into the session so any policy
    # in the agent's spec that returns ASK surfaces an inline
    # y/n prompt here. The hook lives on the session so every
    # turn in this REPL benefits — no per-call re-registration.
    # Shared state couples the hook (which awaits a future) to
    # the main input loop (which resolves it); reusing the
    # normal prompt_toolkit input path avoids the stdin /
    # patch_stdout fight that a direct input() call produced.
    approval_state = _ApprovalState()
    field_input_state = _FieldInputState()
    hooks = StreamHooks(
        on_elicitation_request=_make_elicitation_prompt(
            host, fmt, approval_state, server_url=server_url
        ),
    )
    # Build the tool_callables map from the legacy ToolHandler
    # when present so client-side tool tunneling still works.
    # The ToolHandler's ``execute`` callable matches the
    # SessionsChat ToolCallable contract closely enough; the
    # name → callable indirection is what SessionsChat expects.
    tool_callables: dict[str, object] | None = None
    if tool_handler is not None:
        tool_callables = {
            schema["name"]: tool_handler.execute  # type: ignore[index]
            for schema in tool_handler.schemas
            if isinstance(schema, dict) and "name" in schema
        }
    # ``Session`` typing here is intentional: the adapter
    # is duck-compatible with the legacy surface the REPL
    # uses (send/cancel/current_response_id/model/
    # is_streaming/reset/resume_from_response/
    # set_reasoning_effort/reasoning_effort). mypy is
    # appeased via the runtime cast; the static type
    # mismatch surfaces in tests, not at runtime.
    session = _SessionsChatReplAdapter(  # type: ignore[assignment]
        client=client,
        agent_name=agent_name,
        tool_callables=tool_callables,
        hooks=hooks,
        session_id=resume_conversation_id,
        session_bundle=session_bundle,
        session_bundle_filename=session_bundle_filename,
        runner_id=runner_id,
        runner_recover=runner_recover,
        on_session_start=on_session_start,
        harness=harness,
        attach_only=attach_only,
        field_input_state=field_input_state,
        host=host,
        fmt=fmt,
    )
    # Make per-invocation log paths visible to slash commands such as
    # /logs without broadening the slash-command dispatch signature.
    session._server_log_path = server_log_path  # type: ignore[attr-defined]
    session._runner_log_path = runner_log_path  # type: ignore[attr-defined]

    # True once any TextDelta has been rendered for the current
    # turn. Used to suppress the duplicate full-text that arrives
    # in output_item.done (type=message, role=assistant) after the
    # same prose already streamed as deltas.
    _saw_text_deltas = False
    # Streamed-prose bookkeeping for the relay's persisted-segment
    # publishes: matches an assistant ``message`` output item back to
    # prose that already streamed this turn so it isn't re-rendered as
    # a duplicate "◆ agent + text" block. See :class:`_TurnProseTracker`.
    _prose_tracker = _TurnProseTracker()
    # Tracks whether the most-recent ResponseCompleted event carried
    # provider-reported usage. Reset to False at each "running" status
    # (new turn begins) so the idle-event local-estimate fallback fires
    # on every turn for harnesses that never report usage (e.g. codex).
    _context_ring_state: list[bool] = [False]  # [last_completed_had_usage]

    def _flush_inflight_assistant_text() -> list[FormattedItem]:
        """
        Commit in-flight streamed assistant text at a content-block boundary.

        A single turn can contain several assistant text blocks
        interleaved with tool calls. The streaming executor emits the
        prose as ``TextDelta`` events and the tool calls as inline
        ``function_call`` output items, but it emits the
        ``message`` (role=assistant) output item only once, after all
        deltas — never between consecutive text blocks. The formatter's
        paragraph buffer is reset by ``format_message_done`` only at
        that boundary, so when a tool call interrupts streamed prose the
        buffer keeps accumulating across blocks and the live region
        re-renders the whole turn's prose — prefixed with a fresh
        ``◆`` — on every later delta and tool round (the "growing
        preamble" duplication).

        Flushing here commits the in-flight text whenever a concrete
        non-text item is about to render, so the next block starts from
        an empty buffer. Idempotent: a no-op when no deltas have
        streamed since the last commit. Resets ``_saw_text_deltas`` so
        the next block's first delta re-arms it.

        :returns: The ``StreamReplace`` items emitted by
            ``format_message_done`` (empty when nothing was in flight or
            the text ended on a paragraph boundary). The caller forwards
            these to the event tape for audit accounting.
        """
        nonlocal _saw_text_deltas
        if not _saw_text_deltas:
            return []
        flush_items = list(fmt.format_message_done())
        for it in flush_items:
            host.output(it)
        # Remember the committed segment's text so the relay's
        # persisted-item publish for it (an assistant ``message``
        # output_item.done arriving after a tool call reset
        # ``_saw_text_deltas``) is recognized as already rendered.
        _prose_tracker.commit_segment()
        _saw_text_deltas = False
        return flush_items

    def _spawn_metadata_refresh() -> None:
        """
        Fire a background re-sync of session metadata from a snapshot.

        Shared by the two triggers that can observe an in-place agent
        switch: the ``session.agent_changed`` stream event (live, while
        attached) and the turn-start catch-up in the ``running`` status
        branch. Both funnel into :func:`_refresh_session_metadata` so
        adapter state is always derived from a snapshot — never applied
        piecemeal from event payloads.

        :returns: None.
        """
        _refresh_task = asyncio.create_task(_refresh_session_metadata(session, client, host, fmt))
        _background_event_tasks.add(_refresh_task)
        _refresh_task.add_done_callback(_background_event_tasks.discard)

    def _render_session_event(event: object) -> None:
        """Push-based renderer for all session stream events.

        Called by the pump for every event, both during
        ``send()`` (user-initiated turn) and between sends
        (autonomous turns). Handles:

        * ``TextDelta`` -> streamed text via formatter
        * ``OutputItemDoneEvent`` -> tool calls/results via
          ``_render_history_item``; client-side tool dispatch
          for ``action_required``
        * ``ResponseCreated`` -> response header + track id
        * Terminal events -> signal ``_turn_done``
        * ``ElicitationRequest`` -> approval hook
        * ``ClientTaskCancelEvent`` -> cancel local tool task
        """
        nonlocal _saw_text_deltas

        from omnigent_client._events import (
            ElicitationRequest as _Elicit,
        )
        from omnigent_client._events import (
            ResponseCreated as _Created,
        )
        from omnigent_client._events import (
            TextDelta as _TD,
        )

        from omnigent.server.schemas import (
            ClientTaskCancelEvent as _Cancel,
        )
        from omnigent.server.schemas import (
            OutputItemDoneEvent as _OIDE,
        )
        from omnigent.server.schemas import (
            SessionAgentChangedEvent as _AgentChangedEv,
        )
        from omnigent.server.schemas import (
            SessionInputConsumedEvent as _SICEv,
        )
        from omnigent.server.schemas import (
            SessionStatusEvent as _StatusEv,
        )

        tape_entry = None
        if _event_tape is not None:
            tape_entry = _event_tape.record_raw(event, path="sessions")

        if isinstance(event, _StatusEv):
            if tape_entry is not None:
                _event_tape.update_translation(tape_entry, event)  # type: ignore[union-attr]
            if event.status == "running":
                from omnigent_client import BlockContext, ResponseStartBlock

                _saw_text_deltas = False
                # New turn: drop the prior turn's streamed-segment
                # bookkeeping so its prose can't suppress a later,
                # legitimately identical assistant message.
                _prose_tracker.reset_turn()
                _context_ring_state[0] = False  # reset: new turn, provider usage unknown yet
                host.start_timer()
                # Local name distinct from run_repl's `agent_name` param:
                # assigning to `agent_name` here would shadow it for the
                # whole handler, leaving it unbound in other branches.
                current_agent = session._agent_name  # type: ignore[union-attr]
                items_out = list(
                    fmt.format_response_start(
                        ResponseStartBlock(
                            model=current_agent,
                            response_id="",
                            ctx=BlockContext(agent=current_agent, depth=0, turn=0),
                        ),
                    )
                )
                if tape_entry is not None:
                    _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
                for item in items_out:
                    host.output(item)
                if tape_entry is not None and items_out:
                    _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
                # The bound agent (and its llm_model / harness /
                # context_window) can change between turns via an
                # in-place agent switch from another client. The
                # session.agent_changed branch below catches that live,
                # but the event is transient (no replay) — one landing in
                # a stream-pump reconnect gap or before this REPL
                # attached is lost — so also re-sync at each turn start.
                _spawn_metadata_refresh()
            elif event.status in ("idle", "waiting", "failed"):
                from omnigent_client import TextDone

                # A SETUP-phase failure (spec resolution, spawn-env
                # build) ends the turn before the LLM stream starts, so
                # no response.failed / ErrorEvent ever arrives — the only
                # signal is this terminal ``failed`` status. Render its
                # error message as an error line; without this the turn
                # ends silently and the user sees the spinner vanish with
                # no output. The helper falls back to a generic message
                # when the event carries no error detail.
                if event.status == "failed":
                    err_items = _render_failed_status_error(fmt, host, event)
                    if tape_entry is not None and err_items:
                        _event_tape.mark_rendered(tape_entry, len(err_items))  # type: ignore[union-attr]

                items_out = list(
                    fmt.format_text_done(TextDone(full_text="", has_code_blocks=False))
                )
                if tape_entry is not None:
                    _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
                for item in items_out:
                    host.output(item)
                if tape_entry is not None and items_out:
                    _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
                host.stop_timer()
                turn_done = getattr(session, "_turn_done", None)
                if turn_done is not None:
                    turn_done.set()
                # Fall back to a local token-count estimate only when the
                # provider didn't report usage for this turn.  Prefer the
                # provider-reported value (set by ResponseCompleted via
                # host.update_context_usage) over the local estimate —
                # the local estimate counts conversation history, not the
                # real input window fill.
                _cw = getattr(session, "context_window", None)
                if _cw and not _context_ring_state[0]:
                    _ring_task = asyncio.create_task(
                        _update_context_ring_estimate(session, client, host, _cw)
                    )
                    _background_event_tasks.add(_ring_task)
                    _ring_task.add_done_callback(_background_event_tasks.discard)
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        if isinstance(event, _AgentChangedEv):
            if tape_entry is not None:
                _event_tape.update_translation(tape_entry, event)  # type: ignore[union-attr]
                _maybe_log_tape_entry(tape_entry)
            # Another client switched the session's agent in place. The
            # event is a trigger, not a data source — it carries no
            # llm_model / harness / context_window, and state must come
            # from one place — so re-derive from a fresh snapshot (the
            # refresh renders the "Agent switched" notice).
            _spawn_metadata_refresh()
            return

        if isinstance(event, _SICEv):
            if tape_entry is not None:
                _event_tape.update_translation(tape_entry, event)  # type: ignore[union-attr]
            if event.data.data.get("is_meta") is True:
                if tape_entry is not None:
                    _maybe_log_tape_entry(tape_entry)
                return
            if event.data.type == "message" and event.data.data.get("role") == "user":
                if session._pending_local_user_sends > 0:  # type: ignore[union-attr]
                    session._pending_local_user_sends -= 1  # type: ignore[union-attr]
                else:
                    text = _extract_message_text(event.data.data)
                    items_out = [fmt.user_message(text)]
                    if tape_entry is not None:
                        _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
                    for item in items_out:
                        host.output(item)
                    if tape_entry is not None:
                        _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        # When an elicitation is resolved externally (via the standalone
        # approval page), the server publishes ``elicitation_resolved``.
        # Wake the parked ``_ApprovalState`` future so the REPL unblocks.
        # The ``elicitation_resolved`` event doesn't carry the verdict
        # (accept/decline), but ``_handle_elicitation`` will POST a
        # redundant resolve that the server silently ignores (already
        # resolved). We approve here so the REPL unblocks; the actual
        # outcome is whatever the user chose on the page.
        from omnigent.server.schemas import ElicitationResolvedEvent

        if isinstance(event, ElicitationResolvedEvent):
            if approval_state.pending:
                approval_state.resolve_verdict(_ApprovalVerdict.APPROVE_ONCE)
                host.output(
                    Text.from_markup(
                        f"   [{fmt.muted}]› resolved via approval page[/{fmt.muted}]",
                    ),
                )
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        # Live sub-agent tree updates ride the parent stream as
        # ``session.created`` / ``session.child_session.updated``. Apply them
        # to the host registry (state badge + ↓ menu) before the generic
        # translation below, which has no branch for them and would drop them.
        if _apply_child_session_event(
            event,
            active_conversation_id=session.session_id,
            host=host,
        ):
            if tape_entry is not None:
                _event_tape.update_translation(tape_entry, event)  # type: ignore[union-attr]
                _maybe_log_tape_entry(tape_entry)
            return

        sdk_ev = _server_event_to_sdk_event(event)
        if tape_entry is not None:
            _event_tape.update_translation(tape_entry, sdk_ev)  # type: ignore[union-attr]
        if sdk_ev is None:
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        if isinstance(sdk_ev, _TD):
            from omnigent_client import TextChunk

            _saw_text_deltas = True
            _prose_tracker.on_delta(sdk_ev.delta)
            items_out = list(fmt.format_text_chunk(TextChunk(text=sdk_ev.delta)))
            if tape_entry is not None:
                _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
            for item in items_out:
                host.output(item)
            if tape_entry is not None and items_out:
                _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        from omnigent_client._events import (
            CompactionCompleted as _CC,
        )
        from omnigent_client._events import (
            CompactionInProgress as _CIP,
        )
        from omnigent_client._events import (
            ReasoningDelta as _RD,
        )
        from omnigent_client._events import (
            ReasoningStarted as _RS,
        )
        from omnigent_client._events import (
            ReasoningSummaryDelta as _RSD,
        )

        if isinstance(sdk_ev, _CIP):
            items_out = [
                Text.from_markup(f"  [{fmt.muted}]Compacting conversation context…[/{fmt.muted}]")
            ]
            if tape_entry is not None:
                _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
            for item in items_out:
                host.output(item)
            if tape_entry is not None and items_out:
                _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        if isinstance(sdk_ev, _CC):
            items_out = [Text.from_markup(f"  [{fmt.muted}]Compaction complete.[/{fmt.muted}]")]
            if tape_entry is not None:
                _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
            for item in items_out:
                host.output(item)
            if tape_entry is not None and items_out:
                _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        if isinstance(sdk_ev, _RS):
            from omnigent_client import BlockContext, ReasoningStartBlock

            items_out = list(
                fmt.format_reasoning_start(
                    ReasoningStartBlock(
                        ctx=BlockContext(
                            agent=session._agent_name,  # type: ignore[union-attr]
                            depth=0,
                            turn=0,
                        ),
                    ),
                ),
            )
            if tape_entry is not None:
                _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
            for item in items_out:
                host.output(item)
            if tape_entry is not None and items_out:
                _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        if isinstance(sdk_ev, _RD | _RSD):
            from omnigent_client import BlockContext
            from omnigent_client import ReasoningChunk as _RC

            items_out = list(
                fmt.format_reasoning_chunk(
                    _RC(
                        text=sdk_ev.delta,
                        ctx=BlockContext(
                            agent=session._agent_name,  # type: ignore[union-attr]
                            depth=0,
                            turn=0,
                        ),
                    ),
                ),
            )
            if tape_entry is not None:
                _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
            for item in items_out:
                host.output(item)
            if tape_entry is not None and items_out:
                _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        if isinstance(sdk_ev, _OIDE):
            item = sdk_ev.item
            if isinstance(item, dict):
                if (
                    item.get("type") == "function_call"
                    and item.get("status") == "action_required"
                    and session._tool_callables  # type: ignore[union-attr]
                ):
                    call_id = item.get("call_id", "")
                    name = item.get("name", "")
                    args_str = item.get("arguments", "{}")
                    sid = getattr(session, "session_id", None) or ""
                    if isinstance(call_id, str) and isinstance(name, str):
                        session._spawn_client_tool(  # type: ignore[union-attr]
                            sid,
                            call_id,
                            name,
                            str(args_str),
                        )
                item_type = item.get("type")
                if item_type == "slash_command" and _consume_pending_local_skill_slash_command(
                    session,
                    item,
                ):
                    if tape_entry is not None:
                        _event_tape.update_format(tape_entry, [])  # type: ignore[union-attr]
                        _maybe_log_tape_entry(tape_entry)
                    return
                # An assistant message whose prose is no longer in flight
                # may still be a duplicate: the relay publishes each
                # persisted text segment as an output_item.done AFTER the
                # tool-call boundary already committed the streamed prose
                # (resetting ``_saw_text_deltas``). Matching the item's
                # text against the turn's committed segments catches that
                # — see :class:`_TurnProseTracker`.
                _streamed_match = (
                    item_type == "message"
                    and item.get("role") == "assistant"
                    and not _saw_text_deltas
                    and _prose_tracker.consume_match(item)
                )
                plan = _plan_output_item_render(
                    item_type,
                    item.get("role"),
                    _saw_text_deltas or _streamed_match,
                )
                should_render = plan.render_item
                # When ``True``, the message-boundary flush below already
                # recorded the tape entry; the trailing ``elif`` must
                # not overwrite it with an empty marker.
                tape_handled = False
                if plan.flush_inflight_text and not should_render:
                    # Streamed deltas already rendered this assistant
                    # message; commit the trailing tail at the boundary
                    # instead of re-rendering the full item.
                    flush_items = _flush_inflight_assistant_text()
                    # The flush just recorded this segment's text; this
                    # item IS that segment's persisted copy, so consume
                    # the entry — a stale one could wrongly suppress a
                    # later identical (non-streamed) message this turn.
                    # Skipped when ``_streamed_match`` already consumed
                    # its entry above (the flush was then a no-op, and a
                    # second consume could eat a different identical
                    # segment's entry).
                    if not _streamed_match:
                        _prose_tracker.consume_match(item)
                    if tape_entry is not None:
                        _event_tape.update_format(tape_entry, flush_items)  # type: ignore[union-attr]
                        if flush_items:
                            _event_tape.mark_rendered(  # type: ignore[union-attr]
                                tape_entry,
                                len(flush_items),
                            )
                        tape_handled = True

                if should_render:
                    call_id_to_tool_metadata = getattr(
                        session,
                        "_live_call_id_to_tool_metadata",
                        None,
                    )
                    if call_id_to_tool_metadata is None:
                        call_id_to_tool_metadata = {}
                        session._live_call_id_to_tool_metadata = call_id_to_tool_metadata  # type: ignore[attr-defined]
                    if item_type == "function_call":
                        call_id = item.get("call_id")
                        name, arguments = _tool_metadata_from_function_call_item(item)
                        if isinstance(call_id, str) and name is not None:
                            call_id_to_tool_metadata[call_id] = (name, arguments or {})
                    if tape_entry is not None:
                        captured: list[object] = []
                        original_output = host.output

                        def _capturing_output(it: object) -> None:
                            captured.append(it)
                            original_output(it)

                        host.output = _capturing_output  # type: ignore[assignment]
                        try:
                            if plan.flush_inflight_text:
                                # Commit in-flight streamed prose before
                                # this tool call / output renders, so a
                                # new text block in the same turn doesn't
                                # append to (and re-render) the prior one.
                                _flush_inflight_assistant_text()
                            _render_history_item(
                                item,
                                host,
                                fmt,
                                call_id_to_tool_metadata=call_id_to_tool_metadata,
                            )
                        finally:
                            host.output = original_output  # type: ignore[assignment]
                        _event_tape.update_format(tape_entry, captured)  # type: ignore[union-attr]
                        _event_tape.mark_rendered(tape_entry, len(captured))  # type: ignore[union-attr]
                    else:
                        if plan.flush_inflight_text:
                            # See above: commit in-flight prose at the
                            # content-block boundary before rendering.
                            _flush_inflight_assistant_text()
                        _render_history_item(
                            item,
                            host,
                            fmt,
                            call_id_to_tool_metadata=call_id_to_tool_metadata,
                        )
                elif tape_entry is not None and not tape_handled:
                    _event_tape.update_format(tape_entry, [])  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        if isinstance(sdk_ev, _Created):
            session._current_response_id = sdk_ev.response.id  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        from omnigent_client._events import ResponseCompleted as _Completed

        if isinstance(sdk_ev, _Completed):
            # Update the toolbar context-ring with the best available
            # estimate of next-turn context fill.
            # ``context_tokens`` is populated by multi-call executors
            # (e.g. openai-agents) with the last sub-turn's total, which
            # correctly reflects context fill without over-counting the
            # repeated history across sub-turns. Single-call executors
            # don't set it, so we fall back to ``total_tokens`` (= input
            # + output for that one call), which is equally correct for
            # single-call turns.
            usage = sdk_ev.response.usage
            cw = getattr(session, "context_window", None)
            if usage is not None and cw:
                ring_tokens = getattr(usage, "context_tokens", None) or usage.total_tokens
                host.update_context_usage(ring_tokens, cw)
                _context_ring_state[0] = True  # provider reported usage; skip idle estimate
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        from omnigent_client._events import ErrorEvent as _ErrorEvent

        if isinstance(sdk_ev, _ErrorEvent):
            from omnigent_client import BlockContext, ErrorBlock

            items_out = list(
                fmt.format_error(
                    ErrorBlock(
                        message=sdk_ev.error.message,
                        source=sdk_ev.source,
                        ctx=BlockContext(agent=None, depth=0, turn=0),
                    ),
                )
            )
            if tape_entry is not None:
                _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
            for item in items_out:
                host.output(item)
            if tape_entry is not None and items_out:
                _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        from omnigent_client._events import ResponseFailed as _Failed

        if isinstance(sdk_ev, _Failed):
            err = sdk_ev.response.error
            msg = err.message if err else "unknown error"
            from omnigent_client import BlockContext, ErrorBlock

            items_out = list(
                fmt.format_error(
                    ErrorBlock(
                        message=msg,
                        source="llm",
                        ctx=BlockContext(agent=None, depth=0, turn=0),
                    ),
                )
            )
            if tape_entry is not None:
                _event_tape.update_format(tape_entry, items_out)  # type: ignore[union-attr]
            for item in items_out:
                host.output(item)
            if tape_entry is not None and items_out:
                _event_tape.mark_rendered(tape_entry, len(items_out))  # type: ignore[union-attr]
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        if isinstance(sdk_ev, _Elicit):
            # A mirrored sub-agent prompt names the child session that
            # parked on it via ``target_session_id``; resolve there so the
            # verdict reaches the parked child rather than 404ing against
            # the ancestor stream this event was relayed onto.
            sid = _elicitation_resolve_session_id(
                sdk_ev, getattr(session, "session_id", None) or ""
            )
            elicit_task = asyncio.create_task(
                session._handle_elicitation(sid, sdk_ev),  # type: ignore[union-attr]
            )
            _background_event_tasks.add(elicit_task)
            elicit_task.add_done_callback(_background_event_tasks.discard)
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

        if isinstance(sdk_ev, _Cancel):
            cid = sdk_ev.call_id
            pending = getattr(session, "_pending_local_tasks", {})
            if cid and cid in pending:
                task = pending[cid]
                if not task.done():
                    task.cancel()
            if tape_entry is not None:
                _maybe_log_tape_entry(tape_entry)
            return

    session._on_event = _render_session_event  # type: ignore[union-attr]

    def _maybe_log_tape_entry(entry: TapeEntry) -> None:
        """Write a tape entry to the JSONL log if the handle is open.

        :param entry: A :class:`TapeEntry` to log.
        """
        if _event_log_fh is not None:
            from omnigent.repl._event_tape import log_entry_jsonl

            log_entry_jsonl(_event_log_fh, entry)  # type: ignore[arg-type]

    is_streaming = False

    # Session id tracked for resume hints and session logs.
    conversation_id: str | None = resume_conversation_id
    # Active background event tasks, cancelled at REPL exit to prevent leaks.
    _background_event_tasks: set[asyncio.Task[None]] = set()

    def show_help() -> None:
        from rich.text import Text

        lines = []
        for name, (desc, _) in COMMANDS.items():
            if name in ("/?", "/exit"):
                continue  # Skip aliases.
            lines.append(
                f"  [{fmt.accent}]{name}[/{fmt.accent}]  [{fmt.muted}]{desc}[/{fmt.muted}]"
            )
        host.output(Text.from_markup("\n".join(lines)))

    host.on_help = show_help

    async def on_input(text: str, attachments: list[PendingAttachment] | None = None) -> None:
        nonlocal conversation_id, is_streaming

        # Pending schema field input: consume this line as the
        # current field's value before any other routing.
        if field_input_state.pending:
            field_name = field_input_state.field_name or "field"
            # Plain styled text: ``field_name`` (server schema) and
            # ``text`` (user input) must not be parsed as Rich markup.
            host.output(
                Text(f"   › {field_name}: {text}", style=fmt.muted),
            )
            field_input_state.resolve(text)
            return

        # Pending policy approval: consume this input as the
        # verdict BEFORE slash-command / normal-send routing.
        # The hook is awaiting a future; resolving it wakes
        # the SSE stream. Echo the user's choice in dim so the
        # transcript makes sense on scrollback — otherwise a
        # bare "y" would look like an unrelated message.
        if approval_state.pending:
            if approval_state._url_mode:
                host.output(
                    Text.from_markup(
                        f"   [{fmt.muted}]waiting for approval via the URL above[/{fmt.muted}]",
                    ),
                )
                return
            verdict = _parse_approval_input(text)
            verdict_label = {
                _ApprovalVerdict.APPROVE_ONCE: "approved",
                _ApprovalVerdict.APPROVE_ALWAYS: "approved always (this session)",
                _ApprovalVerdict.REFUSE: "refused",
            }[verdict]
            host.output(
                Text.from_markup(
                    f"   [{fmt.muted}]› {verdict_label}[/{fmt.muted}]",
                ),
            )
            approval_state.resolve_verdict(verdict)
            return

        # Slash commands are short tokens like "/help", "/clear".
        # File paths like "/Users/foo/bar.jpg" start with "/" but
        # contain more path separators — don't treat those as commands.
        first_token = text.split()[0] if text.split() else ""
        if first_token.startswith("/") and "/" not in first_token[1:]:
            await handle_slash_command(text, session, client, host, fmt)
            return

        # While observing a sub-agent read-only (dived in via ↓ on a CLOSED /
        # non-chattable child), refuse plain message sends — there's no live
        # runner to co-drive and a ``message`` to a closed session 409s.
        # Interactive-child mode (a still-open child) lifts this guard: the send
        # below POSTs to the CHILD's existing runner (co-drive), which the
        # ``_readonly_view`` bind-suppression already routes correctly without
        # moving the parent's runner. Slash commands (handled above) still work;
        # press ← to return to the top-level session.
        if getattr(session, "_readonly_view", False) and not getattr(
            session, "_interactive_child", False
        ):
            host.output(
                Text.from_markup(
                    f"   [{fmt.muted}]read-only view (closed sub-agent) — press ← to "
                    f"return to the main session before sending[/{fmt.muted}]",
                ),
            )
            return

        files = [a.path for a in attachments] if attachments else None
        cwd = os.getcwd()
        filenames = [os.path.relpath(a.path, cwd) for a in attachments] if attachments else None
        # The display text (``text``) has ``@path`` tokens stripped
        # — the ``📎`` chip shows them instead.  But the LLM needs
        # the paths inline so it knows which files are referenced.
        llm_text = text
        if filenames:
            suffix = " ".join(filenames)
            llm_text = f"{text} {suffix}".strip() if text else suffix

        if is_streaming:
            # Show the message immediately in dimmed style so the
            # user knows it sent, then steer the agent. Pad with
            # blank lines above AND below so the steering input
            # has visual breathing room — otherwise it wedges
            # directly between the streamed assistant text above
            # and the next tool-call block below. Mid-stream
            # steering has no response-start block to space off
            # against (the agent is already mid-turn), so the
            # caller-side trailing blank IS the only separator
            # before the next tool-call rendering.
            from rich.text import Text as RText

            host.output(RText.from_markup(""))
            host.output(fmt.steering_message(text, attachments=filenames))
            host.output(RText.from_markup(""))
            async for _ in session.send(llm_text, files=files):
                pass  # Steer yields nothing if delivered.
            return

        # Non-streaming (new-turn) user message: blank line
        # ABOVE the prompt to separate it from whatever was
        # there (prior turn, slash-command output, welcome
        # banner). No trailing blank: ``format_response_start``
        # in the SDK formatter already prefixes ``◆ model`` with
        # a ``\n``, so a blank here would stack into two blanks.
        from rich.text import Text as RText

        host.output(RText.from_markup(""))
        host.output(fmt.user_message(text, attachments=filenames))
        # Reset per-turn debug counters so the toolbar reflects only
        # events from the current turn, not cumulative history.
        if _event_tape is not None:
            _event_tape.reset_turn()

        host.start_timer()
        await asyncio.sleep(0)
        is_streaming = True
        try:
            # Sessions mode: send() just POSTs and waits for
            # _turn_done. All rendering is push-based via _on_event.
            async for _ in session.send(llm_text, files=files):
                pass
        except asyncio.CancelledError:
            # Escape key cancels this task. Tell the server to cancel
            # the in-progress response so the session state stays in
            # sync. Without this, _is_terminal stays False and the
            # next send() tries to steer a dead response.
            # shield() prevents the cancel() coroutine from being
            # re-cancelled by the propagating CancelledError.
            # Also refuse any pending approval fail-closed so the
            # hook's future doesn't leak waiting for a verdict
            # that will never come.
            approval_state.cancel()
            field_input_state.cancel()
            # Best-effort — server may already have finished.
            with contextlib.suppress(Exception):
                await asyncio.shield(session.cancel())
            from rich.text import Text as RText

            host.output(RText.from_markup(f"\n  [{fmt.muted}]cancelled[/{fmt.muted}]"))
            raise
        except Exception as exc:  # noqa: BLE001 — REPL UI boundary: any uncaught error here would be swallowed by prompt-toolkit's background runner, leaving the user staring at a silent prompt (see comment below for the concrete incident this guards against)
            # Any non-cancel exception from the server (HTTP 5xx from
            # ``raise_for_status``, transport errors, malformed SSE,
            # etc.) bubbles up through the session send path into
            # here. Without this branch, the exception propagates to
            # prompt-toolkit's background-task runner, which swallows
            # it silently — leaving the user staring at the prompt
            # with no idea why the agent produced no output. This
            # was the exact user-reported bug where ``omnigent chat`` would
            # return to the prompt after "Hello" with zero feedback
            # when ``OPENAI_BASE_URL`` was unset. Render as an
            # :class:`ErrorBlock` so the UI formatter surfaces the
            # panel it already knows how to draw for server-side
            # ``response.error`` events; users see a consistent error
            # UI regardless of whether the failure was pre-stream
            # (HTTP error) or mid-stream (ResponseFailed event).
            _log.exception("REPL send error (server/transport)")
            from omnigent_client import BlockContext, ErrorBlock

            host.output(RText.from_markup(""))  # separate from scrollback above
            host.output(
                fmt.format_error(
                    ErrorBlock(
                        message=str(exc),
                        source="server",
                        ctx=BlockContext(agent=None, depth=0, turn=0),
                    ),
                )[0],
            )
        finally:
            is_streaming = False
            host.stop_timer()
            conversation_id = getattr(session, "session_id", None)

    # Ctrl+O debug overview. Registered here — not inside the SDK —
    # because the content (conversation history, model metadata,
    # usage totals) is omnigent-specific. The SDK's
    # :class:`Overlay` primitive is intentionally content-agnostic;
    # the REPL owns what to render. See ``_build_debug_overview``
    # for the actual content.
    #
    # Why Ctrl+O and not Ctrl+G: Warp terminal (and some others)
    # intercepts Ctrl+G for its own AI Command Search before the
    # sequence reaches the running program, so the binding never
    # fires in `omnigent chat`'s pinned-prompt mode. Ctrl+O is not grabbed
    # by the common terminal emulators we target (iTerm2, Terminal.app,
    # Warp) and prompt-toolkit binds it cleanly.
    from omnigent_ui_sdk import Overlay

    async def _overview_builder(target: OverlayTarget) -> RenderableType:
        from omnigent.cli_diagnostics import current_cli_log_path

        return await _build_debug_overview(
            target,
            client=client,
            session=session,
            agent_name=agent_name,
            fmt=fmt,
            server_log_path=server_log_path,
            runner_log_path=runner_log_path,
            event_log_path=_event_log_path,
            cli_log_path=current_cli_log_path(),
        )

    async def _overview_targets() -> list[OverlayTarget]:
        return await _collect_overview_targets(client, session)

    # Per-target action keys for terminal targets. ``O`` opens
    # an attach in a fresh tmux window; ``R`` opens it
    # read-only. Capitalized so the binding doesn't fight with
    # lowercase letters that may be reserved for navigation /
    # search inside future overlay surfaces. Both no-op (with a
    # stderr message) when the selected target isn't a terminal
    # or when the user isn't running inside tmux to begin with —
    # there's nowhere to open the new window otherwise. Mirrors
    # the legacy non-AP mode F20-overlay shortcuts at
    # ``omnigent/inner/cli.py:1791-1797``.
    from omnigent_ui_sdk import OverlayAction

    async def _attach_handler(target: OverlayTarget, *, read_only: bool) -> None:
        await _open_terminal_in_tmux(
            target,
            client=client,
            read_only=read_only,
        )

    async def _attach_read_write(target: OverlayTarget) -> None:
        await _attach_handler(target, read_only=False)

    async def _attach_read_only(target: OverlayTarget) -> None:
        await _attach_handler(target, read_only=True)

    host.add_overlay(
        Overlay(
            trigger="c-o",
            builder=_overview_builder,
            targets_builder=_overview_targets,
            title=f" Debug overview — {ui_name}",
            actions=(
                OverlayAction(key="O", label="attach", handler=_attach_read_write),
                OverlayAction(key="R", label="attach (read-only)", handler=_attach_read_only),
            ),
        ),
    )

    # ── ↓ Sub-agents menu ──────────────────────────────────────
    # While sub-agents are running, the toolbar reads ``state: N agents
    # running`` instead of ``sleeping`` (see ``build_toolbar``) and a
    # ``↓ agents`` hint advertises the menu. Pressing Down on an empty input
    # opens an inline, navigable list of the running sub-agents at the bottom
    # of the terminal (the host owns the list UI); Enter switches into the
    # selected agent's live session via the ``on_subagent_select`` callback
    # wired below, Esc closes. The tree is fed live by
    # ``_apply_child_session_event`` (direct children) plus the recursive
    # ``_refresh_subagent_tree`` poll (deeper levels).

    # The session the tree is rooted at — the originally-launched "main"
    # session. It tracks the live top-level session id while the user is at
    # the top and freezes once they dive into a sub-agent, so the whole
    # hierarchy + the way back to main stay correct even if the main session
    # id changes (e.g. a runner rebind reassigns it). The adapter's
    # ``_readonly_view`` flag (set atomically by ``view_session`` and cleared
    # on switch / clear / new) is the single source of truth for "are we
    # observing a sub-agent below the root".
    subagent_root: list[str | None] = [None]

    # The root the selector tree was last discovered for. Lets the poll run a
    # one-shot discovery whenever the root CHANGES (resume / ``/switch`` into a
    # session that already has children, with no fresh SSE to seed them) even
    # though no sub-agents are registered yet — see ``_subagent_poll_loop``.
    polled_root: list[str | None] = [None]

    def _sync_subagent_root() -> None:
        # Track the live top-level session id while we're at the top. While
        # observing a sub-agent — either read-only OR co-driving it
        # interactively — freeze the root: never self-heal off
        # ``session.session_id``, which would capture the sub-agent as the root
        # and make Left-arrow "back to main" vanish. The root is DECOUPLED from
        # ``_readonly_view`` alone: interactive-child mode keeps ``_readonly_view``
        # set, but we also guard on ``_interactive_child`` so a future change
        # that toggles read-only can't silently re-root onto the child.
        # ``view_session`` sets the session id + both flags together, so this
        # never sees a half-applied switch.
        observing_child = getattr(session, "_readonly_view", False) or getattr(
            session, "_interactive_child", False
        )
        if not observing_child and session.session_id is not None:
            subagent_root[0] = session.session_id

    async def _refresh_subagents() -> None:
        root_id = subagent_root[0]
        if root_id is None:
            return
        # Capture the generation BEFORE the fetch so a clear-during-poll
        # (``/switch`` / ``/new`` / ``/clear`` re-rooting mid-fetch) makes the
        # resulting seed a no-op instead of resurrecting cleared nodes.
        await _refresh_subagent_tree(client, host, root_id, generation=host.subagent_generation)

    async def _subagent_poll_loop() -> None:
        # Periodically re-fetch the tree so nested (grandchild) levels + live
        # statuses stay current — the SSE stream only carries the active
        # session's direct children. The root-sync runs every tick (cheap, no
        # I/O) so the root stays accurate. The tree re-fetch fires while there
        # is live work to track — an active sub-agent, or a child the user has
        # dived into (whose own stream can't refresh its row) — OR when the root
        # just changed (the discovery poll that repopulates the selector after a
        # resume / ``/switch`` into a session that already has children, which
        # would otherwise never poll: no SSE, no nodes yet). It deliberately
        # goes quiet once everything settles at the top level: a finished
        # sub-agent's status no longer changes, so polling retained-but-terminal
        # nodes forever is pure waste; a child that later resumes re-arms the
        # poll via the active stream's ``session.child_session.updated``.
        while True:
            try:
                _sync_subagent_root()
                root_id = subagent_root[0]
                observing_subagent = getattr(session, "_readonly_view", False) or getattr(
                    session, "_interactive_child", False
                )
                if _should_discover_subagents(
                    root_id,
                    has_active_subagents=host.has_active_subagents(),
                    observing_subagent=observing_subagent,
                    last_polled_root=polled_root[0],
                ):
                    await _refresh_subagents()
                    polled_root[0] = root_id
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — best-effort background poll; never crash the REPL
                pass
            await asyncio.sleep(_SUBAGENT_POLL_SECONDS)

    async def _open_subagent_by_id(target_id: str) -> None:
        # Invoked by the host when the user picks a row in the inline ↓ menu
        # or presses Left to go back. Runs between prompt iterations, so
        # re-pointing + re-rendering is safe.
        #
        # Use ``view_session`` (read-only re-point), NOT ``switch_to_session``
        # (which moves the runner binding): diving into a running sub-agent
        # must not unbind the parent (it would never wake to collect the
        # result) nor hijack the child's runner (it would be left stuck
        # "still running" with nothing delivered).
        if not target_id or target_id == session.session_id:
            return  # Already viewing this session (e.g. selected "main").
        # Returning to the top-level session re-enables runner ownership;
        # diving into a sub-agent is always read-only of the runner BINDING (no
        # rebind). A still-open child additionally becomes an interactive
        # co-drive target — the user can type to chat with it, POSTing to the
        # child's own runner (web-UI parity) without moving the parent's
        # binding. A CLOSED child is view-only (a ``message`` to it 409s).
        # ``view_session`` applies the session id + flags atomically, which both
        # freezes the root (so Left-arrow still returns to the parent after a
        # chat) and re-roots on return.
        returning_to_root = subagent_root[0] is not None and target_id == subagent_root[0]
        interactive = not returning_to_root and host.is_subagent_chattable(target_id)
        try:
            await session.view_session(
                target_id,
                read_only=not returning_to_root,
                interactive=interactive,
            )
        except Exception as exc:  # noqa: BLE001 — REPL boundary: render the failure, stay alive
            host.output(
                Text.from_markup(f"  [bold red]Failed to open {target_id[:16]}…: {exc}[/]")
            )
            return
        await _attach_to_conversation(
            target_id,
            session,
            client,
            host,
            fmt,
            ui_name=_humanize_agent_name(session.model),
            redraw_screen=True,
        )
        # Tell the user which mode they're in so a closed child doesn't look
        # silently unresponsive when a typed message is refused.
        if interactive:
            host.output(
                Text.from_markup(
                    f"  [{fmt.muted}]interactive — type to chat with this sub-agent; "
                    f"← back to main[/{fmt.muted}]"
                )
            )
        elif not returning_to_root:
            host.output(
                Text.from_markup(
                    f"  [{fmt.muted}]read-only (closed sub-agent) — ← back to main[/{fmt.muted}]"
                )
            )
        await _refresh_subagents()

    host.on_subagent_select = _open_subagent_by_id
    # Let the host see the active session id so Left-arrow can return to the
    # top-level session whenever the user is inside a sub-agent.
    host.active_session_id_getter = lambda: session.session_id

    # ── Ctrl+E event tape overlay (--debug-events only) ────────
    # Registered unconditionally only when the debug flag is set.
    # Uses the two-pane Overlay mode: the sidebar lists every tape
    # entry (type + delta + stage icon), and selecting one shows
    # the full detail panel — pipeline journey + raw JSON payload.
    if debug_events and _event_tape is not None:
        from omnigent.repl._event_tape import build_tape_detail, build_tape_targets

        async def _tape_builder(target: OverlayTarget | None) -> RenderableType:
            """Build the detail panel for the selected tape entry.

            :param target: The selected sidebar entry, or ``None``
                when no entries exist.
            :returns: Rich renderable for the detail panel.
            """
            if target is None:
                return Text.from_markup("[dim]No events recorded yet.[/dim]")
            return build_tape_detail(
                _event_tape,
                target.key,
                fmt,  # type: ignore[arg-type]
            )

        async def _tape_targets() -> list[OverlayTarget]:
            """Build the sidebar target list from the tape.

            :returns: One :class:`OverlayTarget` per tape entry.
            """
            from omnigent.repl._event_tape import _OverlayTargetLike

            raw_targets: list[_OverlayTargetLike] = build_tape_targets(
                _event_tape,  # type: ignore[arg-type]
            )
            return [OverlayTarget(key=t.key, label=t.label, icon=t.icon) for t in raw_targets]

        host.add_overlay(
            Overlay(
                trigger="c-e",
                builder=_tape_builder,
                targets_builder=_tape_targets,
                title=" SSE Event Tape",
                sidebar_width=30,
            ),
        )

    async with host:
        # ── Open JSONL event log when --debug-events is on ────
        # Opened inside ``async with host:`` so the finally block
        # that closes it is guaranteed to run even if host setup
        # succeeds but a later step fails.
        if debug_events:
            import time as _dbg_time

            from omnigent.repl._event_tape import open_event_log

            _sid = resume_conversation_id or f"fresh-{int(_dbg_time.time())}"
            _event_log_path = open_event_log(_sid)
            session._event_log_path = _event_log_path  # type: ignore[attr-defined]
            _event_log_fh = open(_event_log_path, "a")  # noqa: SIM115 — closed in finally below

        # Mirror the legacy CLI's mascot-art startup banner so the
        # Omnigent REPL feels identical at boot. Raw stdout write
        # (matching ``omnigent/inner/cli.py:2962``) — the banner
        # is a pre-formatted ANSI string with explicit centering;
        # routing it through ``host.output`` (which renders via a
        # Rich Console at the current terminal width) risks double-
        # padding or wrap surprises, and we don't need the SDK's
        # stream-state bookkeeping at REPL boot since nothing has
        # streamed yet. See
        # ``designs/RUN_OMNIGENT_REPL_PARITY.md``.
        import sys as _sys

        # Resolve the Claude-Code-style header data (folder, model,
        # credential, one-line summary, per-family creds). Best-effort:
        # the header reads the provider config, so on any failure we fall
        # back to the plain name-only banner rather than blocking boot.
        _header: _StartupHeader | None = None
        if attach_only:
            # Session-honest attach banner: agent name + harness + folder. No
            # host-local credential badge and no fresh-start "spawn sub-agents"
            # hint — this is a co-drive client joining the host's live session,
            # not the runner owner, so those would reflect the wrong machine.
            _header = _StartupHeader(
                folder=_display_cwd(),
                description=None,
                model_label=harness,
                credential=None,
                creds_line=None,
            )
        else:
            try:
                _header = _build_startup_header(harness, agent_description, used_families)
            except Exception:  # noqa: BLE001 — startup-UI boundary: a config read must never block REPL boot
                _log.exception("Failed to build startup header; falling back to plain banner")
        # Installed server version for the header's "server <ver>" row.
        # Probed via the connected (authenticated) client so a short, bounded
        # GET /v1/info never stalls boot and answers even on auth-gated hosted
        # servers; None on any failure simply omits the row. Skipped when:
        #   - there's no header (minimal banner ignores the version), or
        #   - the server is a Databricks workspace mount — a workspace build
        #     reports no meaningful version string (its /api/version returns a
        #     placeholder like "source"), so showing it is noise.
        from omnigent.conversation_browser import is_workspace_hosted_url

        _show_version = _header is not None and not (
            server_url is not None and is_workspace_hosted_url(server_url)
        )
        server_version = await _fetch_server_version(client) if _show_version else None
        _sys.stdout.write(
            _render_startup_banner_ansi(
                ui_name,
                server_url=server_url,
                server_version=server_version,
                header=_header,
            )
        )
        _sys.stdout.flush()

        from omnigent_ui_sdk import StreamingText

        host.output(StreamingText(text="\n\n\n"))
        # Resume an existing conversation when requested.
        # ``redraw_screen=False`` because the welcome banner
        # was just printed above — a second banner would
        # double-render and the cleared scrollback would push
        # the first banner off-screen, making the welcome
        # appear twice. ``ui_name`` is reused so any banner
        # text (the "Resumed conversation …" line) matches
        # the panel's display name and avoids the
        # ``resume_test`` / ``resume test`` mismatch.
        if resume_conversation_id is not None:
            try:
                await _attach_to_conversation(
                    resume_conversation_id,
                    session,
                    client,
                    host,
                    fmt,
                    ui_name=ui_name,
                    redraw_screen=False,
                )
                session._notify_session_start_once()
            except Exception as exc:  # noqa: BLE001 — REPL boundary: never crash on resume failure; render and proceed
                _log.exception("Failed to resume conversation %s", resume_conversation_id)
                host.output(
                    Text.from_markup(
                        f"  [bold red]Failed to resume {resume_conversation_id[:16]}…: {exc}[/]"
                    )
                )
        # Hold a reference to the auto-send task for the lifetime of
        # ``host.run`` — ``asyncio.create_task`` only weakly roots its
        # result, so dropping the handle would let the GC collect the
        # task mid-execution and the initial message could vanish.
        auto_send_task: asyncio.Task[None] | None = None
        if initial_message:
            # Auto-send the initial message (e.g. onboarding greeting).
            auto_send_task = asyncio.create_task(on_input(initial_message))
        # Background poll that keeps the sub-agent tree (badge + ↓ menu)
        # current at nested depths for the lifetime of ``host.run``.
        subagent_poll_task = asyncio.create_task(_subagent_poll_loop())
        try:
            await host.run(on_input)
        finally:
            subagent_poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await subagent_poll_task
            if auto_send_task is not None and not auto_send_task.done():
                auto_send_task.cancel()
            for _task in list(_background_event_tasks):
                _task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await _task
            # Close the JSONL event log file handle if it was opened.
            if _event_log_fh is not None:
                _event_log_fh.close()  # type: ignore[union-attr]
    close_session = getattr(session, "aclose", None)
    if close_session is not None:
        result = close_session()
        if inspect.isawaitable(result):
            await result
    # Write the session log (--log) BEFORE the goodbye banner so the
    # user sees the path in their final scroll. The dump uses the
    # same connected client, so it runs inside the same async-with
    # scope that opened it. Failures are caught + reported on
    # stderr; the REPL still exits cleanly.
    if log_dir is not None:
        await _maybe_write_session_log(
            client,
            session,
            agent_name,
            log_dir,
            host,
            fmt,
        )
    # The sessions adapter tracks the durable session_id which doubles as
    # the conversation_id for resume purposes. Prefer it over the local
    # ``conversation_id`` fallback.
    conv_id = getattr(session, "session_id", None) or conversation_id
    # Top-level ``omnigent resume`` only dispatches claude-native today;
    # the REPL exit path is always chat/run, so print the original-invocation
    # form. ``resume_parts`` already carries --server etc.
    resume_hint: str | None = None
    if conv_id is not None and not ephemeral and resume_parts is not None:
        import shlex

        resume_hint = shlex.join([*resume_parts, "--resume", conv_id])
    host.output(fmt.goodbye(resume_hint=resume_hint))
    unregister_skill_commands(_registered_skill_cmds)
    return conv_id


async def _maybe_write_session_log(
    client: OmnigentClient,
    session: Session,
    agent_name: str,
    log_dir: pathlib.Path,
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """
    Resolve the active conversation id from the session and write
    its JSON dump to *log_dir*.

    Sessions mode uses the session id as the conversation id, so no
    response lookup is needed.

    :param client: Connected OmnigentClient — REUSED, not opened
        here, so we ride inside the caller's ``async with`` scope.
    :param session: The REPL session object whose
        ``current_response_id`` we read.
    :param agent_name: Agent name to embed in the dump.
    :param log_dir: Directory to write under,
        e.g. ``Path("~/.omnigent/logs").expanduser()``.
    :param host: TerminalHost for surfacing the result line.
    :param fmt: TimedFormatter for muted-text styling.
    """
    from omnigent.repl._session_log import write_session_log

    conversation_id = getattr(session, "session_id", None)
    if not conversation_id:
        # User exited without sending a single message. Nothing to
        # log because the SessionsChat is created lazily on first send.
        return
    try:
        path = await write_session_log(
            client,
            conversation_id,
            agent_name=agent_name,
            log_dir=log_dir,
        )
    except Exception as exc:  # noqa: BLE001 — REPL UI boundary: same rationale as above
        _log.exception("Session log write failed")
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]session log write failed "
                f"({type(exc).__name__}: {exc})[/{fmt.muted}]"
            )
        )
        return
    host.output(Text.from_markup(f"  [{fmt.muted}]wrote session log to {path}[/{fmt.muted}]"))


def _clear_screen() -> None:
    """Clear visible content by scrolling it off screen."""

    try:
        height = os.get_terminal_size().lines
    except (ValueError, OSError):
        height = 24
    print("\n" * height, end="", flush=True)


# ── Slash commands ───────────────────────────────────────

# Single registry: name → (help string, handler).
#
# Every handler MUST accept the same 5 positional parameters —
# ``(arg, session, client, host, fmt)`` — because the dispatcher
# at the bottom of this file invokes every handler with the exact
# same positional call (see the ``await handler(...)`` site).
# Individual handlers typically use only a subset of these, which
# would normally trip ruff's unused-argument rule; the per-handler
# ARG001 waivers on the function signatures below document that
# the unused args are part of the dispatch contract, not dead code.

COMMANDS: dict[str, tuple[str, SlashCommandHandler]] = {}


def _cmd(
    name: str,
    help_text: str,
) -> Callable[[SlashCommandHandler], SlashCommandHandler]:
    """Decorator to register a slash command."""

    def _register(fn: SlashCommandHandler) -> SlashCommandHandler:
        COMMANDS[name] = (help_text, fn)
        return fn

    return _register


@_cmd("/help", "Show this help")
async def _cmd_help(
    arg: str,  # noqa: ARG001 — dispatch-contract params (see COMMANDS docstring)
    session: Session,  # noqa: ARG001
    client: OmnigentClient,  # noqa: ARG001
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    from rich.text import Text

    # Grouped, column-aligned command help instead of a flat alphabetical
    # wall. Commands not listed in a group still render (under "Other"), so a
    # newly registered command is never silently hidden from /help.
    groups: list[tuple[str, list[str]]] = [
        ("Chat", ["/new", "/clear", "/switch", "/fork", "/history", "/cancel"]),
        ("Context", ["/compact", "/context", "/model", "/effort"]),
        ("Display", ["/theme"]),
        ("Diagnostics", ["/logs", "/report"]),
        ("Help", ["/help", "/quit"]),
    ]
    visible = {n: d for n, (d, _) in COMMANDS.items() if n not in ("/?", "/exit")}
    grouped = {name for _, names in groups for name in names}
    leftover = [n for n in visible if n not in grouped]
    if leftover:
        groups.append(("Other", leftover))

    name_width = max((len(n) for n in visible), default=0)
    lines: list[str] = []
    for title, names in groups:
        rows = [(n, visible[n]) for n in names if n in visible]
        if not rows:
            continue
        if lines:
            lines.append("")  # blank line between sections
        lines.append(f"  [{fmt.muted}]{title}[/{fmt.muted}]")
        for name, desc in rows:
            padded = name.ljust(name_width)
            lines.append(
                f"    [{fmt.accent}]{padded}[/{fmt.accent}]  [{fmt.muted}]{desc}[/{fmt.muted}]"
            )
    host.output(Text.from_markup("\n".join(lines)))


COMMANDS["/?"] = COMMANDS["/help"]


_THEME_CLEAR_ALIASES = {"default", "auto", "reset"}


@_cmd("/theme", "Show/set terminal theme; /theme light or /theme dark")
async def _cmd_theme(
    arg: str,
    session: Session,  # noqa: ARG001 — dispatch-contract params
    client: OmnigentClient,  # noqa: ARG001 — dispatch-contract params
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """Show or explicitly set the TUI's light/dark palette.

    ``/theme`` (no args) shows the current theme and usage hint.
    ``/theme dark`` / ``/theme light`` sets explicitly with a preview.
    ``/theme default`` resets to the built-in default (light).
    """
    from omnigent_ui_sdk.terminal._theme import DARK_THEME, LIGHT_THEME
    from rich.text import Text

    from omnigent.repl._theme_picker import _build_preview, build_theme_confirmation

    value = arg.strip().lower()
    if not value:
        current = getattr(host, "theme", LIGHT_THEME).name
        host.output(Text.from_markup(f"  [{fmt.muted}]theme: {current}[/{fmt.muted}]"))
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]usage: /theme light · /theme dark · "
                f"/theme default to reset[/{fmt.muted}]"
            )
        )
        return
    if value in _THEME_CLEAR_ALIASES or value == "light":
        selected = LIGHT_THEME
    elif value == "dark":
        selected = DARK_THEME
    else:
        host.output(
            Text.from_markup("  [bold red]Invalid theme: expected light, dark, or default[/]")
        )
        return

    if value in _THEME_CLEAR_ALIASES:
        save_user_config(DEFAULT_USER_CONFIG)
    else:
        update_user_config(theme=selected.name)
    host.set_theme(selected)
    fmt.set_theme(selected)
    # Show confirmation + preview panel via host.output() so it
    # integrates cleanly with prompt-toolkit (no alternate screen,
    # no nested Application).
    host.output(build_theme_confirmation(selected))
    host.output(_build_preview(selected.name))


_EFFORT_VALUES = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_EFFORT_CLEAR_ALIASES = {"default", "off", "reset"}


async def _set_session_reasoning_effort(
    session: Session,
    effort: str | None,
) -> None:
    """
    Set reasoning effort on either legacy or sessions-backed chat.

    The legacy SDK helper exposes a synchronous
    ``set_reasoning_effort`` mutator. The sessions-backed REPL
    adapter persists via HTTP and therefore returns an awaitable.
    This adapter keeps the slash command surface shared while still
    awaiting the server-backed path.

    :param session: Current REPL session.
    :param effort: New effort, e.g. ``"high"``, or ``None`` to
        clear to the agent default.
    :returns: None.
    """
    result = session.set_reasoning_effort(effort)
    if inspect.isawaitable(result):
        await result


@_cmd("/effort", "Show/set reasoning effort; /effort lists options")
async def _cmd_effort(
    arg: str,
    session: Session,
    client: OmnigentClient,  # noqa: ARG001 — dispatch-contract params
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """Show or set the session-level reasoning effort override."""
    from rich.text import Text

    value = arg.strip().lower()
    if not value:
        current = getattr(session, "reasoning_effort", None)
        selected = current or "default"
        label = "reasoning effort: default" if current is None else f"reasoning effort: {current}"
        rendered_values = ", ".join(
            f"[{opt}]" if opt == selected else opt for opt in _EFFORT_VALUES
        )
        rendered_default = "[default]" if selected == "default" else "default"
        rendered_options = f"{rendered_values} {rendered_default}"
        host.output(Text.from_markup(f"  [{fmt.muted}]{label}[/{fmt.muted}]"))
        host.output(Text.from_markup(f"  [{fmt.muted}]options: {rendered_options}[/{fmt.muted}]"))
        return

    if value in _EFFORT_CLEAR_ALIASES:
        await _set_session_reasoning_effort(session, None)
        suffix = " (current response unchanged)" if session.is_streaming else ""
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]reasoning effort reset to agent default{suffix}[/{fmt.muted}]"
            )
        )
        return

    if value not in _EFFORT_VALUES:
        host.output(
            Text.from_markup(
                "  [bold red]Invalid effort: "
                f"{value} · expected none, minimal, low, medium, high, xhigh, max, or default[/]"
            )
        )
        return

    await _set_session_reasoning_effort(session, value)
    suffix = " (current response unchanged)" if session.is_streaming else ""
    host.output(
        Text.from_markup(
            f"  [{fmt.muted}]reasoning effort set to {value} "
            f"for future responses{suffix}[/{fmt.muted}]"
        )
    )


_MODEL_CLEAR_ALIASES = {"default", "off", "reset"}


def _model_readout_harness(active_model: str | None) -> str:
    """Infer the harness whose active credential ``/model`` should describe.

    The REPL ``Session`` does not carry its harness name, but the active
    credential readout is per-family (anthropic vs openai). We infer the
    family from the active model string (the in-session override if set,
    else the agent's spec model) via
    :func:`omnigent.llms.routing.infer_harness_from_model`, falling back
    to ``"claude-sdk"`` (the anthropic surface) when the model is
    unrecognised — that yields the anthropic-family default, the most
    common single-key setup, rather than guessing the openai surface.

    :param active_model: The override or spec model, e.g.
        ``"openai/gpt-5.5"`` or ``"claude-sonnet-4-6"``, or ``None`` when
        neither is set.
    :returns: A canonical harness name, e.g. ``"claude-sdk"`` or
        ``"openai-agents"``.
    """
    from omnigent.llms.routing import infer_harness_from_model

    if active_model:
        inferred = infer_harness_from_model(active_model)
        if inferred:
            return inferred
    return "claude-sdk"


def _session_readout_harness(session: Session) -> str:
    """Resolve the harness the ``/model`` readout should describe.

    Prefers the session's actual bound harness
    (:attr:`SessionResponse.harness`, threaded through the client into
    ``session.harness``) so the readout reflects the real provider family —
    anthropic for claude-sdk, openai for codex / openai-agents. Falls back
    to inferring from the active model string (:func:`_model_readout_harness`)
    only when the server reported no harness (older sessions / not yet
    hydrated). Inference is unreliable when the agent declares no model (a
    generic-provider launcher), which is exactly when it wrongly defaulted
    to claude-sdk and reported the anthropic family for an openai-agents run.

    :param session: The REPL session.
    :returns: A canonical harness name, e.g. ``"openai-agents"`` or
        ``"claude-sdk"``.
    """
    harness = getattr(session, "harness", None)
    if harness:
        return harness
    return _model_readout_harness(
        getattr(session, "model_override", None) or getattr(session, "llm_model", None)
    )


def _build_model_readout_lines(
    config: dict[str, object],
    harness: str,
    model_override: str | None,
) -> list[str]:
    """Build the ``/model`` (no-arg) active-credential readout lines.

    Renders one ``Active:`` line — ``<model> · <glyph friendly-provider>
    · <source>`` via :func:`describe_active_credential` — using the kind
    glyph in place of the kind word so a databricks provider named
    ``databricks`` doesn't render as the redundant ``databricks ·
    databricks``. When other providers are also configured, an ``Also
    configured:`` line lists them (friendly names + glyphs) with honest
    guidance: ``/model`` only changes the model within the active
    provider — switching the active provider mid-session is not wired, so
    it goes through ``omnigent setup --no-internal-beta`` + a restart. Falls
    back to the legacy ``(agent default)`` line when nothing is configured
    for the harness's surface.

    No ambient-shadow warning is emitted: a configured default
    (``default: true``) takes precedence over ambient env keys, so an
    ambient ``$ANTHROPIC_API_KEY`` does *not* shadow it — warning the
    opposite was misleading. (If ambient is what's actually used, no
    default is configured and the ``(agent default)`` branch is taken.)

    :param config: The parsed effective config mapping (``providers:``
        block), e.g. from
        :func:`omnigent.onboarding.provider_config.load_config`.
    :param harness: The harness whose credential to describe, e.g.
        ``"claude-sdk"``.
    :param model_override: The in-session ``/model`` override, e.g.
        ``"openai/gpt-5.5"``, or ``None``.
    :returns: Plain (un-markup) display lines, e.g.
        ``["Active:  claude-sonnet-4-6  ·  🔑 Anthropic API Key  ·  $ANTHROPIC_API_KEY",
        "Also configured:  🧱 Databricks", "  /model <name> changes the model. …"]``.
    """
    from omnigent.onboarding.configure_models import (
        credential_label,
        kind_glyph,
    )
    from omnigent.onboarding.provider_config import (
        PI_SURFACE,
        describe_active_credential,
        harness_family,
        load_providers,
        provider_families,
    )

    lines: list[str] = []
    cred = describe_active_credential(config, harness, model_override=model_override)
    if cred is None:
        # Nothing resolves for this harness's surface — not in the explicit
        # config, and nothing ambient was detected (the merged view the
        # caller passes already includes detections). Be honest: report
        # None rather than fabricate a family default. An in-session
        # override is still shown (it's real), but its provider is
        # unresolved until one is configured.
        if model_override is not None:
            lines.append(f"Active:  {model_override}  ·  (provider unresolved)")
        else:
            lines.append("Active:  None  ·  None")
            lines.append(
                "no model configured — run `omnigent setup --no-internal-beta` to add one"
            )
        lines.append("usage: /model <name> · /model default | off | reset to clear")
        return lines

    # One clean "Active" line: <model> · <glyph friendly-provider> · <source>.
    # The kind glyph stands in for the kind word, so a databricks provider
    # named "databricks" no longer renders as the redundant "databricks ·
    # databricks". A provider whose model is chosen elsewhere (databricks
    # profile, subscription CLI) gets an explicit phrase instead of a model.
    if cred.model:
        model_label = cred.model
    elif cred.kind == "databricks":
        model_label = "(Databricks profile picks the model — pin one with /model <name>)"
    elif cred.kind == "subscription":
        model_label = "(CLI login picks the model — pin one with /model <name>)"
    else:
        model_label = "(no model pinned — set one with /model <name>)"
    glyph = kind_glyph(cred.kind)
    # credential_label is the single source of truth shared with `configure
    # harnesses` — a subscription reads "Subscription" (not the brand name
    # "Claude"), a key names the vendor + "API Key", Databricks names itself.
    provider_label = f"{glyph} {credential_label(cred.kind, cred.provider_name)}".strip()
    lines.append(f"Active:  {model_label}  ·  {provider_label}  ·  {cred.source}")

    # List the OTHER configured providers that serve THIS harness's family,
    # so the user only sees relevant alternatives (a Codex run shouldn't list
    # Claude-only providers). A both-family harness (pi) maps to no single
    # family — filter its alternates on the pi surface instead, which every
    # kind but subscription serves (a CLI login can't drive pi).
    providers = load_providers(config)
    fam = harness_family(harness)
    surface = fam if fam is not None else PI_SURFACE
    others = [
        (name, entry)
        for name, entry in providers.items()
        if name != cred.provider_name and surface in provider_families(entry)
    ]
    if others:
        items = [
            (
                f"{kind_glyph(e.kind)} "
                f"{credential_label(e.kind, n, profile=e.profile, display_name=e.display_name)}"
            ).strip()
            for n, e in others
        ]
        lines.append("Also configured:  " + "  ·  ".join(items))
        # Honest guidance: `/model` only changes the model within the active
        # provider; switching the active provider mid-session is not wired,
        # so it goes through `configure harnesses` + a restart.
        lines.append(
            "  /model <name> changes the model. To switch provider: omnigent setup (then restart)."
        )
    return lines


def _match_configured_provider(config: dict[str, object], token: str) -> str | None:
    """Resolve *token* to a configured provider name, or ``None``.

    Matches case-insensitively against both the raw provider keys and
    their friendly display names (so a user can type what the readout
    shows — ``"Anthropic"`` resolves to the configured ``"anthropic"``).
    Used by ``/model`` to detect (and reject) cross-provider switch
    attempts and to resolve a bare provider name to its default model.

    :param config: The parsed effective config mapping (``providers:``
        block).
    :param token: The user-typed token, e.g. ``"Anthropic"``,
        ``"anthropic"``, or a bare model name like ``"claude-opus-4-1"``.
    :returns: The canonical configured provider name (e.g.
        ``"anthropic"``) when *token* names one, else ``None`` (the token
        is a model name, not a provider).
    """
    from omnigent.onboarding.configure_models import provider_display_name
    from omnigent.onboarding.provider_config import load_providers

    low = token.lower()
    for name in load_providers(config):
        if name.lower() == low or provider_display_name(name).lower() == low:
            return name
    return None


def _resolve_provider_default_model(config: dict[str, object], provider_name: str) -> str | None:
    """Resolve a configured provider's default model for ``/model <provider>``.

    Looks up *provider_name* in the parsed providers and returns its
    family default model (anthropic preferred, else openai). Returns
    ``None`` when the provider is not configured or declares no default
    model (e.g. a bare gateway or a subscription whose CLI picks the
    model).

    :param config: The parsed effective config mapping.
    :param provider_name: The configured provider name, e.g.
        ``"anthropic"``.
    :returns: The provider's default model, e.g. ``"claude-sonnet-4-6"``,
        or ``None``.
    """
    from omnigent.onboarding.provider_config import (
        ANTHROPIC_FAMILY,
        OPENAI_FAMILY,
        load_providers,
    )

    providers = load_providers(config)
    entry = providers.get(provider_name)
    if entry is None:
        return None
    for family in (ANTHROPIC_FAMILY, OPENAI_FAMILY):
        default_model = entry.family_default_model(family)
        if default_model:
            return default_model
    return None


def _model_validation_warning(model: str) -> str | None:
    """Return a warning when *model* is not in the catalog, else ``None``.

    Validates ``provider/model`` against the bundled catalog
    (:func:`omnigent.onboarding.providers.get_chat_models`). An unknown
    provider prefix or an unlisted model returns a human-readable warning
    string; ``/model`` warns but does **not** block on it (gateways and
    new models are legitimately absent from the catalog).

    :param model: The model string the user passed, e.g.
        ``"openai/gpt-5.5"`` or ``"anthropic/claude-sonnet-4-6"``.
    :returns: A warning string when the model is not found in the catalog,
        e.g. ``"'openai/ghost' is not in the model catalog (continuing
        anyway)."``, or ``None`` when it validates.
    """
    from omnigent.errors import OmnigentError
    from omnigent.llms.routing import parse_model_string
    from omnigent.onboarding.providers import get_chat_models

    try:
        routed = parse_model_string(model)
    except OmnigentError:
        # A non-catalog prefix is normal for gateway / OSS models (e.g.
        # ``qwen/qwen3.7-plus`` via OpenRouter) — the gateway, not our
        # catalog, owns the naming. Inform, don't alarm.
        return f"{model!r} isn't a catalog model — fine for gateway / OSS models; using it as-is."
    catalog_models = {m.name for m in get_chat_models(routed.provider)}
    if catalog_models and routed.model not in catalog_models:
        return (
            f"{model!r} isn't in the local model catalog (may lag new releases); using it as-is."
        )
    return None


@_cmd("/model", "Show/set the LLM model for this session")
async def _cmd_model(
    arg: str,
    session: Session,
    client: OmnigentClient,  # noqa: ARG001 — dispatch-contract params
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """Show or set the session-level LLM model override.

    No-arg shows the active credential (model · provider · source) via
    :func:`describe_active_credential` plus the other configured
    providers. ``/model`` changes the *model within the active provider*
    only: ``/model <model>`` / ``/model <active-provider>/<model>``
    validate against the catalog (warn, never block) and set the override;
    a bare ``/model <active-provider>`` resolves that provider's default
    model. A value naming a **different** configured provider fails loud
    with guidance — switching the active provider mid-session is not wired
    (it goes through ``omnigent setup --no-internal-beta`` + a restart).
    ``/model default|off|reset`` clears the override.
    """
    from rich.text import Text

    value = arg.strip()
    if not value:
        from omnigent.onboarding.detected import effective_config_with_detected
        from omnigent.onboarding.provider_config import load_config

        current = getattr(session, "model_override", None)
        harness = _session_readout_harness(session)
        # Merge ambient detections so the readout names the provider that is
        # actually authenticating the turn (matching routing), never a guess.
        config = effective_config_with_detected(load_config())
        for line in _build_model_readout_lines(config, harness, current):
            host.output(Text.from_markup(f"  [{fmt.muted}]{line}[/{fmt.muted}]"))
        return

    if value.lower() in _MODEL_CLEAR_ALIASES:
        result = session.set_model_override(None)
        if inspect.isawaitable(result):
            await result
        suffix = " (current response unchanged)" if session.is_streaming else ""
        host.output(
            Text.from_markup(f"  [{fmt.muted}]model reset to agent default{suffix}[/{fmt.muted}]")
        )
        return

    # ``/model`` changes the *model* within the already-active provider. It
    # cannot switch the provider — that's resolved server-side from the
    # configured default / agent YAML, independently of the model override
    # (see _resolve_provider_for_build). So:
    #   - a value naming a DIFFERENT configured provider (by raw or friendly
    #     name) fails loud with guidance, instead of silently shipping a
    #     mismatched model string to the wrong provider's harness;
    #   - a bare value naming the ACTIVE provider resolves its default model;
    #   - anything else is treated as a model string within the active provider.
    from omnigent.onboarding.configure_models import kind_glyph, provider_display_name
    from omnigent.onboarding.detected import effective_config_with_detected
    from omnigent.onboarding.provider_config import (
        default_provider_for_harness,
        load_config,
    )

    # Merge ambient detections so the "active provider" the switch-guard
    # resolves matches what actually routes the turn.
    config = effective_config_with_detected(load_config())
    harness = _session_readout_harness(session)
    active = default_provider_for_harness(config, harness)
    active_name = active.name if active is not None else None

    candidate = value.split("/", 1)[0] if "/" in value else value
    matched = _match_configured_provider(config, candidate)
    if matched is not None and active_name is not None and matched != active_name:
        active_label = f"{kind_glyph(active.kind)} {provider_display_name(active_name)}".strip()
        target_label = f"{provider_display_name(matched)}"
        host.output(
            Text.from_markup(
                "  [bold red]Switching the active provider isn't supported mid-session.[/]"
            )
        )
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]Active provider: {active_label}. To use {target_label}, run "
                f"`omnigent setup --no-internal-beta` and select it as the "
                f"default, then restart. "
                f"(You can still change the model within {active_label}: /model <model-name>.)"
                f"[/{fmt.muted}]"
            )
        )
        return

    target = value
    if "/" not in value and matched is not None and matched == active_name:
        # Bare active-provider name → resolve its configured default model.
        resolved = _resolve_provider_default_model(config, matched)
        if resolved is None:
            # databricks / subscription pick their own model — nothing to set.
            host.output(
                Text.from_markup(
                    f"  [{fmt.muted}]{provider_display_name(matched)} picks the model itself; "
                    f"pass a specific one: /model <model-name>.[/{fmt.muted}]"
                )
            )
            return
        target = resolved
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]resolved provider {value!r} → {target}[/{fmt.muted}]"
            )
        )

    # Validate against the catalog — inform, but do NOT block (gateways and
    # brand-new models are legitimately absent from the bundled catalog, so
    # a non-catalog name is normal, not an error). Keep it a muted note.
    if "/" in target:
        warning = _model_validation_warning(target)
        if warning is not None:
            host.output(Text.from_markup(f"  [dim]note: {warning}[/dim]"))

    # set_model_override raises ValueError on empty-after-trim;
    # surface inline rather than letting it crash the REPL.
    try:
        result = session.set_model_override(target)
        if inspect.isawaitable(result):
            await result
    except ValueError as exc:
        host.output(Text.from_markup(f"  [bold red]Invalid model: {exc}[/]"))
        return

    suffix = " (current response unchanged)" if session.is_streaming else ""
    host.output(
        Text.from_markup(
            f"  [{fmt.muted}]model set to {target} for future responses{suffix}[/{fmt.muted}]"
        )
    )


async def _start_new_conversation(
    session: Session,
    host: TerminalHost,
    fmt: RichBlockFormatter,  # noqa: ARG001 — reserved for future banner styling
) -> bool:
    """Tear down the current session; legacy mode falls back to sync ``reset()``.

    :returns: ``False`` if the server unbind PATCH failed (rendered
        inline); caller skips the welcome banner redraw.
    """
    from rich.text import Text

    starter = getattr(session, "start_new_conversation", None)
    if callable(starter):
        try:
            await starter()
        except Exception as exc:  # noqa: BLE001 — REPL boundary
            _log.exception("New conversation failed")
            host.output(Text.from_markup(f"  [bold red]New conversation failed: {exc}[/]"))
            return False
    else:
        session.reset()
    # Drop the prior conversation's sub-agent tree so its agents don't linger
    # in the badge / ↓ menu under the fresh session.
    host.clear_subagents()
    return True


@_cmd("/new", "Start a new conversation (keeps scrollback)")
async def _cmd_new(
    arg: str,  # noqa: ARG001 — dispatch-contract params
    session: Session,
    client: OmnigentClient,  # noqa: ARG001
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """Start a new conversation in place; the prior transcript stays on screen."""
    from rich.text import Text

    if not await _start_new_conversation(session, host, fmt):
        return
    # Humanize the agent name so the banner matches the initial
    # ``run_repl`` welcome (avoids a ``resume_test`` / ``resume test``
    # mismatch).
    host.output(fmt.welcome(_humanize_agent_name(session.model), hints=WELCOME_HINTS))
    host.output(Text.from_markup(f"\n  [{fmt.muted}]New conversation.[/{fmt.muted}]"))


@_cmd("/clear", "Clear the screen and start a new conversation")
async def _cmd_clear(
    arg: str,  # noqa: ARG001 — dispatch-contract params
    session: Session,
    client: OmnigentClient,  # noqa: ARG001
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """Clear the visible scrollback and start a new conversation.

    The prior conversation persists server-side and is resumable via
    ``/switch``.
    """
    from rich.text import Text

    if not await _start_new_conversation(session, host, fmt):
        return
    _clear_screen()
    host.output(fmt.welcome(_humanize_agent_name(session.model), hints=WELCOME_HINTS))
    host.output(Text.from_markup(f"\n  [{fmt.muted}]New conversation.[/{fmt.muted}]"))


@_cmd("/switch", "List or switch conversations")
async def _cmd_switch(
    arg: str,
    session: Session,
    client: OmnigentClient,
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    from datetime import datetime

    from rich.table import Table
    from rich.text import Text

    if not arg:
        sessions_list = await client.sessions.list(limit=20)
        if sessions_list:
            table = Table(title="Switch to…")
            table.add_column("#", style="bold " + fmt.accent)
            table.add_column("ID", style="dim")
            table.add_column("Title")
            table.add_column("Status", style="dim")
            table.add_column("Created", style="dim")
            for i, s in enumerate(sessions_list, 1):
                when = datetime.fromtimestamp(s.created_at).strftime("%b %d %H:%M")
                table.add_row(str(i), s.id, s.title or "(untitled)", s.status, when)
            host.output(table)
            host.output(
                Text.from_markup(f"  [{fmt.muted}]/switch <#> or <id> to resume[/{fmt.muted}]")
            )
        else:
            host.output(Text.from_markup(f"  [{fmt.muted}]No sessions.[/{fmt.muted}]"))
    else:
        if arg.isdigit():
            sessions_list = await client.sessions.list(limit=20)
            index = int(arg) - 1
            if index < 0 or index >= len(sessions_list):
                host.output(
                    Text.from_markup(
                        f"  [bold red]No session #{arg} "
                        f"({len(sessions_list)} listed). Run /switch with no "
                        f"argument to see the table.[/]"
                    )
                )
                return
            arg = sessions_list[index].id
        try:
            # Sessions mode: re-point the adapter (session_id,
            # runner PATCH, SSE pump) before re-rendering history.
            # session.reset() / resume_from_response() are no-ops
            # in sessions mode, so without this the REPL keeps
            # sending to the original session.
            await session.switch_to_session(arg)  # type: ignore[attr-defined]
            # Drop the prior session's sub-agent tree so its agents don't
            # linger under the switched-to session's root.
            host.clear_subagents()

            # ``/switch`` runs mid-session, so the user already
            # has prior-conversation transcript on screen —
            # redraw to clear that visual context and replace
            # it with the full target conversation rendered
            # below the welcome banner.
            await _attach_to_conversation(
                arg,
                session,
                client,
                host,
                fmt,
                ui_name=_humanize_agent_name(session.model),
                redraw_screen=True,
            )
        except Exception as exc:  # noqa: BLE001 — REPL UI boundary: render network/server errors as inline text so the REPL stays responsive instead of crashing
            host.output(Text.from_markup(f"  [bold red]Error: {exc}[/]"))


async def _attach_to_conversation(
    conversation_id: str,
    session: Session,
    client: OmnigentClient,
    host: TerminalHost,
    fmt: RichBlockFormatter,
    *,
    ui_name: str,
    redraw_screen: bool,
) -> None:
    """
    Attach the current REPL session to an existing conversation
    and re-render its complete history.

    Fetches every item in the conversation (paginating past the
    server's per-request 100-item cap), threads new turns onto
    the last response_id, and renders the conversation in full
    using the same :class:`RichBlockFormatter` the live stream
    uses — so a resumed conversation looks identical to the
    transcript the user originally saw, with full tool-call
    args, result panels, reasoning panels, and untruncated
    assistant text.

    Used by:

    - The ``/switch <id>`` slash command (interactive switch
      mid-session) — passes ``redraw_screen=True`` because the
      previous conversation's transcript is visible above the
      input prompt and needs to be cleared before re-rendering
      the welcome banner + new conversation.
    - ``run_repl(resume_conversation_id=...)`` on startup
      (``--continue`` / ``--resume <id>``; see
      designs/RUN_OMNIGENT_SESSION_RESUMPTION.md) — passes
      ``redraw_screen=False`` because the welcome banner has
      already been drawn by ``run_repl`` and there's nothing
      else on screen to replace.

    Both call sites render the FULL conversation, not a tail —
    truncating to a "preview" lost too much context (tool args,
    result content, multi-turn reasoning) and surprised users
    coming back to long sessions.

    :param conversation_id: The conversation to attach to.
    :param session: The active REPL session (gets ``reset()``
        + ``resume_from_response()`` called on it).
    :param client: The :class:`OmnigentClient` for fetching
        items.
    :param host: The :class:`TerminalHost` to render against.
    :param fmt: The :class:`RichBlockFormatter` driving styling.
    :param ui_name: The display-formatted agent name shown in
        the welcome banner when *redraw_screen* is True.
        Callers compute this consistently with the initial
        banner — typically via :func:`_humanize_agent_name`.
    :param redraw_screen: When True, clear the screen and
        re-render the welcome banner before re-rendering the
        conversation. When False, only print the "Resumed
        conversation …" line — appropriate when the banner is
        already on screen.
    :raises Exception: Network / server errors propagate;
        callers render them as inline REPL output.
    """
    from rich.text import Text

    # Fail loud on a bad session id: _list_all_conversation_items
    # silently falls back to the legacy items endpoint (which returns
    # [] for missing conversations), hiding the 404 until first send.
    if hasattr(session, "session_id"):
        await client.sessions.get(conversation_id)

    # Eagerly bind THIS REPL's runner and start the SSE pump so
    # turns posted from the web UI / another client stream into the
    # local REPL right away — without this, they only surface after
    # the local user sends a message and triggers the lazy bind.
    # Idempotent: a later ``send()`` short-circuits in ``_ensure_session``.
    ensure = getattr(session, "_ensure_session", None)
    if callable(ensure):
        await ensure()

    items = await _list_all_conversation_items(client, conversation_id)

    last_response_id = None
    for item in reversed(items):
        rid = item.get("response_id")
        if isinstance(rid, str):
            last_response_id = rid
            break
    if last_response_id is None:
        # An empty conversation (no response items yet). On a fresh
        # `omnigent run` the daemon hands the REPL a freshly-created session
        # as the resume target, so this is the normal new-session case — the
        # old "Empty conversation." line was misleading noise at the top of
        # every new run. Render nothing extra on the startup path (the welcome
        # header is already on screen); on the interactive `/switch` path,
        # redraw the welcome header so the screen still reflects the switch.
        if redraw_screen:
            _clear_screen()
            host.output(fmt.welcome(ui_name, hints=WELCOME_HINTS))
        return
    session.reset()
    session.resume_from_response(last_response_id)
    if redraw_screen:
        _clear_screen()
        host.output(fmt.welcome(ui_name, hints=WELCOME_HINTS))
    host.output(
        Text.from_markup(
            f"  [{fmt.muted}]Resumed conversation {conversation_id[:16]}…[/{fmt.muted}]\n"
        )
    )

    # Pre-pass: build a call_id → tool metadata lookup so
    # ``function_call_output`` items (which only carry
    # ``call_id``, never ``name`` / ``arguments``) can be rendered
    # by the same pretty tool renderers as the live stream.
    call_id_to_tool_metadata = _build_call_id_to_tool_metadata_lookup(items)
    for item in items:
        _render_history_item(
            item,
            host,
            fmt,
            call_id_to_tool_metadata=call_id_to_tool_metadata,
        )

    # Seed the toolbar ring immediately on resume so it reflects the
    # existing context usage without waiting for the first idle event.
    # ``items`` is already fetched above — no extra API call needed.
    cw = getattr(session, "context_window", None)
    if cw:
        last_total = getattr(session, "_last_total_tokens", None)
        if last_total is not None:
            # Use the provider-reported total_tokens from the most
            # recent completed task. This includes system prompt +
            # tool schemas + messages, so it matches what the provider
            # will see as input_tokens on the next turn — far more
            # accurate than a local count_tokens() estimate.
            tokens = last_total
        else:
            from omnigent.runtime.compaction import count_tokens

            effective = _items_for_context_token_count(items)
            llm = getattr(session, "llm_model", None) or getattr(session, "_agent_name", "")
            tokens = count_tokens(
                [dict(i) for i in effective],  # type: ignore[arg-type]
                llm,
            )
        host.update_context_usage(tokens, cw)


@_cmd("/fork", "Fork the current conversation into a new session")
async def _cmd_fork(
    arg: str,
    session: Session,
    client: OmnigentClient,
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """Fork the current session into a new session with copied items.

    Creates a server-side fork via ``POST /v1/sessions/{id}/fork``,
    then switches the REPL to the fork **in-place** — no screen
    clear, no transcript repaint. The fork is an exact copy of the
    conversation up to this point, so there is nothing new to render.

    The original session id is printed so the user can recover it
    via ``/switch``.

    :param arg: User-supplied text after ``/fork``.  Treated as the
        fork's title when non-empty, e.g. ``"experiment-2"``.
    :param session: The active REPL session (must expose
        ``session_id`` for sessions-API mode).
    :param client: Agent-plane HTTP client used to call the fork
        endpoint.
    :param host: Terminal host for rendering output messages.
    :param fmt: Rich block formatter for consistent styling.
    """
    from rich.text import Text

    # Only supported in sessions-API mode.
    current_id = getattr(session, "session_id", None)
    if current_id is None:
        host.output(
            Text.from_markup(
                "  [bold red]/fork requires the sessions API (not available in legacy mode).[/]"
            )
        )
        return

    title = arg.strip() or None
    try:
        result = await client.sessions.fork(current_id, title=title)
    except Exception as exc:  # noqa: BLE001 — REPL UI boundary: render server errors inline
        host.output(Text.from_markup(f"  [bold red]Fork failed: {exc}[/]"))
        return

    new_id = result["id"]

    # Switch the session adapter to the fork in-place.
    switch_fn = getattr(session, "switch_session", None)
    if switch_fn is not None:
        switch_fn(new_id)

    host.output(
        Text.from_markup(
            f"  [{fmt.muted}]Conversation forked. "
            f"To return to the previous conversation, run /switch {current_id}[/{fmt.muted}]"
        )
    )


@_cmd("/history", "Show current conversation history")
async def _cmd_history(
    arg: str,  # noqa: ARG001 — dispatch-contract params
    session: Session,
    client: OmnigentClient,
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    from rich.text import Text

    # Sessions-API mode: the adapter exposes the durable
    # session_id (== conversation_id) directly. Skip the
    # responses.get round-trip entirely.
    sessions_api_conv_id = getattr(session, "session_id", None)
    if sessions_api_conv_id is not None:
        try:
            items = await _list_all_conversation_items(
                client,
                sessions_api_conv_id,
            )
            call_id_to_tool_metadata = _build_call_id_to_tool_metadata_lookup(items)
            for item in items:
                _render_history_item(
                    item,
                    host,
                    fmt,
                    call_id_to_tool_metadata=call_id_to_tool_metadata,
                )
        except Exception as exc:  # noqa: BLE001 — REPL UI boundary: surface server errors inline
            host.output(Text.from_markup(f"  [bold red]Error: {exc}[/]"))
        return

    if not session.current_response_id:
        host.output(Text.from_markup(f"  [{fmt.muted}]No active conversation.[/{fmt.muted}]"))
        return
    try:
        resp = await client.responses.get(session.current_response_id)
        if resp.conversation:
            items = await _list_all_conversation_items(client, resp.conversation.id)
            call_id_to_tool_metadata = _build_call_id_to_tool_metadata_lookup(items)
            for item in items:
                _render_history_item(
                    item,
                    host,
                    fmt,
                    call_id_to_tool_metadata=call_id_to_tool_metadata,
                )
        else:
            host.output(Text.from_markup(f"  [{fmt.muted}]No conversation.[/{fmt.muted}]"))
    except Exception as exc:  # noqa: BLE001 — REPL UI boundary: render network/server errors as inline text so the REPL stays responsive instead of crashing
        host.output(Text.from_markup(f"  [bold red]Error: {exc}[/]"))


# Coin bar rendering constants for /context.
# Mirrors _DEFAULT_TRIGGER_THRESHOLD from omnigent.runtime.compaction.
_CONTEXT_COMPACTION_TRIGGER: float = 0.8
_CONTEXT_COIN_TOTAL: int = 10  # bar width in positions
_CONTEXT_COIN_USED: str = "█"
_CONTEXT_COIN_FREE: str = "░"
_CONTEXT_COIN_BUF: str = "▓"


@dataclass
class _ContextItems:
    """
    Result of the conversation-item fetch for ``/context``.

    :param items: Conversation item dicts fetched from the server.
    :param error: User-facing error string if the fetch failed, or
        ``None`` on success.
    """

    items: list[dict[str, object]]
    error: str | None


async def _fetch_context_items(
    session: Session,
    client: OmnigentClient,
) -> _ContextItems:
    """
    Fetch conversation items for the current REPL session.

    Mirrors the fetch logic in :func:`_cmd_history`: tries the
    sessions-API path (``session_id``) first, then falls back to the
    legacy responses path (``current_response_id``).

    :param session: Current REPL session; exposes ``session_id`` and
        ``current_response_id`` for the two fetch paths.
    :param client: Agent-plane HTTP client used to query items.
    :returns: A :class:`_ContextItems` with populated ``items`` on
        success or a non-``None`` ``error`` string on failure.
    """
    sessions_api_conv_id = getattr(session, "session_id", None)
    if sessions_api_conv_id is not None:
        try:
            items = await _list_all_conversation_items(client, sessions_api_conv_id)
            return _ContextItems(items=items, error=None)
        except Exception as exc:  # noqa: BLE001 — REPL UI boundary: surface inline
            return _ContextItems(items=[], error=str(exc))

    if session.current_response_id:
        try:
            resp = await client.responses.get(session.current_response_id)
            if resp.conversation:
                items = await _list_all_conversation_items(client, resp.conversation.id)
                return _ContextItems(items=items, error=None)
        except Exception as exc:  # noqa: BLE001 — REPL UI boundary
            return _ContextItems(items=[], error=str(exc))

    return _ContextItems(items=[], error=None)


def _items_for_context_token_count(
    items: list[dict[str, object]],
) -> list[dict[str, object]]:
    """
    Return the effective prompt history represented by conversation items.

    Raw conversation storage keeps older items even after compaction and
    appends a metadata ``type=compaction`` item. Runtime prompt loading uses
    that metadata as a cursor and sends only a synthetic summary pair plus
    items after ``last_item_id``. ``/context`` should report that same
    effective prompt, not the raw archival transcript.

    :param items: Chronological conversation items from the server.
    :returns: Compaction-aware items suitable for token counting.
    """
    latest_compaction = next(
        (item for item in reversed(items) if item.get("type") == "compaction"),
        None,
    )
    if latest_compaction is None:
        return [item for item in items if item.get("type") not in {"resource_event"}]

    last_item_id = latest_compaction.get("last_item_id")
    summary = latest_compaction.get("summary")
    if not isinstance(last_item_id, str) or not isinstance(summary, str):
        return [item for item in items if item.get("type") not in {"compaction", "resource_event"}]

    boundary_index = -1
    for idx, item in enumerate(items):
        if item.get("id") == last_item_id:
            boundary_index = idx
            break
    recent_items = items[boundary_index + 1 :] if boundary_index >= 0 else []
    content_recent = [
        item for item in recent_items if item.get("type") not in {"compaction", "resource_event"}
    ]
    summary_items: list[dict[str, object]] = [
        {
            "type": "message",
            "role": "user",
            "content": (
                "[This is an automatically generated summary of the prior "
                "conversation context.]\n\n"
                "Please provide a summary of our conversation so far."
            ),
        },
        {
            "type": "message",
            "role": "assistant",
            "content": summary,
        },
    ]
    return summary_items + content_recent


async def _refresh_session_metadata(
    session: Session,
    client: OmnigentClient,
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """
    Re-sync client-side session metadata from a fresh server snapshot.

    Fired in the background from two triggers. The session's bound
    agent — and with it ``llm_model`` / ``harness`` /
    ``context_window`` / ``model_override`` — can change between turns
    when another client switches the agent in place
    (``POST /v1/sessions/{id}/switch-agent``). The server publishes a
    ``session.agent_changed`` stream event for the switch (the live
    trigger), but the event is transient SSE-only with no replay — one
    landing in a stream-pump reconnect gap or before this REPL attached
    is lost — so each turn start re-fires the refresh as the catch-up
    trigger. Both paths re-derive state from the snapshot rather than
    applying event payloads. When the agent name changed, the toolbar
    label and window title are updated and a muted notice line is
    rendered.

    :param session: Sessions-API adapter; must expose ``session_id``,
        ``model``, and ``_hydrate_from_session_snapshot``. Legacy
        sessions without those attributes are a no-op.
    :param client: Omnigent HTTP client used to fetch the snapshot.
    :param host: Terminal host whose toolbar label is updated.
    :param fmt: Active formatter; supplies the muted style for the
        switch notice.
    :returns: None.
    """
    session_id = getattr(session, "session_id", None)
    hydrate = getattr(session, "_hydrate_from_session_snapshot", None)
    if session_id is None or hydrate is None:
        return
    old_name = session.model
    try:
        snap = await client.sessions.get(session_id)
    except Exception:  # noqa: BLE001 — background refresh at the REPL UI boundary: a stale toolbar beats an unhandled-task traceback
        return
    hydrate(snap)
    new_name = session.model
    if new_name != old_name:
        host.set_model_name(_humanize_agent_name(new_name))
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]Agent switched: {_humanize_agent_name(old_name)} → "
                f"{_humanize_agent_name(new_name)}[/{fmt.muted}]"
            )
        )


async def _update_context_ring_estimate(
    session: Session,
    client: OmnigentClient,
    host: TerminalHost,
    context_window: int,
) -> None:
    """
    Update the toolbar context ring from a local token-count estimate.

    Fallback for turn-idle when the provider reported no usage (e.g.
    native harnesses). Fetches the conversation items, reduces them to
    the compaction-aware effective prompt, counts tokens, and pushes
    the result to the host's context ring. A failed item fetch leaves
    the ring untouched.

    :param session: Current REPL session; exposes ``llm_model`` (the
        spec-pinned LLM id, ``None`` for native harnesses) and
        ``model`` (the agent name, e.g. ``"claude-native-ui"``).
    :param client: Omnigent HTTP client used to query items.
    :param host: Terminal host whose context ring is updated.
    :param context_window: Context window size in tokens, e.g. ``200_000``.
    :returns: None.
    """
    from omnigent.runtime.compaction import count_tokens

    result = await _fetch_context_items(session, client)
    if result.error is not None:
        return
    effective = _items_for_context_token_count(result.items)
    # Fall back to the agent name when the spec pins no LLM model
    # (native-harness agents); count_tokens maps unknown names to
    # cl100k_base. Both values are read from the session at call
    # time, not captured at task-spawn time, so they stay current.
    llm = getattr(session, "llm_model", None) or session.model
    tokens = count_tokens(
        [dict(i) for i in effective],  # type: ignore[arg-type]
        llm,
    )
    host.update_context_usage(tokens, context_window)


def _render_context_tree(
    agent_name: str,
    model_override: str | None,
    message_tokens: int,
    context_window: int | None,
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """
    Build and emit the context-usage Rich tree to the terminal host.

    When ``context_window`` is ``None`` the tree shows a plain token
    count and an "unknown" hint. Otherwise it renders the coin bar plus
    a per-category breakdown.

    :param agent_name: Agent's wire name, e.g. ``"my-agent"``.
    :param model_override: User-set LLM model identifier,
        e.g. ``"openai/gpt-4o"``, or ``None`` when using the agent default.
    :param message_tokens: Estimated token count for conversation messages,
        e.g. ``35_000``.
    :param context_window: Model's context window in tokens, or ``None``
        if unknown, e.g. ``200_000``.
    :param host: Terminal host to emit the tree to.
    :param fmt: Formatter supplying REPL colour names.
    """
    from rich.text import Text
    from rich.tree import Tree

    display_name = _humanize_agent_name(agent_name)
    if model_override:
        header_label = (
            f"[{fmt.accent}]{display_name}[/{fmt.accent}]"
            f" [{fmt.muted}]({model_override})[/{fmt.muted}]"
        )
    else:
        header_label = f"[{fmt.accent}]{display_name}[/{fmt.accent}]"

    tree = Tree(Text.from_markup(f"Context Usage · {header_label}"))

    if context_window is None:
        tree.add(
            Text.from_markup(
                f"[{fmt.muted}]Context window size unknown — "
                f"will be detected on first overflow[/{fmt.muted}]"
            )
        )
        tree.add(
            Text.from_markup(
                f"[{fmt.accent}]Messages[/{fmt.accent}]"
                f"  [{fmt.muted}]{message_tokens:,} tokens[/{fmt.muted}]"
            )
        )
        host.output(tree)
        return

    used_frac = min(message_tokens / context_window, 1.0)
    buf_frac = 1.0 - _CONTEXT_COMPACTION_TRIGGER  # 0.20
    used_coins = round(used_frac * _CONTEXT_COIN_TOTAL)
    buf_coins = round(buf_frac * _CONTEXT_COIN_TOTAL)
    # Free zone sits between used and buffer; clamp to zero if used is large.
    free_coins = max(_CONTEXT_COIN_TOTAL - used_coins - buf_coins, 0)
    # Absorb overflow into buffer when used spills past the trigger threshold.
    buf_coins = _CONTEXT_COIN_TOTAL - used_coins - free_coins
    coin_bar = (
        _CONTEXT_COIN_USED * used_coins
        + _CONTEXT_COIN_FREE * free_coins
        + _CONTEXT_COIN_BUF * buf_coins
    )

    buf_tokens = int(context_window * buf_frac)
    # Free space excludes the compaction buffer so the three rows partition the
    # window (Messages + Free + Buffer = window) and each row's token count
    # agrees with its own percentage. (Previously free omitted the buffer, so it
    # read e.g. "920,150 tokens (72%)" — a count that is 92% of the window.)
    free_tokens = max(context_window - message_tokens - buf_tokens, 0)
    used_pct = used_frac * 100.0

    tree.add(
        Text.from_markup(
            f"{coin_bar}  "
            f"[{fmt.accent}]{message_tokens / 1000:.1f}k[/{fmt.accent}]"
            f" [{fmt.muted}]/ {context_window // 1000}k tokens ({used_pct:.0f}%)[/{fmt.muted}]"
        )
    )
    tree.add(
        Text.from_markup(
            f"{_CONTEXT_COIN_USED} [{fmt.accent}]Messages[/{fmt.accent}]"
            f"  [{fmt.muted}]{message_tokens:,} tokens ({used_pct:.0f}%)[/{fmt.muted}]"
        )
    )
    tree.add(
        Text.from_markup(
            f"{_CONTEXT_COIN_FREE} [{fmt.muted}]Free space[/{fmt.muted}]"
            f"  [{fmt.muted}]{free_tokens:,} tokens"
            f" ({max(0.0, (1.0 - used_frac - buf_frac)) * 100:.0f}%)[/{fmt.muted}]"
        )
    )
    tree.add(
        Text.from_markup(
            f"{_CONTEXT_COIN_BUF} [{fmt.muted}]Compaction buffer[/{fmt.muted}]"
            f"  [{fmt.muted}]{buf_tokens:,} tokens ({buf_frac * 100:.0f}%)[/{fmt.muted}]"
        )
    )
    host.output(tree)


@_cmd("/compact", "Compact conversation context now")
async def _cmd_compact(
    arg: str,  # noqa: ARG001 — dispatch-contract params
    session: Session,
    client: OmnigentClient,  # noqa: ARG001 — dispatch-contract params
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """Request proactive context compaction for the current conversation."""
    from rich.text import Text

    if session.is_streaming:
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]Cannot compact while a response is running; "
                f"use /cancel or wait for it to finish.[/{fmt.muted}]"
            )
        )
        return
    compact = getattr(session, "compact", None)
    if not callable(compact):
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]This connection does not support /compact.[/{fmt.muted}]"
            )
        )
        return
    # Progress messages ("Compacting…" / "Compaction complete.") arrive
    # via the session SSE stream as response.compaction.in_progress /
    # response.compaction.completed events, so we don't output them here
    # — doing so would duplicate them for explicit /compact calls.
    try:
        result = compact()
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001 — REPL boundary: keep prompt alive
        host.output(Text.from_markup(f"  [bold red]Compaction failed: {exc}[/]"))


@_cmd("/context", "Show context window usage")
async def _cmd_context(
    arg: str,  # noqa: ARG001 — dispatch-contract params
    session: Session,
    client: OmnigentClient,
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """
    Display context window usage for the current conversation.

    Delegates item fetching to :func:`_fetch_context_items` and
    rendering to :func:`_render_context_tree`. Falls back gracefully
    when the context window size is unknown (custom model or first turn
    before any overflow has been observed).

    :param arg: Ignored (dispatch-contract filler), e.g. ``""``.
    :param session: Current REPL session; provides agent name and
        optional model override.
    :param client: Agent-plane HTTP client used to fetch conversation
        items for token counting.
    :param host: Terminal host used to render the output.
    :param fmt: Formatter supplying the REPL colour names.
    """
    from rich.text import Text

    from omnigent.runtime.compaction import count_tokens

    agent_name = session.model
    # /model override wins; fall back to the spec model. ``is not None``
    # so an empty-string override doesn't silently fall through.
    _override = getattr(session, "model_override", None)
    llm_model: str | None = (
        _override if _override is not None else getattr(session, "llm_model", None)
    )

    # context_window is pre-computed server-side (litellm lookup) and
    # returned in SessionResponse — avoids requiring litellm client-side.
    context_window: int | None = getattr(session, "context_window", None)

    # Use the provider-reported token count from the most recently
    # completed response when available — it includes the system prompt,
    # tool schemas, and all messages, so it matches what the provider
    # will see as input_tokens on the next turn. Fall back to a local
    # count_tokens() estimate only when no response has completed yet
    # (e.g. before the very first turn in a fresh session).
    live_tokens: int | None = getattr(host, "tokens_used", None)
    if live_tokens is not None:
        message_tokens = live_tokens
    else:
        result = await _fetch_context_items(session, client)
        if result.error is not None:
            host.output(Text.from_markup(f"  [bold red]Error fetching history: {result.error}[/]"))
            return
        # count_tokens falls back to cl100k_base when agent_name isn't a
        # recognised LLM identifier — good enough for an estimate.
        effective_items = _items_for_context_token_count(result.items)
        message_tokens = count_tokens(
            [dict(item) for item in effective_items],  # type: ignore[arg-type]
            llm_model or agent_name,
        )
    _render_context_tree(agent_name, llm_model, message_tokens, context_window, host, fmt)


@_cmd("/cancel", "Cancel the current response")
async def _cmd_cancel(
    arg: str,  # noqa: ARG001 — dispatch-contract params
    session: Session,
    client: OmnigentClient,  # noqa: ARG001
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    from rich.text import Text

    resp = await session.cancel()
    if resp:
        host.output(Text.from_markup(f"  [{fmt.warning}]Cancelled {resp.id}[/{fmt.warning}]"))


def _build_github_issue_url(
    session_id: str | None,
    agent_name: str,
    description: str,
    version: str | None = None,
    os_info: str | None = None,
) -> str:
    """Build a pre-filled GitHub new-issue URL for bug reports."""
    import datetime
    from urllib.parse import quote

    session_line = f"`{session_id}`" if session_id else "not started"
    timestamp = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    what_happened = description if description else "<!-- Describe what went wrong -->"

    info_lines = [
        f"- **Session ID:** {session_line}",
        f"- **Agent:** {agent_name}",
    ]
    if version:
        info_lines.append(f"- **Version:** {version}")
    if os_info:
        info_lines.append(f"- **OS:** {os_info}")
    info_lines.append(f"- **Timestamp:** {timestamp}")

    body_parts: list[str] = [
        "<!-- Filed via /report in the TUI -->",
        "",
        "## Session Info",
        "",
        *info_lines,
        "",
        "## What happened",
        "",
        what_happened,
        "",
        "## Expected behavior",
        "",
        "<!-- What did you expect? -->",
        "",
        "## Screenshots",
        "",
        "<!-- Paste a screenshot here (GitHub uploads automatically on paste) -->",
        "",
        "## Console / terminal output",
        "",
        "<!-- Scroll up in your terminal for error output, or run with",
        "     --debug-events for a JSONL event log in ~/.omnigent/debug/ -->",
    ]

    body = "\n".join(body_parts)
    base = "https://github.com/omnigent-ai/omnigent/issues/new"
    return (
        f"{base}"
        f"?title={quote('[Bug] TUI issue')}"
        f"&body={quote(body)}"
        f"&labels={quote('bug,area/harnesses')}"
    )


@_cmd("/logs", "Collect current session logs into a zip")
async def _cmd_logs(
    arg: str,
    session: Session,
    client: OmnigentClient,
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """Create a zip bundle containing logs for the active REPL session."""
    from rich.text import Text

    from omnigent.cli_diagnostics import current_cli_log_path
    from omnigent.repl._session_log import write_logs_zip, write_session_log

    output_path = pathlib.Path(arg).expanduser() if arg.strip() else None
    session_id: str | None = getattr(session, "session_id", None)
    log_paths: list[pathlib.Path] = []

    # Always-on CLI diagnostics are per process/invocation, so the
    # current path is session-scoped. Do not glob the whole logs dir.
    cli_log = current_cli_log_path()
    if cli_log is not None:
        log_paths.append(cli_log)

    # These attributes are attached by run_repl for local diagnostics
    # that are specific to this REPL invocation/session.
    for attr in ("_event_log_path", "_server_log_path", "_runner_log_path"):
        value = getattr(session, attr, None)
        if isinstance(value, pathlib.Path):
            log_paths.append(value)

    # Include a fresh JSON transcript of the active session. This is
    # the only file we create here; all other entries are explicit
    # per-invocation paths. A fresh REPL with no turns has no session
    # id yet, so it simply omits the transcript instead of sweeping
    # unrelated old conversation logs.
    if session_id:
        try:
            transcript = await write_session_log(
                client,
                session_id,
                agent_name=session.model,
                log_dir=None,
            )
            log_paths.append(transcript)
        except Exception as exc:  # noqa: BLE001 — slash-command UI boundary
            _log.exception("Session transcript write failed for /logs")
            host.output(
                Text.from_markup(
                    f"  [{fmt.muted}]Could not write session transcript "
                    f"({type(exc).__name__}: {exc}); bundling available logs.[/{fmt.muted}]"
                )
            )

    path, count = write_logs_zip(output_path, log_paths=log_paths, session_id=session_id)
    conversation_label = session_id or "(none yet)"
    host.output(
        Text.from_markup(
            f"  [{fmt.muted}]Collected {count} current-session log file"
            f"{'s' if count != 1 else ''} into {path}\n"
            f"  Conversation ID: {conversation_label}[/{fmt.muted}]"
        )
    )


@_cmd("/report", "Open a pre-filled GitHub issue for this session")
async def _cmd_report(
    arg: str,
    session: Session,
    client: OmnigentClient,  # noqa: ARG001 — dispatch-contract param
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """Open a GitHub issue pre-filled with the current session context."""
    import platform
    import webbrowser
    from importlib.metadata import version as _pkg_version

    from rich.text import Text

    session_id: str | None = session.session_id if hasattr(session, "session_id") else None

    try:
        version = _pkg_version("omnigent")
    except Exception:  # noqa: BLE001
        version = None

    os_info = f"{platform.system()} {platform.release()}".strip() or None

    url = _build_github_issue_url(
        session_id=session_id,
        agent_name=session.model,
        description=arg,
        version=version,
        os_info=os_info,
    )
    opened = webbrowser.open(url)
    if opened:
        host.output(
            Text.from_markup(f"  [{fmt.muted}]Opening GitHub issue in browser…[/{fmt.muted}]")
        )
    else:
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]Could not open browser. "
                f"Copy this URL to file an issue:[/{fmt.muted}]"
            )
        )
        host.output(Text(f"  {url}"))


@_cmd("/quit", "Exit")
async def _cmd_quit(
    arg: str,  # noqa: ARG001 — dispatch-contract params
    session: Session,  # noqa: ARG001
    client: OmnigentClient,  # noqa: ARG001
    host: TerminalHost,
    fmt: RichBlockFormatter,  # noqa: ARG001
) -> None:
    host.request_exit()


COMMANDS["/exit"] = COMMANDS["/quit"]


async def _list_all_conversation_items(
    client: OmnigentClient,
    conv_id: str,
) -> list[dict[str, object]]:
    """
    Fetch every item in *conv_id*, paginating past the
    server's per-request 100-item cap via
    ``GET /v1/sessions/{id}/items``.

    :param client: Agent-plane HTTP client.
    :param conv_id: Session to enumerate, e.g.
        ``"conv_abc123"``.
    :returns: All items in chronological order. Empty when the
        session has no items or every page errored.
    """
    all_items: list[dict[str, object]] = []
    page_size = _LIST_ITEMS_PAGE_SIZE
    after: str | None = None
    while True:
        try:
            raw_page = await client.sessions.list_items(
                conv_id,
                limit=page_size,
                after=after,
                order="asc",
            )
        except Exception:  # noqa: BLE001 — overlay builder: any per-page error falls back to whatever was already fetched; partial sidebar beats no sidebar
            break
        page: list[dict[str, object]] = list(raw_page) if raw_page else []
        if not page:
            break
        all_items.extend(page)
        if len(page) < page_size:
            break
        last_item = page[-1]
        last_id = last_item.get("id") if isinstance(last_item, dict) else None
        if not isinstance(last_id, str):
            break
        after = last_id
    return all_items


def _should_discover_subagents(
    root_id: str | None,
    *,
    has_active_subagents: bool,
    observing_subagent: bool,
    last_polled_root: str | None,
) -> bool:
    """Decide whether the background loop should (re-)fetch the sub-agent tree.

    Re-fetches while there is live work to track, AND runs a one-shot discovery
    when the ROOT just changed. Concretely, polls when:

    * ``has_active_subagents`` — a sub-agent anywhere in the tree is still
      running, so its (and any grandchild's) status keeps changing; or
    * ``observing_subagent`` — the user has dived into a child (read-only or
      co-driving). The active stream is then the child's own, which carries the
      child's events but NOT a ``session.child_session.updated`` about itself,
      so the poll is the only thing that keeps the dived-into child's row (and
      the badge) fresh while chatting; or
    * ``last_polled_root != root_id`` — the root just changed: a one-shot
      discovery that repopulates the selector for a resumed / ``/switch``-ed
      session that already has children (no fresh SSE to seed them).

    Deliberately keyed on *active* work, NOT merely "any node exists": finished
    sub-agents are retained in the selector indefinitely (web parity), but a
    terminal child's status no longer changes, so polling it forever is pure
    waste. Once everything settles at the top level the loop goes quiet; a child
    that later resumes does so on the active (root) stream, whose SSE
    ``session.child_session.updated`` re-arms ``has_active_subagents`` and the
    poll wakes again.

    :param root_id: The current tree root (top-level session id), or ``None``.
    :param has_active_subagents: Whether any sub-agent is still running.
    :param observing_subagent: Whether the user is currently viewing/co-driving
        a child (so the parent-rooted poll is what keeps that child fresh).
    :param last_polled_root: The root the loop last ran discovery for.
    :returns: ``True`` to fetch the tree this tick.
    """
    if root_id is None:
        return False
    return has_active_subagents or observing_subagent or last_polled_root != root_id


def _apply_child_session_event(
    event: object,
    *,
    active_conversation_id: str | None,
    host: TerminalHost,
) -> bool:
    """Apply a child-session SSE event to the host's sub-agent registry.

    Handles ``session.created`` (register a launching child) and
    ``session.child_session.updated`` (merge the partial summary), but only
    when the event's carrier ``conversation_id`` is the active session — so a
    relayed grandchild event riding an ancestor stream doesn't get attached
    to the wrong parent. Deeper tree levels are populated by the recursive
    ``child_sessions`` poll (:func:`_refresh_subagent_tree`), not these events.

    :param event: The decoded SSE event from the stream pump.
    :param active_conversation_id: The session the REPL is currently
        streaming, used both as the filter and as the new child's parent.
    :param host: The :class:`TerminalHost` whose registry is mutated.
    :returns: ``True`` if *event* was a child-session event (so the caller
        stops dispatching it), ``False`` otherwise.
    """
    from omnigent.server.schemas import (
        SessionChildSessionUpdatedEvent as _ChildUpdated,
    )
    from omnigent.server.schemas import (
        SessionCreatedEvent as _ChildCreated,
    )

    if isinstance(event, _ChildCreated):
        if event.conversation_id == active_conversation_id:
            host.upsert_subagent(
                event.child_session_id,
                parent_id=active_conversation_id,
                child={"current_task_status": "launching"},
            )
        return True
    if isinstance(event, _ChildUpdated):
        if event.conversation_id == active_conversation_id:
            host.upsert_subagent(
                event.child_session_id,
                parent_id=active_conversation_id,
                child=event.child,
            )
        return True
    return False


async def _refresh_subagent_tree(
    client: OmnigentClient,
    host: TerminalHost,
    root_id: str,
    *,
    max_depth: int = _MAX_SUBAGENT_TREE_DEPTH,
    generation: int | None = None,
) -> None:
    """Recursively fetch the sub-agent tree under *root_id* and push it into
    the host registry.

    Delegates the recursion to :meth:`SessionsNamespace.child_sessions_tree`
    (the same helper the SDK ``subtree_busy`` rollup uses), which walks
    ``GET …/child_sessions`` breadth-first capped at ``MAX_TREE_DEPTH`` and tags
    each row with the parent it was queried under so the host can reconstruct
    the hierarchy. The SSE stream only delivers the active session's direct
    children, so this poll is what keeps grandchildren live. A failed fetch is
    swallowed, leaving the prior tree in place rather than crashing the REPL.

    :param generation: :attr:`TerminalHost.subagent_generation` captured before
        the fetch began. Passed through to :meth:`TerminalHost.seed_subagent_tree`
        so a snapshot whose tree was cleared (``/switch`` / ``/new`` / ``/clear``)
        mid-fetch is dropped instead of resurrecting the cleared nodes.
    """
    try:
        # Recursion + parent_id tagging now live in the shared SDK helper so the
        # CLI tree and the SDK rollup (subtree_busy) walk identical data.
        nodes = await client.sessions.child_sessions_tree(root_id, max_depth=max_depth)
    except Exception:  # noqa: BLE001 — best-effort poll: a failed fetch leaves the prior tree in place rather than crashing the REPL
        return
    host.seed_subagent_tree(root_id, nodes, generation=generation)


async def _collect_overview_targets(
    client: OmnigentClient,
    session: Session,
) -> list[OverlayTarget]:
    """
    Enumerate the debug overview's sidebar targets.

    Always yields a ``main`` entry bound to the current chat's
    conversation. Additionally walks that conversation's items for
    ``function_call_output`` results from ``sys_session_send`` /
    ``sys_session_send`` (continuation path) — the tool outputs include a persistent
    ``conversation_id`` plus ``type`` + ``name`` for every
    sub-agent handle, so we can assemble a sidebar row per
    sub-agent without needing a separate server endpoint. The
    parent-conversation walk tolerates malformed outputs (missing
    fields, non-JSON strings, non-sub-agent tool outputs) by
    skipping them, so unrelated tool calls don't leak into the
    sidebar.

    :param client: Agent-plane HTTP client used for the items
        fetch.
    :param session: REPL session — only
        ``current_response_id`` is consulted; everything else is
        derived from the server response.
    :returns: A list of :class:`OverlayTarget` entries, always
        starting with ``main``. Empty list is never returned;
        when no conversation exists yet, the main target is
        still present with a synthetic placeholder key so the
        sidebar renders correctly from first paint.
    """
    targets: list[OverlayTarget] = [OverlayTarget(key="main", label="main", icon="🤖")]

    # Sessions-API path: read session_id off the adapter rather
    # than round-tripping through responses.get.
    sessions_api_conv_id: str | None = getattr(session, "session_id", None)
    if sessions_api_conv_id is not None:
        conv_id: str | None = sessions_api_conv_id
    else:
        if not session.current_response_id:
            return targets
        try:
            resp = await client.responses.get(session.current_response_id)
            conv_id = resp.conversation.id if resp.conversation else None
        except Exception:  # noqa: BLE001 — overlay builder: any network/server error falls back to the base targets list; the overlay must open even under partial failure
            return targets

    if conv_id is None:
        return targets

    # Store the conversation id on the main target's key so the
    # content builder can fetch the right conversation's items.
    # Recreate rather than mutate — dataclasses are frozen-ish in
    # intent even when the runtime allows assignment.
    targets[0] = OverlayTarget(key=conv_id, label="main", icon="🤖")

    # Paginate past the server's per-request 100-item cap so
    # long sessions (>100 items) still surface every terminal +
    # sub-agent. Without pagination, the user-reported 2026-04-30
    # symptom returns: 17 of 20 terminals visible because the
    # 18th-20th launch outputs landed past position 99.
    try:
        items: list[dict[str, object]] = await _list_all_conversation_items(
            client,
            conv_id,
        )
    except Exception:  # noqa: BLE001 — overlay builder: any network/server error falls back to the base targets list; the overlay must open even under partial failure
        return targets

    # Dedupe by conversation_id — repeated sys_session_send calls (continuation path)
    # to the same handle would otherwise emit duplicate rows. Walk
    # in chronological order (list_items returns oldest-first) so
    # the sidebar order reflects spawn order.
    sub_agent_conv_ids: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item.get("type") != "function_call_output":
            continue
        raw = item.get("output")
        if not isinstance(raw, str):
            continue
        payload = _parse_sub_agent_handle(raw)
        if payload is None:
            continue
        sub_conv = payload.get("conversation_id")
        if not isinstance(sub_conv, str) or sub_conv in seen:
            continue
        sa_agent = payload.get("agent")
        sa_title = payload.get("title")
        if not isinstance(sa_agent, str) or not isinstance(sa_title, str):
            # Malformed payload — skip rather than emit a "?:?"
            # sidebar row that would claim a sub-agent exists
            # but render with no useful identity. If the spawn
            # tool ever ships a ``kind: sub_agent`` output
            # without ``agent`` + ``title``, the sidebar going
            # silent is a clearer signal than a row full of
            # question marks.
            continue
        seen.add(sub_conv)
        sub_agent_conv_ids.append(sub_conv)
        targets.append(
            OverlayTarget(
                key=sub_conv,
                label=f"{sa_agent}:{sa_title}",
                icon="👾",
            ),
        )

    # Terminals — inferred from the agent's tool history rather
    # than fetched from a server-side registry. The legacy
    # non-AP path read ``Session._terminal_instances``
    # directly; without an HTTP endpoint mirroring that, we
    # reconstruct the live set from persisted
    # ``sys_terminal_launch`` / ``sys_terminal_close`` outputs
    # in each conversation's items. Trade-off: a terminal whose
    # process crashed outside the agent's tool surface still
    # appears here. Acceptable for an MVP — see the design
    # discussion in the Layer-1 plan for the supervision gap.
    terminals = await _collect_terminals_for_conversations(
        client,
        [conv_id, *sub_agent_conv_ids],
        seed_items={conv_id: items},
    )
    for info in terminals:
        # ``💻`` (U+1F4BB PERSONAL COMPUTER, "laptop") is
        # wcswidth-wide (2 cells) AND reads as a computer
        # — keeps the visual category consistent with the
        # F20 overview pane the legacy CLI had. Avoid the
        # otherwise-tempting ``🖥`` (U+1F5A5 DESKTOP COMPUTER):
        # Unicode classifies it East-Asian-Width Neutral
        # (wcswidth=1) but every terminal we ship to renders
        # it as 2 cells. wcswidth is the source of truth for
        # both the sidebar's padding AND prompt-toolkit's
        # :class:`Window` containing us, so a wcswidth ↔ render
        # mismatch can't be compensated for from inside the
        # host: it has to be avoided at the icon-pick step.
        # ``💻`` / ``🤖`` / ``👾`` all read 2 cells in both
        # wcswidth and the terminal, so rows stay aligned.
        targets.append(
            OverlayTarget(
                key=_terminal_target_key(info),
                label=f"{info.name}:{info.session}",
                icon="💻",
            ),
        )

    return targets


async def _collect_terminals_for_conversations(
    client: OmnigentClient,
    conv_ids: list[str],
    *,
    seed_items: dict[str, list[dict[str, object]]] | None = None,
) -> list[_TerminalInfo]:
    """
    Walk every named conversation's items and aggregate live terminals.

    Fetches each ``conv_id``'s items in parallel and runs
    :func:`_reconstruct_terminals_from_items` over each, then
    flattens. Sub-agents own their own conversation (via
    ``sys_session_send``) and can launch terminals there, so
    surfacing terminals across sub-agents AND the main
    conversation is what makes "I supervise a fleet of
    sub-agents each with terminals" actually visible in the
    overlay.

    :param client: The omnigent HTTP client.
    :param conv_ids: Conversations to walk. Order is preserved
        in the output so the sidebar lists main-conversation
        terminals before sub-agent ones.
    :param seed_items: Optional pre-fetched items per
        conversation, used to skip a redundant round-trip
        when the caller already has the main conversation's
        items in hand.
    :returns: All live terminals across the named
        conversations, in (conversation order, launch order)
        priority.
    """
    seed = seed_items or {}

    async def fetch_items(cid: str) -> tuple[str, list[dict[str, object]]]:
        if cid in seed:
            return cid, seed[cid]
        # Paginate past the per-request 100-item cap — same
        # reason as the parent-conversation fetch above. A
        # sub-agent conversation that ran 50+ tool calls
        # would otherwise hide its later terminals from the
        # sidebar.
        return cid, await _list_all_conversation_items(
            client,
            cid,
        )

    results = await asyncio.gather(*(fetch_items(cid) for cid in conv_ids))

    flat: list[_TerminalInfo] = []
    for cid, items in results:
        flat.extend(_reconstruct_terminals_from_items(items, conv_id=cid))
    return flat


# Sentinel prefix the overlay uses to distinguish terminal
# sidebar targets from main / sub-agent targets — both of those
# encode a real conversation_id in ``OverlayTarget.key`` and the
# builder uses that id to fetch items. Terminals have no
# conversation_id of their own; the prefix tells the builder to
# decode the rest of the key as ``conv_id::name::session`` and
# re-walk the owning conversation's items to find the
# terminal's socket. ``::`` is unused in real conversation ids
# (``conv_<hex>``), terminal names, or session keys, so it
# splits cleanly.
_TERMINAL_KEY_PREFIX = "terminal::"


@dataclass(frozen=True)
class _TerminalInfo:
    """
    Inferred-live terminal reconstructed from conversation items.

    Walking ``sys_terminal_launch`` / ``sys_terminal_close``
    function-call-output pairs gives us the terminal's identity
    plus the tmux coordinates needed to construct an attach
    command. State here is "last-known per the agent's tool
    calls" — a process that crashed outside the agent's tool
    surface still appears here, the same way it would on the
    legacy in-memory ``Session._terminal_instances`` dict
    until the next ``capture-pane`` failure cleared it.

    :param name: Terminal name from the spec /
        ``sys_terminal_launch`` arg, e.g. ``"bash"``.
    :param session: Session key the launch call passed,
        e.g. ``"s1"``. Multiple sessions per terminal name are
        independent tmux sessions of the same configured
        terminal.
    :param socket: Tmux socket path from the launch output —
        the ``-S`` arg an attach command needs.
    :param target: Tmux target. Always ``"main"`` per the
        :class:`TerminalInstance.tmux_target` constant
        (``omnigent/inner/terminal.py:167``); kept on the
        struct for forward compatibility if that constant ever
        becomes per-terminal.
    :param conv_id: The conversation that owns this terminal
        — main agent's conversation for parent-spawned
        terminals, sub-agent's conversation for sub-agent-
        spawned ones. The overlay uses it to label which
        agent the terminal belongs to.
    """

    name: str
    session: str
    socket: str
    target: str
    conv_id: str


def _terminal_target_key(info: _TerminalInfo) -> str:
    """
    Encode a :class:`_TerminalInfo` as an :class:`OverlayTarget`
    key the content builder can decode.

    Format: ``"terminal::<conv_id>::<name>::<session>"``. Socket
    + target are NOT encoded — the builder re-walks the owning
    conversation's items on selection to recover them, keeping
    the key short and the source of truth (the persisted
    function_call_output) authoritative.

    :param info: The terminal info to encode.
    :returns: An opaque key string.
    """
    return f"{_TERMINAL_KEY_PREFIX}{info.conv_id}::{info.name}::{info.session}"


def _decode_terminal_target_key(key: str) -> tuple[str, str, str] | None:
    """
    Reverse :func:`_terminal_target_key`.

    :param key: A target key, possibly a terminal key.
    :returns: ``(conv_id, name, session)`` if *key* is a
        terminal key, ``None`` otherwise — non-terminal keys
        let the caller fall through to the main / sub-agent
        rendering paths.
    """
    if not key.startswith(_TERMINAL_KEY_PREFIX):
        return None
    rest = key[len(_TERMINAL_KEY_PREFIX) :]
    parts = rest.split("::", 2)
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def _parse_terminal_tool_output(raw: object) -> dict[str, object] | None:
    """
    Decode a ``sys_terminal_launch`` / ``sys_terminal_close``
    function-call-output payload.

    Mirrors :func:`_parse_sub_agent_handle`'s tolerance for the
    two on-the-wire shapes the workflow persists: raw JSON
    string (default executor, omnigent builtins) and
    MCP-content-parts wrapper (claude-sdk harness). Returning
    ``None`` for anything else lets the reconstructor's loop
    skip cleanly.

    :param raw: The ``function_call_output.output`` value as
        persisted — a string in both cases.
    :returns: The decoded payload dict, or ``None`` when *raw*
        doesn't look like a terminal-tool output.
    """
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        # MCP content-parts wrapper. ``sys_terminal_*`` tools
        # emit one ``text`` part with the JSON envelope inside;
        # walk parts and decode the first match. Same pattern
        # as :func:`_parse_sub_agent_handle`.
        for part in payload:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            try:
                inner = json.loads(text)
            except (ValueError, TypeError):
                continue
            if isinstance(inner, dict):
                return inner
    return None


def _reconstruct_terminals_from_items(
    items: list[dict[str, object]],
    *,
    conv_id: str,
) -> list[_TerminalInfo]:
    """
    Walk function-call/output pairs to infer the live terminal set.

    Replays the conversation's tool history in chronological
    order: each ``sys_terminal_launch`` whose paired output
    includes a ``tmux_socket`` adds an entry; each
    ``sys_terminal_close`` whose paired output reports
    ``status: "closed"`` removes it. The remaining map is the
    set of terminals the agent currently believes are live.

    Failed launches (output has ``error`` field) and
    ``not_found`` closes are ignored. Errors during JSON
    decode skip the item rather than crash — the inferred view
    is best-effort, and one malformed output mustn't kill the
    sidebar.

    :param items: Conversation items in chronological order
        (caller fetched with default ``order="asc"``). Each
        item is the API-shape dict returned by
        ``client.sessions.list_items``.
    :param conv_id: The conversation these items belong to;
        recorded on every emitted :class:`_TerminalInfo` so
        the overlay can label terminals by their owning
        conversation (parent vs. sub-agent).
    :returns: A list of currently-live terminals in launch
        order. Terminals that were launched then closed
        within *items* don't appear.
    """
    live: dict[tuple[str, str], _TerminalInfo] = {}
    pending_calls: dict[str, str] = {}
    for item in items:
        item_type = item.get("type")
        if item_type == "function_call":
            call_id = item.get("call_id")
            tool_name = item.get("name")
            if isinstance(call_id, str) and isinstance(tool_name, str):
                pending_calls[call_id] = tool_name
            continue
        if item_type != "function_call_output":
            continue
        call_id = item.get("call_id")
        if not isinstance(call_id, str):
            continue
        tool_name = pending_calls.pop(call_id, None)
        if tool_name not in {"sys_terminal_launch", "sys_terminal_close"}:
            continue
        payload = _parse_terminal_tool_output(item.get("output"))
        if payload is None or "error" in payload:
            continue
        terminal_name = payload.get("terminal")
        session_key = payload.get("session")
        if not isinstance(terminal_name, str) or not isinstance(session_key, str):
            continue
        key = (terminal_name, session_key)
        if tool_name == "sys_terminal_launch":
            socket = payload.get("tmux_socket")
            if not isinstance(socket, str):
                continue
            # ``status`` may be "launched" (fresh) or
            # "already_running" (idempotent re-launch — same
            # tmux socket); both mean live, both produce one
            # entry under ``key``. Re-launches overwrite which
            # is fine since the key is identical.
            live[key] = _TerminalInfo(
                name=terminal_name,
                session=session_key,
                socket=socket,
                target="main",
                conv_id=conv_id,
            )
        elif tool_name == "sys_terminal_close":
            if payload.get("status") == "closed":
                live.pop(key, None)
            # ``not_found`` closes are no-ops — the LLM tried
            # to close something that wasn't actually live, no
            # state change in our reconstructed map either.
    return list(live.values())


async def _open_terminal_in_tmux(
    target: OverlayTarget,
    *,
    client: OmnigentClient,
    read_only: bool,
) -> None:
    """
    Spawn a fresh tmux window that attaches to *target*'s tmux session.

    Bound on the Ctrl+O overlay's ``O`` (attach) and ``R``
    (attach read-only) keybindings. Mirrors the legacy
    non-AP mode F20-overlay shortcuts at
    ``omnigent/inner/cli.py::_open_current_terminal_window``
    so users with muscle memory from the legacy CLI see the
    same behavior under Omnigent mode.

    Four guards short-circuit cleanly without raising — the
    overlay's exception swallow at the host catches anything
    else, but these four are the common failure modes worth
    surfacing as a clear stderr message:

    1. The selected target isn't a terminal (e.g. user pressed
       ``O`` on the ``main`` row). No-op.
    2. Not running inside tmux (``$TMUX`` unset). The
       ``tmux new-window`` command needs a current session to
       attach the new window to, so this would fail. Print a
       hint instead.
    3. The terminal vanished from the conversation between
       sidebar build and key press. Print a "no longer live"
       message.
    4. The agent's tmux session is dead at runtime (user
       previously attached and exited the bash shell, killing
       the pane → window → session → tmux server). The walker
       still reports the terminal as live because no
       ``sys_terminal_close`` was recorded — the agent didn't
       initiate the teardown. Without this guard, the second
       ``O`` press silently fails: ``tmux new-window`` opens a
       window whose ``tmux attach`` immediately errors and
       closes, looking like a dead key. ``tmux has-session``
       against the recovered socket catches it and prints a
       hint that the agent needs to relaunch.

    On success, the user's tmux client gets a new window
    showing the agent's tmux session. ``-r`` flag adds
    read-only mode; without it both the user and the agent
    can type into the same pane.

    :param target: The selected :class:`OverlayTarget`.
    :param client: Omnigent HTTP client — used to re-walk the
        owning conversation's items so we recover the latest
        socket path (the sidebar's encoded key intentionally
        omits it; see ``_terminal_target_key``).
    :param read_only: When ``True``, pass ``-r`` to ``tmux
        attach`` so the spawned window can't send keys to the
        underlying session.
    """
    decoded = _decode_terminal_target_key(target.key)
    if decoded is None:
        # Not a terminal target — silently ignore. Pressing
        # ``O`` / ``R`` on the main / sub-agent rows is harmless.
        return
    conv_id, name, session = decoded

    if not os.environ.get("TMUX"):
        # Outside tmux there's no host session for the new
        # window to attach to. The user can copy the Attach
        # command from the panel and paste it themselves —
        # surface a hint instead of failing silently.
        import sys as _sys

        print(
            "\nCan't open attach: not running inside tmux. "
            "Copy the Attach command from the panel and run it "
            "in a separate terminal, or start your REPL inside "
            "tmux to enable the O / R hotkeys.\n",
            file=_sys.stderr,
        )
        return

    # Paginate so the attach action finds terminals whose
    # launch outputs land past position 99 — same bug shape as
    # the sidebar enumeration in ``_collect_overview_targets``
    # (the user-reported 2026-04-30 "17 of 20 terminals" case).
    try:
        items = await _list_all_conversation_items(
            client,
            conv_id,
        )
    except Exception:  # noqa: BLE001 — overlay action: a per-conversation fetch error becomes a stderr hint instead of crashing the overlay
        import sys as _sys

        print(
            f"\nCan't open attach for {target.label}: items fetch failed.\n",
            file=_sys.stderr,
        )
        return

    matches = [
        info
        for info in _reconstruct_terminals_from_items(items, conv_id=conv_id)
        if info.name == name and info.session == session
    ]
    if not matches:
        import sys as _sys

        print(
            f"\nCan't open attach for {target.label}: terminal is no longer "
            f"live (closed since the sidebar was built).\n",
            file=_sys.stderr,
        )
        return

    info = matches[0]
    import shlex
    import subprocess as _subprocess
    import sys as _sys

    # Runtime liveness check shared with the Status field
    # rendering in :func:`_build_terminal_overview`. Skipping
    # the new-window spawn when the session is gone means the
    # second ``O`` press doesn't silently fail (a window that
    # opens just to error and close).
    if not _tmux_session_alive(info.socket, info.target):
        print(
            f"\nCan't open attach for {target.label}: tmux session is gone "
            f"(user likely exited the shell on a previous attach, killing "
            f"the agent's pane → window → session → tmux server). The "
            f"sidebar still shows it as live because no sys_terminal_close "
            f"tool call was recorded. Ask the agent to launch a new "
            f"terminal, or close + relaunch the conversation.\n",
            file=_sys.stderr,
        )
        return

    # ``tmux new-window`` runs inside the user's existing tmux
    # session ($TMUX picks it up). The argument is the shell
    # command that the new window's pane will run — we hand
    # tmux another tmux invocation that attaches to the
    # AGENT's tmux server (different socket, distinct from
    # the user's outer session).
    inner = (
        f"tmux -S {shlex.quote(info.socket)} attach"
        f"{' -r' if read_only else ''} -t {shlex.quote(info.target)}"
    )
    try:
        _subprocess.run(
            ["tmux", "new-window", inner],
            check=True,
            timeout=5,
        )
    except (FileNotFoundError, _subprocess.CalledProcessError, _subprocess.TimeoutExpired):
        print(
            f"\nCan't open attach for {target.label}: tmux new-window failed. "
            f"Run manually: {inner}\n",
            file=_sys.stderr,
        )


def _terminal_attach_command(info: _TerminalInfo) -> str:
    """
    Build the shell command that attaches to *info*'s tmux session.

    Matches the legacy CLI's
    :meth:`omnigent.inner.cli._terminal_attach_command`
    output (``cli.py:2196``) so users with muscle memory from
    the non-AP path see the same string. ``shlex.quote``
    keeps the socket path safe for terminals where the path
    contains spaces (uncommon but possible on macOS).

    :param info: The terminal to attach to.
    :returns: A complete shell command,
        e.g. ``"tmux -S /tmp/.../sock attach -t main"``.
    """
    import shlex

    return f"tmux -S {shlex.quote(info.socket)} attach -t {shlex.quote(info.target)}"


async def _build_terminal_overview(
    decoded: tuple[str, str, str],
    *,
    target: OverlayTarget,
    client: OmnigentClient,
    fmt: RichBlockFormatter,
) -> RenderableType:
    """
    Render the content panel for a terminal sidebar target.

    Re-fetches the owning conversation's items and runs
    :func:`_reconstruct_terminals_from_items` again to find the
    matching terminal. This re-walk is what lets the encoded
    key stay short — the socket isn't on the key, it's read
    fresh from the persisted launch output.

    The panel mirrors the legacy CLI's
    :meth:`omnigent.inner.cli._render_overview_terminal_text`
    output (``cli.py:2232``):

      Terminal: <name>:<session>
      Owner: <conv_id>
      Status: live (per tool history)
      Socket: <tmux_socket>
      Attach: tmux -S <socket> attach -t main

    Plus a hint reminding the user that ``Status`` reflects
    the agent's last tool call, not a live process check.

    Failure modes (items fetch errors, terminal not found in
    the re-walk) surface inside the panel as inline text;
    overlays are diagnostic and shouldn't be the thing that
    crashes the REPL.

    :param decoded: ``(conv_id, name, session)`` tuple from
        :func:`_decode_terminal_target_key`.
    :param target: The selected :class:`OverlayTarget` —
        used only for the header label.
    :param client: Agent-plane HTTP client.
    :param fmt: REPL formatter for muted / accent styling.
    :returns: A :class:`rich.console.Group` for the overlay
        host's content area.
    """
    from rich.console import Group
    from rich.text import Text

    conv_id, name, session = decoded
    parts: list[RenderableType] = []
    parts.append(Text.from_markup(f"[bold]Terminal: {target.label}[/bold]"))
    parts.append(
        Text.from_markup(
            f"  [{fmt.muted}]Owner conversation[/{fmt.muted}]: {conv_id}",
        ),
    )

    # Re-walk to find the matching terminal. Paginate so
    # terminals whose launch outputs land past position 99
    # are still findable here — the user-reported 2026-04-30
    # symptom otherwise rendered "not found in conversation
    # history" for s18-s20 even though the launch outputs
    # existed (just past the cap).
    try:
        items = await _list_all_conversation_items(
            client,
            conv_id,
        )
    except Exception as exc:  # noqa: BLE001 — overlay content builder: any items-fetch error surfaces as a diagnostic line; the panel still renders
        parts.append(
            Text.from_markup(
                f"  [{fmt.error}]Failed to fetch conversation items: "
                f"{type(exc).__name__}: {exc}[/{fmt.error}]",
            ),
        )
        return Group(*parts)

    matches = [
        info
        for info in _reconstruct_terminals_from_items(items, conv_id=conv_id)
        if info.name == name and info.session == session
    ]
    if not matches:
        # Terminal isn't in the live set — either it was closed
        # since the sidebar was built, or the agent's tool
        # history doesn't include the launch. Either way the
        # action keys won't work, so we say so explicitly.
        parts.append(
            Text.from_markup(
                f"  [{fmt.muted}]Status[/{fmt.muted}]: "
                f"[{fmt.error}]not found in conversation history[/{fmt.error}]",
            ),
        )
        parts.append(Text(""))
        parts.append(
            Text.from_markup(
                f"[{fmt.muted}]The terminal may have been closed, or this "
                f"sidebar entry is stale. Reopen the overlay (Esc, then "
                f"Ctrl+O) to refresh.[/{fmt.muted}]",
            ),
        )
        return Group(*parts)

    info = matches[0]
    # Runtime liveness check via ``tmux has-session`` against
    # the recovered socket — the inferred-from-tool-history
    # view doesn't catch cases where the user attached and
    # exited the bash shell on a previous attach (which kills
    # the agent's pane → window → session → tmux server). The
    # walker still shows the terminal as live in those cases
    # because no ``sys_terminal_close`` was recorded. Querying
    # tmux directly here gives ground truth: the panel
    # reflects whether the user can ACTUALLY attach right now,
    # not the agent's last-known state.
    is_alive = _tmux_session_alive(info.socket, info.target)
    if is_alive:
        status_markup = f"[{fmt.success}]live[/{fmt.success}]"
    else:
        status_markup = f"[{fmt.error}]dead[/{fmt.error}]"
    parts.append(
        Text.from_markup(
            f"  [{fmt.muted}]Status[/{fmt.muted}]: {status_markup}",
        ),
    )
    parts.append(
        Text.from_markup(
            f"  [{fmt.muted}]Socket[/{fmt.muted}]: {info.socket}",
        ),
    )
    attach = _terminal_attach_command(info)
    parts.append(
        Text.from_markup(
            f"  [{fmt.muted}]Attach[/{fmt.muted}]: [{fmt.accent}]{attach}[/{fmt.accent}]",
        ),
    )
    parts.append(Text(""))
    if is_alive:
        snapshot = _tmux_pane_snapshot(info.socket, info.target)
        parts.append(Text.from_markup(f"[{fmt.muted}]Screen snapshot[/{fmt.muted}]:"))
        if snapshot is None:
            parts.append(
                Text.from_markup(
                    f"  [{fmt.error}]unavailable (tmux capture-pane failed)[/{fmt.error}]",
                ),
            )
        elif not snapshot.strip():
            parts.append(
                Text.from_markup(
                    f"  [{fmt.muted}](empty terminal screen)[/{fmt.muted}]",
                ),
            )
        else:
            for line in snapshot.rstrip("\n").splitlines():
                parts.append(Text(f"  {line}"))
        parts.append(Text(""))
        parts.append(
            Text.from_markup(
                f"[{fmt.muted}]Press [/{fmt.muted}]"
                f"[{fmt.accent}]O[/{fmt.accent}]"
                f"[{fmt.muted}] to attach in a new tmux window, "
                f"or [/{fmt.muted}]"
                f"[{fmt.accent}]R[/{fmt.accent}]"
                f"[{fmt.muted}] to attach read-only. You can also "
                f"copy the Attach command above and run it manually.[/{fmt.muted}]",
            ),
        )
    else:
        parts.append(
            Text.from_markup(
                f"[{fmt.muted}]The agent's tmux session is gone (e.g. the "
                f"shell exited on a previous attach, killing the pane). The "
                f"agent doesn't know — no ``sys_terminal_close`` was "
                f"recorded — so the sidebar still shows the row. Ask the "
                f"agent to launch a new terminal to recover.[/{fmt.muted}]",
            ),
        )
    return Group(*parts)


def _tmux_session_alive(socket: str, target: str) -> bool:
    """
    Probe whether ``tmux has-session`` succeeds against *socket*.

    Used by :func:`_build_terminal_overview` to surface the real
    runtime liveness of an agent-launched tmux session — the
    inferred-from-tool-history view alone can't catch sessions
    the agent didn't formally close (e.g. the user attached,
    typed ``exit`` in the bash pane, killing the pane → window
    → session → tmux server). The agent never knew, so no
    ``sys_terminal_close`` ended up in the conversation, so the
    walker still shows the row as live.

    Best-effort: any subprocess error (tmux missing, timeout,
    permission glitch on the socket) returns ``False`` so the
    panel surfaces "dead" rather than crashing the overlay.
    The Attach command stays printed regardless — the user can
    still try it manually.

    :param socket: Tmux socket path the agent's
        :class:`TerminalInstance` opened on, e.g.
        ``"/tmp/omnigent-terminal-xyz/tmux.sock"``.
    :param target: Tmux session/target name. Always
        ``"main"`` per
        :class:`omnigent.inner.terminal.TerminalInstance.tmux_target`.
    :returns: ``True`` only when ``tmux has-session`` exits
        zero — i.e. the session is reachable on the socket.
    """
    import subprocess as _subprocess

    try:
        result = _subprocess.run(
            ["tmux", "-S", socket, "has-session", "-t", target],
            capture_output=True,
            timeout=5,
        )
    except (FileNotFoundError, _subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def _tmux_pane_snapshot(socket: str, target: str) -> str | None:
    """
    Capture the current visible tmux pane text for a terminal overview.

    The Ctrl+O debug overlay calls this only after
    :func:`_tmux_session_alive` reports that the terminal is reachable.
    The helper still treats every subprocess failure as non-fatal
    because the overlay is diagnostic: stale sockets, missing tmux, or
    permission errors should render an inline "unavailable" line rather
    than crashing the REPL.

    :param socket: Tmux socket path the agent's
        :class:`TerminalInstance` opened on, e.g.
        ``"/tmp/omnigent-terminal-xyz/tmux.sock"``.
    :param target: Tmux session/target name, e.g. ``"main"``.
    :returns: The current visible pane text from ``tmux capture-pane
        -p``, decoded as UTF-8 with replacement for invalid bytes.
        Returns ``None`` when tmux cannot capture the pane.
    """
    import subprocess as _subprocess

    try:
        result = _subprocess.run(
            ["tmux", "-S", socket, "capture-pane", "-t", target, "-p"],
            capture_output=True,
            timeout=5,
        )
    except (FileNotFoundError, _subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def _parse_sub_agent_handle(raw: str) -> dict[str, object] | None:
    """
    Extract a sys_session_send handle dict from a function_call_output.

    Native omnigent builtins (``sys_session_send`` /
    ``sys_session_send`` continuation on the builtin path) persist the output
    as a raw JSON string of the handle dict —
    ``{"kind": "sub_agent", "conversation_id": ..., ...}``.

    Harnesses that route tools through an MCP server — notably the
    claude-sdk harness's MCP bridge — wrap the same payload as an
    MCP content-part list before persistence:
    ``[{"type": "text", "text": "<handle-json-string>"}]``. Without
    the second branch here, the overlay silently drops every
    sub-agent row on that harness, which manifests as zero
    sub-agent tabs even while ``list_tasks`` reports the children
    live (reported against coding_supervisor_with_forks on 2026-04-22).

    Both shapes are tolerated; anything else returns ``None`` so
    the caller's loop can skip the item cleanly.

    :param raw: The ``function_call_output.output`` string as
        persisted by the workflow, e.g. a handle JSON string or an
        MCP content-parts JSON string.
    :returns: The handle dict when *raw* parses to one of the two
        recognized shapes AND carries ``kind == "sub_agent"``;
        ``None`` otherwise.
    """
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(payload, dict):
        return payload if payload.get("kind") == "sub_agent" else None
    if isinstance(payload, list):
        # MCP content-parts wrapper. Walk parts, parse the first
        # ``text`` part that decodes to a sub_agent handle. Multiple
        # text parts on a single tool result are valid per the MCP
        # spec, but sys_session_send emits exactly one — return the
        # first match rather than aggregating.
        for part in payload:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "text":
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            try:
                inner = json.loads(text)
            except (ValueError, TypeError):
                continue
            if isinstance(inner, dict) and inner.get("kind") == "sub_agent":
                return inner
    return None


async def _build_debug_overview(
    target: OverlayTarget,
    *,
    client: OmnigentClient,
    session: Session,
    agent_name: str,
    fmt: RichBlockFormatter,
    server_log_path: pathlib.Path | None = None,
    runner_log_path: pathlib.Path | None = None,
    event_log_path: pathlib.Path | None = None,
    cli_log_path: pathlib.Path | None = None,
) -> RenderableType:
    """
    Assemble the Ctrl+O debug overview for the REPL.

    The overview intentionally mirrors the ``omnigent run``
    debug panel: a "Session: main" header with session id /
    agent / response / conversation metadata, followed by an
    indexed event stream where every conversation item is
    printed as ``[N] type=...`` with its fields on the
    following indented lines. This keeps the two CLIs visually
    consistent when comparing behavior across harnesses.

    Sections (in order):

    1. **Session header** — ``Session: main``, Session ID
       (conversation id), Agent, Model, Response, Messages
       count. Matches :func:`_render_overview_session_text`
       in ``omnigent/cli.py``.
    2. **Event stream** — all items from the conversation,
       paginated via :func:`_list_all_conversation_items`,
       re-rendered via
       :func:`_render_overview_event` into the same
       ``[N] type=...`` shape omnigent uses. Responses API
       items map onto the omnigent event vocabulary as
       follows: ``message`` (user) → ``user_message``;
       ``message`` (assistant) → ``assistant_message``;
       ``function_call`` → ``tool_call_request``;
       ``function_call_output`` → ``tool_call_complete``.
       Reasoning items are shown as ``reasoning``.
    3. **Fallback** — when no conversation exists yet (fresh
       REPL, no turns), a one-liner explaining the state.

    Errors from the ``/v1/sessions`` fetch surface inside
    the overlay as a red line rather than propagating — Ctrl+O is
    a debug surface, not a critical path, and a transient server
    hiccup shouldn't kill the overlay.

    :param client: The omnigent client used for the items fetch.
    :param session: The client ``Session`` tracking
        ``current_response_id``.
    :param agent_name: Registered agent name for the header, e.g.
        ``"coding_supervisor"``.
    :param fmt: The REPL's :class:`RichBlockFormatter` — reused so
        the overview uses the same palette (muted / accent / error
        colors) as the scrollback.
    :param server_log_path: Optional path to the local server log.
    :param event_log_path: Optional path to the JSONL event log.
    :param cli_log_path: Optional path to the always-on CLI
        diagnostics log (``~/.omnigent/logs/cli-*.log``).
    :returns: A Rich :class:`Group` suitable for passing to
        :meth:`TerminalHost.add_overlay`'s ``builder`` contract.
    """
    from rich.console import Group
    from rich.text import Text

    # Terminal targets short-circuit to a dedicated renderer —
    # they're not conversations and the items-fetch / event-stream
    # path below doesn't apply. The decoded key carries everything
    # needed (conv_id, name, session); the renderer re-walks the
    # owning conversation's items to recover the socket and emit
    # an attach command.
    if target is not None:
        decoded = _decode_terminal_target_key(target.key)
        if decoded is not None:
            return await _build_terminal_overview(
                decoded,
                target=target,
                client=client,
                fmt=fmt,
            )

    # Branch on the selected sidebar target. The ``main`` target's
    # key holds either the current conversation id (once a turn
    # has started) or the sentinel string ``"main"`` (fresh REPL).
    # Sub-agent targets' keys are always real conversation ids by
    # construction in :func:`_collect_overview_targets`.
    is_main = target is None or target.label == "main"
    resolve_error: str | None = None
    conv_id: str | None = None
    response_id = session.current_response_id if is_main else None

    if is_main:
        if target is not None and target.key != "main":
            # Sidebar already resolved the conversation id when
            # building the target list — skip the extra round-trip.
            conv_id = target.key
        elif response_id:
            try:
                resp = await client.responses.get(response_id)
                conv_id = resp.conversation.id if resp.conversation else None
            except Exception as exc:  # noqa: BLE001 — overlay content builder: capture any lookup error as a displayable string; the overlay panel must still render even under partial failure
                resolve_error = f"{type(exc).__name__}: {exc}"
        else:
            # Sessions-API path: no response_id but the adapter
            # may already have a session_id (== conv_id). Read it
            # off rather than leaving the overlay empty.
            _sid = getattr(session, "session_id", None)
            if isinstance(_sid, str):
                conv_id = _sid
    else:
        conv_id = target.key

    parts: list[RenderableType] = []
    header = target.label if target is not None else "main"
    parts.append(Text.from_markup(f"[bold]Session: {header}[/bold]"))
    parts.append(
        Text.from_markup(
            f"  [{fmt.muted}]Session ID[/{fmt.muted}]: {conv_id or '(no conversation yet)'}",
        ),
    )
    if is_main:
        parts.append(
            Text.from_markup(
                f"  [{fmt.muted}]Agent[/{fmt.muted}]: {agent_name}",
            ),
        )
        parts.append(
            Text.from_markup(
                f"  [{fmt.muted}]Model[/{fmt.muted}]: {session.model}",
            ),
        )
        parts.append(
            Text.from_markup(
                f"  [{fmt.muted}]Response[/{fmt.muted}]: {response_id or '(none yet)'}",
            ),
        )
        if server_log_path is not None:
            parts.append(
                Text.from_markup(
                    f"  [{fmt.muted}]Server log[/{fmt.muted}]: {server_log_path}",
                ),
            )
        if runner_log_path is not None:
            parts.append(
                Text.from_markup(
                    f"  [{fmt.muted}]Runner log[/{fmt.muted}]: {runner_log_path}",
                ),
            )
        if event_log_path is not None:
            parts.append(
                Text.from_markup(
                    f"  [{fmt.muted}]Event log[/{fmt.muted}]: {event_log_path}",
                ),
            )
        if cli_log_path is not None:
            parts.append(
                Text.from_markup(
                    f"  [{fmt.muted}]CLI log[/{fmt.muted}]: {cli_log_path}",
                ),
            )

    if resolve_error is not None:
        parts.append(
            Text.from_markup(
                f"  [{fmt.error}]Failed to resolve conversation: {resolve_error}[/{fmt.error}]",
            ),
        )

    if conv_id is None:
        parts.append(Text(""))
        parts.append(
            Text.from_markup(
                f"[{fmt.muted}]No conversation yet. Send a message to start one.[/{fmt.muted}]",
            ),
        )
        return Group(*parts)

    # Fetch conversation-level metadata (labels) alongside items so
    # the overlay can render guardrails label state — the legacy
    # Ctrl+G overview shows ``Labels: key=val, ...`` on every
    # session, and Ctrl+O should match. Failure here is non-fatal:
    # the overlay is diagnostic and should still render the event
    # stream if the labels fetch hiccups.
    labels: dict[str, str] = {}
    labels_error: str | None = None
    items: list[dict[str, object]] = []
    items_error: str | None = None
    try:
        snap = await client.sessions.get(conv_id)
        labels = snap.labels
    except Exception as exc:  # noqa: BLE001 — overlay content builder
        labels_error = f"{type(exc).__name__}: {exc}"
    try:
        items = await _list_all_conversation_items(
            client,
            conv_id,
        )
    except Exception as exc:  # noqa: BLE001 — overlay content builder
        items_error = f"{type(exc).__name__}: {exc}"
    if items_error is not None:
        parts.append(
            Text.from_markup(
                f"\n[{fmt.error}]Failed to fetch conversation items: {items_error}[/{fmt.error}]",
            ),
        )
        return Group(*parts)

    # Render ``Labels: key=val, ...`` (sorted by key, ``(none)`` when
    # empty) directly after the session header to mirror the legacy
    # Ctrl+G overview's ``_format_session_labels`` output line for
    # line. Placed before ``Messages`` so the label state is visible
    # even when the conversation has many items.
    if labels_error is not None:
        parts.append(
            Text.from_markup(
                f"  [{fmt.error}]Labels fetch failed: {labels_error}[/{fmt.error}]",
            ),
        )
    else:
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(labels.items())) if labels else "(none)"
        parts.append(Text.from_markup(f"  [{fmt.muted}]Labels[/{fmt.muted}]: {rendered}"))
    parts.append(Text.from_markup(f"  [{fmt.muted}]Messages[/{fmt.muted}]: {len(items)}"))
    parts.append(Text(""))

    if not items:
        parts.append(
            Text.from_markup(
                f"[{fmt.muted}](no messages yet)[/{fmt.muted}]",
            ),
        )
        return Group(*parts)

    # Pre-pass: function_call items hold the tool name, but the
    # matching function_call_output only carries ``call_id`` +
    # ``output``. Index names by call_id so the event stream
    # renders ``name=Bash`` on both sides of the request/complete
    # pair — matching the omnigent event view. When an output
    # arrives without a prior request (e.g. the server trimmed
    # the head of the history), we fall back to ``"?"`` in the
    # renderer.
    call_id_to_name: dict[str, str] = {}
    for item in items:
        if item.get("type") == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            if isinstance(call_id, str) and isinstance(name, str):
                call_id_to_name[call_id] = name

    for idx, item in enumerate(items, start=1):
        parts.extend(_render_overview_event(idx, item, call_id_to_name, fmt))

    return Group(*parts)


def _render_overview_event(
    idx: int,
    item: dict[str, object],
    call_id_to_name: dict[str, str],
    fmt: RichBlockFormatter,
) -> list[RenderableType]:
    """
    Render one conversation item as an omnigent-style event.

    Produces a header line ``[N] type=<event>`` followed by
    indented field lines (``name: ...``, ``args: ...``,
    ``status: ...``, ``result: ...``). The per-type field set
    matches what ``omnigent/cli.py::_render_overview_item``
    emits so the two overviews read identically.

    :param idx: 1-based index for the ``[N]`` header.
    :param item: Raw conversation-items dict as returned by the
        ``/v1/sessions/<id>/items`` endpoint. Expected
        fields depend on ``item["type"]``: ``message`` carries
        ``role`` + ``content`` parts; ``function_call`` carries
        ``name`` + ``arguments`` + ``call_id``;
        ``function_call_output`` carries ``call_id`` + ``output``;
        ``reasoning`` carries ``summary``.
    :param call_id_to_name: Precomputed ``call_id → tool_name``
        lookup built from the same items list — used to print
        the tool name on ``tool_call_complete`` lines where the
        raw item only has a call_id.
    :param fmt: The REPL formatter used for color styling.
    :returns: A list of Rich renderables (one per line) ready to
        append to the overview :class:`Group`.
    """
    from rich.text import Text

    # Missing ``type`` is an API violation — every conversation
    # item ships with a discriminator. Render it as ``(unknown)``
    # so the sidebar surfaces the broken row rather than silently
    # swallowing it; a fresh server-side type that this switch
    # doesn't recognise falls into the same branch.
    itype = item.get("type")
    if itype == "message":
        return _render_overview_message_event(idx, item, fmt)
    if itype == "function_call":
        name = item.get("name")
        if not isinstance(name, str):
            # ``name`` is required for function_call per API.md.
            # A missing name means the item is malformed; render
            # the event so scroll context isn't lost, but flag
            # the missing field explicitly.
            name = "(missing name)"
        lines: list[RenderableType] = [
            Text.from_markup(
                f"[{fmt.accent}][{idx}][/{fmt.accent}] "
                f"[bold]type[/bold]=tool_call_request "
                f"[{fmt.muted}]name={name}[/{fmt.muted}]",
            ),
        ]
        args = item.get("arguments")
        if args:
            lines.append(Text.from_markup(f"    [{fmt.muted}]args[/{fmt.muted}]: {args}"))
        return lines
    if itype == "function_call_output":
        call_id = item.get("call_id")
        # ``call_id`` is required per API.md, so a missing entry
        # means the item is malformed. Skip the name-lookup in
        # that case — ``(missing call_id)`` tells the reader the
        # item couldn't be correlated to its request.
        if isinstance(call_id, str):
            name = call_id_to_name.get(call_id) or "(unknown)"
        else:
            name = "(missing call_id)"
        lines = [
            Text.from_markup(
                f"[{fmt.accent}][{idx}][/{fmt.accent}] "
                f"[bold]type[/bold]=tool_call_complete "
                f"[{fmt.muted}]name={name}[/{fmt.muted}]",
            ),
        ]
        status = item.get("status")
        if status:
            lines.append(Text.from_markup(f"    [{fmt.muted}]status[/{fmt.muted}]: {status}"))
        output = item.get("output")
        if output:
            text = str(output)
            preview = text[:400]
            if len(text) > 400:
                preview += "…"
            for line in preview.split("\n"):
                lines.append(Text.from_markup(f"    [{fmt.muted}]{line}[/{fmt.muted}]"))
        return lines
    if itype == "reasoning":
        # ``summary`` and ``content`` are both optional on
        # reasoning items (different providers populate
        # different fields). Use ``None`` as the absence
        # sentinel, not ``""``, so the truthy check below
        # doesn't conflate an empty string with a missing
        # field.
        summary = item.get("summary") or item.get("content")
        lines = [
            Text.from_markup(
                f"[{fmt.accent}][{idx}][/{fmt.accent}] [bold]type[/bold]=reasoning",
            ),
        ]
        if summary:
            text = str(summary)[:400]
            for line in text.split("\n"):
                if line.strip():
                    lines.append(Text.from_markup(f"    [{fmt.muted}]{line}[/{fmt.muted}]"))
        return lines
    # Unknown item type — surface it rather than silently dropping,
    # so new server-side types become visible instead of invisible.
    label = itype if isinstance(itype, str) and itype else "(unknown)"
    return [
        Text.from_markup(
            f"[{fmt.accent}][{idx}][/{fmt.accent}] [bold]type[/bold]={label}",
        ),
    ]


def _render_overview_message_event(
    idx: int,
    item: dict[str, object],
    fmt: RichBlockFormatter,
) -> list[RenderableType]:
    """
    Render a ``message`` item as an omnigent event.

    Splits on role so the header reads ``user_message`` or
    ``assistant_message`` (matching omnigent' event vocabulary)
    instead of the raw Responses API ``message`` label. The
    content parts are flattened to text and printed on indented
    continuation lines, preview-capped at 400 chars so one
    long system-synthesized message doesn't dominate the pane.

    :param idx: 1-based index for the ``[N]`` header.
    :param item: Conversation item with ``role`` and ``content``.
    :param fmt: Formatter for color styling.
    :returns: Rich renderables — header + continuation lines.
    """
    from rich.text import Text

    # ``role`` is required on message items per API.md. A message
    # with a missing role has no meaningful event vocabulary to
    # map onto; fall through to the assistant form (the more
    # visually-obvious side) so the user sees the broken row.
    role = item.get("role")
    event_type = "user_message" if role == "user" else "assistant_message"
    content = item.get("content") or []
    text_parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("input_text", "output_text"):
                # ``text`` is the payload of every text content
                # part; missing it means the block is malformed.
                # Render the non-text blocks as empty rather than
                # as the literal string "None" by skipping here.
                block_text = block.get("text")
                if isinstance(block_text, str):
                    text_parts.append(block_text)
    text = " ".join(text_parts)
    header = f"[{fmt.accent}][{idx}][/{fmt.accent}] [bold]type[/bold]={event_type}"
    if role == "assistant":
        model = item.get("model")
        if isinstance(model, str) and model:
            header += f" [{fmt.muted}]model={model}[/{fmt.muted}]"
    lines: list[RenderableType] = [Text.from_markup(header)]
    preview = text[:400]
    if len(text) > 400:
        preview += "…"
    for line in preview.split("\n"):
        if line.strip():
            lines.append(Text.from_markup(f"    [{fmt.muted}]{line}[/{fmt.muted}]"))
    return lines


def _tool_metadata_from_function_call_item(
    item: dict[str, object],
) -> tuple[str | None, dict[str, object] | None]:
    """Extract tool name/arguments from a ``function_call`` item.

    Sessions-API live events may surface persisted conversation items
    in either the flat API shape (``name`` / ``arguments`` at top
    level) or the entity-shaped envelope (``data.name`` /
    ``data.arguments``). The history renderer and the live
    sessions renderer both need the same tolerant extraction so tool
    result panels can dispatch to the pretty renderers instead of
    falling back to generic ``?`` JSON boxes.
    """
    name = item.get("name")
    raw_arguments = item.get("arguments")
    data = item.get("data")
    if isinstance(data, dict):
        if not isinstance(name, str):
            name = data.get("name")
        if raw_arguments is None:
            raw_arguments = data.get("arguments")
    parsed_arguments = _coerce_arguments_dict(raw_arguments)
    return (name if isinstance(name, str) else None), parsed_arguments


def _build_call_id_to_tool_metadata_lookup(
    items: list[dict[str, object]],
) -> dict[str, tuple[str, dict[str, object]]]:
    """Index ``function_call`` items' tool metadata by ``call_id``.

    ``function_call_output`` items only carry the ``call_id``;
    re-rendering them with a tool name and original arguments attached
    requires walking the full item list once and stashing each
    function_call's metadata. The result lets
    :func:`_render_history_item` call the same pretty tool renderers
    that live responses use (for example shell/read/edit panels).
    """
    out: dict[str, tuple[str, dict[str, object]]] = {}
    for item in items:
        if item.get("type") != "function_call":
            continue
        call_id = item.get("call_id")
        name, arguments = _tool_metadata_from_function_call_item(item)
        if isinstance(call_id, str) and name is not None and arguments is not None:
            out[call_id] = (name, arguments)
    return out


def _build_call_id_to_name_lookup(items: list[dict[str, object]]) -> dict[str, str]:
    """
    Index ``function_call`` items' tool names by ``call_id``.

    ``function_call_output`` items only carry the ``call_id``;
    re-rendering them with a tool name attached requires walking
    the full item list once and stashing each function_call's
    ``name`` keyed by its ``call_id``. The result is consumed by
    :func:`_render_history_item` to build the panel title for
    the matching output.

    :param items: Conversation items in any order. Both API and
        entity shapes are tolerated since each function_call
        carries the same flat ``call_id`` + ``name`` fields in
        either shape.
    :returns: Map from ``call_id`` to tool name. Items missing
        either field are skipped silently — callers fall back to
        a placeholder when a lookup misses.
    """
    return {
        call_id: name
        for call_id, (name, _arguments) in _build_call_id_to_tool_metadata_lookup(items).items()
    }


def _coerce_arguments_dict(raw: object) -> dict[str, object]:
    """
    Normalize a ``function_call.arguments`` field to a dict.

    The omnigent API surfaces tool-call arguments as a JSON
    object dict; some legacy / harness paths persist the raw
    JSON string instead. Accept both so resume rendering works
    regardless of which writer produced the row.

    :param raw: Either a dict, a JSON-encoded string, or
        anything else (returned as the empty dict).
    :returns: Parsed arguments dict, e.g. ``{"file_path": "/x.py"}``.
        Empty dict on any decode failure or non-object payload —
        the caller renders ``⏵ name()`` instead of raising.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def _extract_message_text(item: dict[str, object]) -> str:
    """
    Concatenate ``input_text`` / ``output_text`` content blocks
    in a message item into a single string.

    :param item: A ``type="message"`` conversation item.
    :returns: Joined text from every text content block in
        order. Empty string when the message has no text blocks
        (e.g. a steering message with only file attachments).
    """
    content = item.get("content", [])
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for b in content:
        if isinstance(b, dict) and b.get("type") in ("input_text", "output_text"):
            parts.append(str(b.get("text", "")))
    return " ".join(parts)


def _extract_function_call_output_text(item: dict[str, object]) -> str:
    """
    Pull the textual output payload out of a
    ``function_call_output`` item, accepting both the API shape
    (``output`` flattened to the top level) and the entity shape
    (``data.output``). Used when re-rendering a tool result
    panel on resume.

    :param item: A ``type="function_call_output"`` conversation
        item.
    :returns: Raw output string. Empty on non-string / missing
        payloads — the caller still renders an empty result
        panel so the call/output pairing is visible.
    """
    raw = item.get("output")
    if isinstance(raw, str):
        return raw
    data = item.get("data")
    if isinstance(data, dict):
        nested = data.get("output")
        if isinstance(nested, str):
            return nested
    return ""


def _render_message_history_item(
    item: dict[str, object],
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """
    Render a ``type="message"`` item.

    User messages emit via :meth:`RichBlockFormatter.user_message`
    (same ``❯`` echo shown live). Assistant messages emit a
    ``◆ <model>`` header then the body as one or more Markdown
    paragraphs, matching what the live stream produces — so
    headers, code blocks, lists, etc. render the same on resume
    as they did originally, in default terminal foreground (the
    previous rendering used a muted gray that looked
    second-class). Empty assistant items (the workflow's
    trailing ``[{"type":"output_text","text":""}]``) are silently
    skipped to avoid a phantom header with no body underneath.

    :param item: A ``type="message"`` conversation item.
    :param host: The :class:`TerminalHost` to render against.
    :param fmt: The :class:`RichBlockFormatter` used for styling
        and for the Markdown ``code_theme``.
    """
    from rich.markdown import Markdown
    from rich.padding import Padding
    from rich.text import Text

    if item.get("is_meta") is True:
        return
    role = item.get("role", "")
    text = _extract_message_text(item)
    if role == "user":
        host.output(fmt.user_message(text))
        return
    if role == "assistant":
        # Skip empty assistant messages. The omnigent workflow
        # persists a trailing empty assistant item alongside every
        # real reply (``[{"type":"output_text","text":""}]``);
        # without this guard, replaying the conversation renders a
        # phantom ``◆ <model>`` line with no body underneath.
        if not text.strip():
            return
        model = item.get("model", "")
        host.output(Text.from_markup(f" [{fmt.assistant}]◆ {model}[/{fmt.assistant}]"))
        # Match the live stream's per-paragraph Markdown rendering
        # (see ``RichBlockFormatter._markdown_replace``): split on
        # blank-line paragraph boundaries, render each non-empty
        # paragraph as a padded Markdown panel using the
        # formatter's ``code_theme`` so resumed output is visually
        # identical to what the user originally saw — full
        # foreground color, syntax-highlighted code fences,
        # rendered headings.
        for paragraph in text.split("\n\n"):
            if not paragraph.strip():
                continue
            host.output(
                Padding(
                    Markdown(paragraph, code_theme=fmt.code_theme),
                    # (top, right, bottom, left): no vertical
                    # padding (paragraphs already separate),
                    # 1 right and 3 left = same indentation the
                    # live stream's ``_markdown_replace`` uses, so
                    # resumed paragraphs align with live ones.
                    (0, 1, 0, 3),
                ),
            )


def _render_function_call_history_item(
    item: dict[str, object],
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """
    Render a ``type="function_call"`` item as the live
    ``⏵ <name>(<args>)`` line.

    Builds a :class:`ToolExecution` from the item's flat fields,
    populates ``args_summary`` via :func:`format_tool_args_brief`
    (the same helper the live stream uses), and dispatches to
    :meth:`RichBlockFormatter.format_tool_group` so the call
    line on resume matches the line emitted live.

    :param item: A ``type="function_call"`` conversation item.
    :param host: The :class:`TerminalHost` to render against.
    :param fmt: The :class:`RichBlockFormatter` used for styling.
    """
    extracted_name, extracted_arguments = _tool_metadata_from_function_call_item(item)
    name = extracted_name or "?"
    arguments = extracted_arguments or {}
    call_id = str(item.get("call_id") or "")
    execution = ToolExecution(
        name=name,
        arguments=arguments,
        args_summary=format_tool_args_brief(name, arguments),
        call_id=call_id,
        agent_name="",
    )
    for renderable in fmt.format_tool_group(
        ToolGroup(executions=[execution], ctx=BlockContext()),
    ):
        host.output(renderable)


def _render_function_call_output_history_item(
    item: dict[str, object],
    host: TerminalHost,
    fmt: RichBlockFormatter,
    call_id_to_name: dict[str, str],
    call_id_to_tool_metadata: dict[str, tuple[str, dict[str, object]]],
) -> None:
    """
    Render a ``type="function_call_output"`` item as the live
    result panel.

    The tool name and original arguments are recovered from
    *call_id_to_tool_metadata* — the matching ``function_call``
    carries them, but the output row only carries ``call_id``. On
    miss (e.g. orphan output whose call was trimmed by the server),
    falls back to ``"?"`` rather than skipping so the turn-boundary
    signal is preserved.

    :param item: A ``type="function_call_output"`` conversation
        item.
    :param host: The :class:`TerminalHost` to render against.
    :param fmt: The :class:`RichBlockFormatter` used for styling.
    :param call_id_to_name: Back-compat map from ``call_id`` to
        tool name.
    :param call_id_to_tool_metadata: Map from ``call_id`` to
        ``(tool name, arguments)`` built once per conversation by
        :func:`_build_call_id_to_tool_metadata_lookup`.
    """
    call_id = str(item.get("call_id") or "")
    metadata = call_id_to_tool_metadata.get(call_id)
    if metadata is not None:
        name, arguments = metadata
    else:
        name = call_id_to_name.get(call_id, "?")
        arguments = {}
    output_text = _extract_function_call_output_text(item)
    for renderable in fmt.format_tool_result(
        ToolResultBlock(
            name=name,
            call_id=call_id,
            agent_name="",
            output=output_text,
            arguments=arguments,
            args_summary=format_tool_args_brief(name, arguments),
            ctx=BlockContext(),
        ),
    ):
        host.output(renderable)


def _render_reasoning_history_item(
    item: dict[str, object],
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """
    Render a ``type="reasoning"`` item as the live thinking
    panel.

    ``summary`` and ``content`` are both optional on reasoning
    rows (different providers populate different fields). When
    both are empty, the panel renderer would emit nothing
    anyway — short-circuit explicitly so the reader doesn't see
    a stray blank line.

    :param item: A ``type="reasoning"`` conversation item.
    :param host: The :class:`TerminalHost` to render against.
    :param fmt: The :class:`RichBlockFormatter` used for styling.
    """
    summary = item.get("summary")
    content = item.get("content")
    summary_text = summary if isinstance(summary, str) else ""
    reasoning_text = content if isinstance(content, str) else ""
    if not summary_text.strip() and not reasoning_text.strip():
        return
    for renderable in fmt.format_reasoning(
        ReasoningBlock(
            reasoning_text=reasoning_text,
            summary_text=summary_text,
            ctx=BlockContext(),
        ),
    ):
        host.output(renderable)


def _consume_pending_local_skill_slash_command(
    session: object,
    item: dict[str, object],
) -> bool:
    """
    Consume a matching locally echoed skill slash command.

    The command handler echoes the user's local ``/<skill>`` input
    immediately. When the server later publishes the durable
    ``slash_command`` item, the live TUI should skip exactly one
    matching item for this session while still rendering commands
    that came from another client.

    :param session: REPL session object, usually
        :class:`_SessionsChatReplAdapter`.
    :param item: Live ``slash_command`` item from
        ``response.output_item.done``.
    :returns: ``True`` when a matching pending local echo was found
        and removed.
    """
    pending = getattr(session, "_pending_local_skill_slash_commands", None)
    if not isinstance(pending, list):
        return False
    command_key = (
        str(item.get("name") or ""),
        str(item.get("arguments") or ""),
    )
    for idx, pending_key in enumerate(pending):
        if pending_key == command_key:
            del pending[idx]
            return True
    return False


def _render_slash_command_history_item(
    item: dict[str, object],
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """
    Render a ``type="slash_command"`` item as a compact command echo.

    Skill slash commands are metadata, not normal user messages. The
    visible transcript should show the command the user invoked while
    the paired ``message.is_meta`` record carries the hidden skill
    instructions for agent context.

    :param item: A ``type="slash_command"`` conversation item.
    :param host: The :class:`TerminalHost` to render against.
    :param fmt: The :class:`RichBlockFormatter` used for styling.
    :returns: None.
    """
    from rich.text import Text

    name = str(item.get("name") or "")
    arguments = str(item.get("arguments") or "")
    output = item.get("output")
    label = f"/{name}" if not arguments else f"/{name} {arguments}"
    host.output(Text(f"  {label}", style=fmt.muted))
    if isinstance(output, str) and output:
        host.output(Text(f"  {output}", style=fmt.muted))


def _render_history_item(
    item: dict[str, object],
    host: TerminalHost,
    fmt: RichBlockFormatter | None = None,
    *,
    call_id_to_name: dict[str, str] | None = None,
    call_id_to_tool_metadata: dict[str, tuple[str, dict[str, object]]] | None = None,
) -> None:
    """
    Render a single conversation history item using the same
    visual primitives the live stream uses, so a resumed
    conversation looks identical to its original transcript.

    Dispatches to a per-type helper:

    - ``message`` → :func:`_render_message_history_item`
    - ``function_call`` → :func:`_render_function_call_history_item`
    - ``function_call_output`` →
      :func:`_render_function_call_output_history_item`
    - ``reasoning`` → :func:`_render_reasoning_history_item`

    Unknown types are silently dropped — historically the store
    has only ever emitted these four, and a future addition
    should land its own helper rather than implicitly coercing
    into one of the existing renderers.

    :param item: A conversation item dict from ``list_items``.
    :param host: The :class:`TerminalHost` to render against.
    :param fmt: The :class:`RichBlockFormatter` used for styling.
        A fresh formatter is constructed when omitted — useful
        in tests, less efficient than reusing the caller's
        instance.
    :param call_id_to_name: Back-compat map from ``call_id`` to
        tool name. ``None`` is treated as an empty map (orphan
        outputs render with ``"?"``).
    :param call_id_to_tool_metadata: Map from ``call_id`` to
        ``(tool name, arguments)``. Used to route
        ``function_call_output`` panels through pretty renderers
        that need the original function-call arguments.
    """
    if fmt is None:
        fmt = RichBlockFormatter(show_tool_output=True)
    if call_id_to_name is None:
        call_id_to_name = {}
    if call_id_to_tool_metadata is None:
        call_id_to_tool_metadata = {
            call_id: (name, {}) for call_id, name in call_id_to_name.items()
        }
    itype = item.get("type", "")
    if itype == "message":
        _render_message_history_item(item, host, fmt)
    elif itype == "function_call":
        _render_function_call_history_item(item, host, fmt)
    elif itype == "function_call_output":
        _render_function_call_output_history_item(
            item,
            host,
            fmt,
            call_id_to_name,
            call_id_to_tool_metadata,
        )
    elif itype == "reasoning":
        _render_reasoning_history_item(item, host, fmt)
    elif itype == "slash_command":
        _render_slash_command_history_item(item, host, fmt)


# ── Slash-command autocomplete ───────────────────────────


# Hidden from the popup so it doesn't show duplicate rows for the
# same handler — ``/help`` already lists the canonical names.
_SLASH_COMMAND_ALIASES: frozenset[str] = frozenset({"/?", "/exit"})


# A skill name must read as a slash-command token: an alphanumeric start then
# word chars, ``:`` (Claude ``plugin:skill``), or ``-`` (Cursor ``plugin--skill``).
# Rejects whitespace, ``/``, and control characters. Mirrors the web composer's
# SLASH_COMMAND_RE so the terminal and the menu agree on what is a command.
_SKILL_COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9][\w:-]*$")


def register_skill_commands(skills: list[SkillSpec]) -> list[str]:
    """
    Auto-register each discovered skill as a REPL slash command.

    For every :class:`SkillSpec` whose ``/<name>`` does not collide
    with an existing built-in command, a handler is added to the
    global :data:`COMMANDS` registry. Collisions are skipped with a
    warning log so built-in commands always win.

    Skills marked ``user-invocable: false`` are skipped — they are
    internal orchestration skills, not user-typeable slash commands, so
    they must not appear in the REPL's slash-command/autocomplete surface
    (the same contract the web composer menu honors). The skill stays
    loadable by the agent itself; only the user-facing command is hidden.

    :param skills: The agent's parsed skill list.
    :returns: List of registered command names (e.g. ``["/code-review"]``).
        Callers should pass this to :func:`unregister_skill_commands`
        on exit to prevent leaking into subsequent ``run_repl`` calls.
    """
    registered: list[str] = []
    for skill in skills:
        if not skill.user_invocable:
            continue
        if not _SKILL_COMMAND_NAME_RE.match(skill.name):
            # A name with whitespace, ``/``, or control chars yields an
            # uninvocable or colliding command — skip + warn rather than
            # register garbage. Mirrors the web composer's SLASH_COMMAND_RE.
            _log.warning(
                "Skill %r skipped: name is not a valid slash-command token",
                skill.name,
            )
            continue
        cmd_name = f"/{skill.name}"
        if cmd_name in COMMANDS:
            _log.warning(
                "Skill %r skipped: /%s collides with a built-in command",
                skill.name,
                skill.name,
            )
            continue

        def _make_handler(sk: SkillSpec) -> SlashCommandHandler:
            """Build a slash-command handler for a single skill."""

            async def _skill_handler(
                arg: str,
                session: Session,
                client: OmnigentClient,  # noqa: ARG001 — dispatch-contract params
                host: TerminalHost,
                fmt: RichBlockFormatter,
            ) -> None:
                send_skill = getattr(session, "send_skill_slash_command", None)
                if not callable(send_skill):
                    raise RuntimeError("Skill slash commands require the sessions API adapter")
                if arg:
                    host.output(Text.from_markup(f"  [{fmt.muted}]/{sk.name}[/{fmt.muted}]"))
                    host.output(Text.from_markup(""))
                    host.output(fmt.user_message(arg))
                else:
                    host.output(
                        Text.from_markup(f"  [{fmt.muted}]Loading skill {sk.name}…[/{fmt.muted}]")
                    )
                host.start_timer()
                await asyncio.sleep(0)
                async for _ in send_skill(sk.name, arg):
                    pass

            return _skill_handler

        handler = _make_handler(skill)
        COMMANDS[cmd_name] = (skill.description, handler)
        registered.append(cmd_name)
        _log.debug("Registered skill slash command: %s", cmd_name)

    return registered


def unregister_skill_commands(names: list[str]) -> None:
    """Remove previously registered skill commands from the global registry."""
    for name in names:
        COMMANDS.pop(name, None)


class _SlashCommandCompleter(Completer):
    """
    Suggest registered slash commands as the user types.

    Trigger conditions are kept parallel to the dispatcher in
    :func:`run_repl.on_input` so the popup only fires when the
    typed text would actually be routed as a command. Suggestions
    come from :data:`COMMANDS` at call time (not import time), so
    new ``@_cmd`` registrations appear without rewiring.
    """

    def get_completions(
        self,
        document: Document,
        complete_event: CompleteEvent,  # noqa: ARG002 — Completer protocol contract
    ) -> Iterable[Completion]:
        """
        Yield :class:`Completion` entries matching the current input.

        :param document: prompt-toolkit's input-buffer view; this
            method only reads ``document.text_before_cursor``
            (e.g. ``"/he"``).
        :param complete_event: prompt-toolkit trigger metadata
            (manual vs. while-typing). Unused — the popup is cheap
            so we always return the same set.
        :returns: One :class:`prompt_toolkit.completion.Completion`
            per matching slash command, in :data:`COMMANDS` order.
        """
        text_before = document.text_before_cursor
        # Trigger only inside the first token: ``hey /tmp`` is chat,
        # not a command. Path-like ``/Users/foo`` is parallel with
        # the dispatcher's guard in :func:`run_repl.on_input`.
        if " " in text_before or "\n" in text_before:
            return
        if not text_before.startswith("/"):
            return
        if "/" in text_before[1:]:
            return

        prefix = text_before.lower()
        for name, (desc, _) in COMMANDS.items():
            if name in _SLASH_COMMAND_ALIASES:
                continue
            if not name.startswith(prefix):
                continue
            yield Completion(
                text=name,
                # Replace everything typed so far so the splice
                # produces ``/help``, not ``//help``.
                start_position=-len(text_before),
                display=name,
                display_meta=desc,
            )


async def handle_slash_command(
    line: str,
    session: Session,
    client: OmnigentClient,
    host: TerminalHost,
    fmt: RichBlockFormatter,
) -> None:
    """
    Dispatch a slash command from the registry.

    :param line: Raw user input line, e.g. ``"/switch"``.
    :param session: Current REPL session.
    :param client: Agent-plane client used by command handlers.
    :param host: Terminal host used for rendering command output.
    :param fmt: Formatter carrying the REPL style names.
    :returns: None.
    """
    from rich.text import Text

    parts = line.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    entry = COMMANDS.get(cmd)
    if entry:
        _, handler = entry
        try:
            await handler(arg, session, client, host, fmt)
        except Exception as exc:  # noqa: BLE001
            # Slash commands run in prompt-toolkit background tasks, so
            # render failures inline instead of letting asyncio log an
            # unretrieved task exception.
            _log.exception("Slash command failed: %s", cmd)
            host.output(Text(f"  Error: {exc}", style="bold red"))
    else:
        host.output(
            Text.from_markup(
                f"  [{fmt.muted}]Unknown command: {cmd} · /help for list[/{fmt.muted}]"
            )
        )
