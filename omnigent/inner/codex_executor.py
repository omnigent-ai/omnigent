"""CodexExecutor: run agents through the Codex App Server.

This executor keeps one long-lived ``codex app-server`` subprocess per
Omnigent session, persists the Codex thread across turns, and exposes
Omnigent tools to Codex as App Server ``dynamicTools``.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    MutableMapping,
    Sequence,
)
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeAlias, cast

from omnigent import _native_forwarder_health as native_forwarder_health
from omnigent import model_catalog
from omnigent._platform import resolve_cli_binary
from omnigent.codex_model_vocabulary import (
    EXTENDED_CATALOG_MODELS,
    EXTENDED_MODEL_DEFAULT_EFFORT,
    EXTENDED_MODEL_EFFORTS,
)
from omnigent.inner.agent_env import clean_agent_env, declared_passthrough
from omnigent.llms._usage_observer import notify_from_dict as _notify_usage_from_dict
from omnigent.model_fallbacks import CODEX_CATALOG_CLONE_SOURCE_SLUG, CODEX_DEFAULT_MODEL
from omnigent.reasoning_effort import CODEX_EFFORTS, EFFORT_ALIASES, validate_effort
from omnigent.spec.types import RetryPolicy

from . import _proc
from ._subprocess_lifecycle import close_subprocess_transport
from .async_utils import run_sync_on_thread
from .codex_goal_command import goal_objective_from_content as _goal_objective_from_content
from .databricks_executor import (
    _databricks_gateway_host,
)
from .datamodel import OSEnvSandboxSpec, OSEnvSpec
from .executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ReasoningChunk,
    TextChunk,
    ToolArgs,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    ToolSpec,
    TurnComplete,
    classify_tool_result,
    describe_exception,
)
from .hook_scripts.subagent_router import HOOK_TIMEOUT_HEADROOM_S as _ROUTER_HOOK_HEADROOM_S
from .hook_scripts.subagent_router import REQUEST_TIMEOUT_S as _ROUTER_REQUEST_TIMEOUT_S

logger = logging.getLogger(__name__)

# Default auth-token refresh cadence (ms) for the vendor-neutral gateway
# transport when ``HARNESS_CODEX_GATEWAY_AUTH_REFRESH_INTERVAL_MS`` is unset.
# Not Databricks-specific: the same fallback applies to any gateway producer
# (Databricks AI gateway or a generic key/gateway provider).
_GATEWAY_AUTH_REFRESH_MS = 900_000

# ---------------------------------------------------------------------------
# Type aliases for JSON-shaped Codex App Server boundaries
# ---------------------------------------------------------------------------

# Each line of the Codex App Server JSONL protocol: envelope with optional
# ``id`` / ``method`` / ``params`` / ``result`` / ``error`` keys. The inner
# field shapes vary per method and are the Codex CLI's contract, not ours.
CodexMessage: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# ``params`` payload inside a Codex message — free-form JSON keyed by string.
CodexParams: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# Normalised tool result dict. Most handlers return ``{"result": ...}`` or
# ``{"error": ...}``; the inner value can be any JSON-shaped payload.
CodexToolResult: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# Content passed to ``enqueue_message`` — arbitrary user-supplied payload.
CodexEnqueuedContent: TypeAlias = Any  # type: ignore[explicit-any]

# Tool-server callback provided by ``Session._wire_sdk_executor``. Takes a
# tool name + args dict and returns either a result dict directly or a
# coroutine yielding one.
CodexToolExecutor: TypeAlias = Callable[
    [str, ToolArgs],
    Awaitable[CodexToolResult] | CodexToolResult,
]

# When the app-server is silent for this long we emit a warning,
# but keep waiting — a long-running tool or model call can legitimately
# block events far longer than any fixed deadline.
_TURN_EVENT_WARN_SECONDS = 600.0
# The idle wait polls on this shorter interval so a fatal gateway error the
# stderr loop sets mid-wait is acted on promptly, rather than only when the
# 600s warn window elapses. Chosen to divide the warn window evenly so the
# warning cadence is unchanged.
_TURN_EVENT_POLL_SECONDS = 5.0
_TURN_COMPLETED_DRAIN_SECONDS = 1.0
# Wall-clock budget for the ``codex --version`` probe. A broken codex
# build that blocks (e.g. on stdin) must not stall session startup — on
# timeout the probe kills the process and reports the version as unknown.
_CODEX_VERSION_PROBE_TIMEOUT_SECONDS = 5.0
_STDERR_CHUNK_LIMIT = 65536
_STREAM_READ_CHUNK_SIZE = 65536
# Files symlinked from the real CODEX_HOME into the per-session temp home.
# Symlinks (not copies) so credential refreshes in the real home propagate
# to running sessions without any action from Omnigent.
_CODEX_HOME_SYMLINK_FILES = ("auth.json",)
_CODEX_HOME_GLOBAL_INSTRUCTION_FILES = ("AGENTS.md", "AGENTS.override.md", "hooks.json")
# Name of the hooks file inside a CODEX_HOME. Symlinked from the user's home
# by default; generated as a merged regular file when subagent routing is on.
_CODEX_HOOKS_FILENAME = "hooks.json"

# Files copied (not symlinked) from the real CODEX_HOME into the per-session
# temp home. config.toml is intentionally copied so that an in-TUI ``/model``
# command writes only to the session's own private copy and never touches the
# shared ``~/.codex/config.toml``. This keeps model selection and cost-policy
# enforcement isolated between concurrent sessions.
_CODEX_HOME_COPY_FILES = ("config.toml",)
# Directories symlinked (not copied) from the real CODEX_HOME into the
# per-session temp home. ``plugins/cache`` is codex's content-addressed
# (versioned) plugin store — read-only reference data that codex would
# otherwise re-materialize tens of MB into every private home. Sharing the
# one real cache dedupes it across sessions; codex's own writes land in the
# shared cache exactly as they would without the private home.
_CODEX_HOME_SYMLINK_DIRS = (Path("plugins") / "cache",)
_CODEX_MINIMAL_CONFIG_ENV = "HARNESS_CODEX_MINIMAL_CONFIG"
_CODEX_PROVIDER_CONFIG_PREFIX = "model_providers."

# Environment variables explicitly excluded from the codex subprocess even
# when their prefix is in the allowlist. ``OPENAI_API_KEY`` is stripped so
# the codex CLI uses its subscription auth (``auth.json``) rather than a
# developer API key that would charge separately.
_CODEX_ENV_DENY_EXACT: frozenset[str] = frozenset({"OPENAI_API_KEY"})

# The codex CLI logs a rejected gateway request to stderr as
# ``unexpected status <code> <reason>: {...}, url: <url>`` and precedes it with
# ``Reconnecting... N/5`` retry lines. These parse that shape so the head can
# attribute the real gateway error to a turn that otherwise emits no events.
_CODEX_STDERR_STATUS_RE = re.compile(
    r"unexpected status (?P<code>\d{3})(?:\s+(?P<reason>[A-Za-z][A-Za-z ]*?))?\s*[:,]"
)
_CODEX_STDERR_URL_RE = re.compile(r"url:\s*(?P<url>\S+)")
_CODEX_STDERR_RETRY_EXHAUSTED_RE = re.compile(
    r"Reconnecting\.{0,3}\s*(?P<n>\d+)\s*/\s*(?P<total>\d+)"
)
# HTTP statuses that are not transient — a retry can never fix them, so the
# head fails the turn fast instead of riding the full idle watchdog.
_CODEX_STDERR_FATAL_STATUSES: frozenset[int] = frozenset({401, 403})


class _CodexGatewayError:
    """A parsed gateway rejection read off the codex CLI's stderr.

    ``code`` is the HTTP status; ``fatal`` marks an auth-class status that a
    retry cannot fix (the turn should fail fast rather than stall).
    """

    __slots__ = ("code", "fatal", "reason", "url")

    def __init__(self, code: int, reason: str | None, url: str | None) -> None:
        self.code = code
        self.reason = reason
        self.url = url
        self.fatal = code in _CODEX_STDERR_FATAL_STATUSES

    def detail(self, *, model: str | None = None) -> str:
        """A concise, actionable one-line cause for the turn-failure message."""
        reason = f" {self.reason}" if self.reason else ""
        target = f" for {model}" if model else ""
        where = f" at {self.url}" if self.url else ""
        hint = " (auth likely expired/misconfigured)" if self.fatal else ""
        return f"gateway returned {self.code}{reason}{target}{where}{hint}"


def _parse_codex_gateway_error(line: str) -> _CodexGatewayError | None:
    """Return a parsed gateway rejection from a codex stderr *line*, else None.

    Only lines carrying an ``unexpected status <code>`` shape are classified;
    ordinary stderr (including the ``Reconnecting`` retry lines) returns None.
    """
    match = _CODEX_STDERR_STATUS_RE.search(line)
    if match is None:
        return None
    code = int(match.group("code"))
    raw_reason = match.group("reason")
    reason = raw_reason.strip() if raw_reason else None
    url_match = _CODEX_STDERR_URL_RE.search(line)
    url = url_match.group("url").rstrip(",") if url_match else None
    return _CodexGatewayError(code, reason, url)


def _extract_codex_last_turn_usage(params: object, model: str | None) -> dict[str, object] | None:
    """Map a ``thread/tokenUsage/updated`` payload's ``last`` breakdown
    onto the wire shape that :class:`TurnComplete` consumes.

    Codex's ``inputTokens`` is INCLUSIVE of cached tokens (codex-rs:
    ``non_cached_input = input_tokens - cached_input_tokens``), and
    :func:`compute_llm_cost` expects ``input_tokens`` to be the *non-cached*
    portion with cache reads priced separately. So split ``cachedInputTokens``
    out into ``cache_read_input_tokens`` and keep only the remainder in
    ``input_tokens`` — otherwise cached tokens are billed at the full input
    rate. Mirrors the codex-native forwarder split.

    :param model: The resolved model this turn ran with, e.g.
        ``"gpt-5.4-mini"`` — the ``run_turn`` argument, which carries the
        harness/provider's resolved default even when the agent spec pins
        none. Stamped into the returned usage as ``"model"`` so the
        server's per-model cost/token attribution (``session_usage.by_model``)
        can key on it instead of silently falling back to an unresolvable
        spec model and dropping the turn from the per-model breakdown
        (mirrors ``claude_sdk_executor``'s ``observed_model`` and every
        other relay executor, all of which already report ``usage.model``).
    """
    if not isinstance(params, dict):
        return None
    token_usage = params.get("tokenUsage")
    if not isinstance(token_usage, dict):
        return None
    last = token_usage.get("last")
    if not isinstance(last, dict):
        return None
    input_total = int(last.get("inputTokens") or 0)
    # Clamp so a malformed cached > total never makes input_tokens negative.
    cached = min(int(last.get("cachedInputTokens") or 0), input_total)
    usage: dict[str, object] = {
        "input_tokens": input_total - cached,  # non-cached portion
        "output_tokens": int(last.get("outputTokens") or 0),
        "total_tokens": int(last.get("totalTokens") or 0),
    }
    if cached:
        usage["cache_read_input_tokens"] = cached
    if model:
        usage["model"] = model
    return usage


def _format_codex_error_params(params: object) -> str:
    """
    Format a Codex App Server JSON-RPC error frame's ``params`` dict
    into a single-line diagnostic string.

    JSON-RPC error frames carry ``code``, ``message``, and ``data``
    fields. The codex CLI's app-server populates them inconsistently
    — some failure modes leave ``message`` empty and only set
    ``code`` + ``data``, which used to surface to the user as the
    bare fallback string ``"Codex App Server error"`` with no clue
    why. This helper emits whatever populated fields are present so
    config mismatches (Claude model on a codex harness, bad
    profile, provider HTTP errors, etc.) leave a useful trail.

    :param params: The ``params`` value from the JSON-RPC error
        frame. Expected to be a dict, but defensive against
        non-dict shapes (codex's protocol allows ``None`` or
        scalars in some edge paths).
    :returns: A single-line human-readable summary, e.g.
        ``"Bad model 'foo'; code=invalid_argument; data='details'"``.
        Falls back to ``"Codex App Server error (no params)"`` only
        when ``params`` is empty / not a dict — never returns a
        message that makes the user re-grep the codebase to find
        out what failed.
    """
    if not isinstance(params, dict) or not params:
        return "Codex App Server error (no params)"
    parts: list[str] = []
    message = params.get("message")
    if isinstance(message, str) and message.strip():
        parts.append(message.strip())
    # The app server's ``error`` events nest the actual upstream
    # failure under ``params["error"]`` (a dict with its own
    # ``message`` / ``codexErrorInfo`` / ``additionalDetails``
    # fields). The inner message is sometimes a stringified JSON
    # blob from the provider — try to parse it so the human-readable
    # ``message`` field surfaces; otherwise fall back to the raw
    # string. This is the path that turns a bare "Codex App Server
    # error" into something like "Responses API passthrough is not
    # supported for model X".
    inner = params.get("error")
    if isinstance(inner, dict):
        inner_message = inner.get("message")
        if isinstance(inner_message, str) and inner_message.strip():
            parts.append(_unwrap_provider_error_json(inner_message.strip()))
        inner_info = inner.get("codexErrorInfo")
        if isinstance(inner_info, str) and inner_info.strip() and inner_info != "other":
            parts.append(f"codexErrorInfo={inner_info}")
        inner_details = inner.get("additionalDetails")
        if inner_details:
            parts.append(f"details={inner_details!r}")
    code = params.get("code")
    if code is not None:
        parts.append(f"code={code}")
    data = params.get("data")
    if data is not None and data != "":
        parts.append(f"data={data!r}")
    if not parts:
        # No standard JSON-RPC fields populated; dump the raw params
        # so the user can still see what codex sent.
        return f"Codex App Server error: raw_params={params!r}"
    return "; ".join(parts)


def _unwrap_provider_error_json(text: str) -> str:
    """
    Try to JSON-parse *text* and return its ``message`` field.

    Codex relays provider HTTP errors as a stringified JSON blob
    (e.g. ``'{"error_code":"BAD_REQUEST","message":"..."}'`` from
    Databricks gateway). Returning the raw string is technically
    accurate but visually noisy; extracting the human-readable
    ``message`` field gives the user the actionable line directly.

    :param text: The candidate string. Expected to be either a JSON
        object with a ``message`` field, or a plain error string
        (e.g. ``"connection refused"``).
    :returns: The extracted ``message`` field if *text* parses as
        JSON and contains one; otherwise *text* unchanged.
    """
    import json

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text
    if isinstance(parsed, dict):
        # Provider-style: ``{"error_code": ..., "message": "..."}``.
        provider_message = parsed.get("message")
        if isinstance(provider_message, str) and provider_message.strip():
            error_code = parsed.get("error_code") or parsed.get("code")
            if error_code:
                return f"{provider_message.strip()} (error_code={error_code})"
            return provider_message.strip()
    return text


class _Process(Protocol):
    """Subset of ``asyncio.subprocess.Process`` we touch in process-tree helpers.

    Declaring a Protocol lets these helpers accept both real processes and
    test-supplied fakes without falling back on ``getattr(..., "<literal>")``
    to dodge mypy's attr-defined check. Properties (not bare attributes) to
    match ``asyncio.subprocess.Process`` where these are read-only.
    """

    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...

    def terminate(self) -> None: ...
    def kill(self) -> None: ...


def _terminate_process_tree(process: _Process | None) -> None:
    _proc.terminate_tree(process)


def _kill_process_tree(process: _Process | None) -> None:
    _proc.kill_tree(process)


# Env override for an explicit codex binary, mirroring goose's
# OMNIGENT_GOOSE_PATH. Set this when codex lives on a PATH the host
# daemon doesn't inherit (e.g. an nvm-managed global bin dir).
_CODEX_PATH_ENV = "OMNIGENT_CODEX_PATH"


def _find_codex_cli() -> str | None:
    """Resolve the ``codex`` CLI binary (override → ``PATH`` → global dirs)."""
    return resolve_cli_binary("codex", env_var=_CODEX_PATH_ENV)


async def _codex_cli_version(codex_path: str) -> tuple[int, int, int] | None:
    """
    Return the codex CLI version as a ``(major, minor, patch)`` tuple.

    Runs ``codex --version`` and parses the numeric core of its
    ``codex-cli X.Y.Z`` output. A pre-release suffix (e.g.
    ``0.132.0-alpha.1``) is ignored — only ``X.Y.Z`` is parsed — so an
    alpha of a supported release still compares as that release.

    :param codex_path: Path to the codex CLI, e.g.
        ``"/usr/local/bin/codex"``.
    :returns: The parsed version, e.g. ``(0, 136, 0)``, or ``None`` if
        the command cannot be run, times out, or its output has no
        ``X.Y.Z`` token (caller treats ``None`` as "version unknown", not
        "too old").
    """
    try:
        proc = await _create_subprocess_exec(
            codex_path,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=_CODEX_VERSION_PROBE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        # A hung `codex --version` must not block session startup: kill it
        # and report the version as unknown (the caller proceeds).
        with suppress(ProcessLookupError):
            proc.kill()
        with suppress(Exception):
            await proc.wait()
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", stdout.decode("utf-8", errors="replace"))
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


_ProcessPath: TypeAlias = str | bytes | os.PathLike[str] | os.PathLike[bytes]


async def _create_subprocess_exec(
    program: _ProcessPath,
    *args: _ProcessPath,
    stdin: int | None = None,
    stdout: int | None = None,
    stderr: int | None = None,
    env: Mapping[str, str] | Mapping[bytes, bytes] | None = None,
    cwd: _ProcessPath | None = None,
    start_new_session: bool = False,
    creationflags: int = 0,
) -> asyncio.subprocess.Process:
    """Start a subprocess through the module-local test seam."""
    return await asyncio.create_subprocess_exec(
        program,
        *args,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        env=env,
        cwd=cwd,
        start_new_session=start_new_session,
        creationflags=creationflags,
    )


def _clean_codex_env(extra_allow: Iterable[str] = ()) -> dict[str, str]:
    """
    Build a filtered copy of ``os.environ`` for the codex subprocess.

    Thin wrapper over :func:`omnigent.inner.agent_env.clean_agent_env`; the
    families below are codex's own on top of the shared safe base. Keys in
    :data:`_CODEX_ENV_DENY_EXACT` are excluded even when their prefix matches;
    ``OPENAI_API_KEY`` is stripped so the codex CLI falls back to subscription
    auth (``auth.json``) rather than a developer API key that would charge
    separately.

    The filtered dict is also the executor's own view of its launch, not just
    the subprocess env: the app-server session reads Omnigent's per-session
    codex signals back out of it, so those names have to survive the filter
    (see :data:`_CODEX_OMNIGENT_LAUNCH_ENV_VARS`).

    :returns: Filtered environment dict.
    """
    return clean_agent_env(
        allow_prefixes=("OPENAI_", "REQUESTS_", "CODEX_HOME"),
        allow_exact=(
            "PYTHONUTF8",
            "DATABRICKS_BEARER",  # explicit CI/integration bearer used by auth.command
            "DATABRICKS_CODEX_TOKEN",  # env_key in ~/.codex/config.toml's DB provider
            *_CODEX_OMNIGENT_LAUNCH_ENV_VARS,
        ),
        deny_exact=_CODEX_ENV_DENY_EXACT,
        extra_allowed=extra_allow,
    )


def codex_skill_sources(bundle_dir: Path | None, home: Path) -> list[Path]:
    """
    Build the ordered Codex skill-source list: bundle skills, then host skills.

    The single source of truth for *where* Codex skills come from, shared
    by :func:`populate_codex_skills_from_bundle` (which symlinks them into
    ``$CODEX_HOME/skills/``) and the slash-command menu's ``codex_host_skills``
    provider — so the linked set and the menu cannot drift on which roots
    are scanned. Priority order: the agent's own ``<bundle>/skills/`` before
    host-installed ``<home>/.codex/skills/`` (a bundled skill shadows a host
    skill of the same name). Only existing directories are returned.

    :param bundle_dir: Materialized agent-bundle root, or ``None``.
    :param home: The user home directory (``Path.home()``); injected so
        tests and the menu provider can pin it.
    :returns: Existing skill-dir roots in priority order.
    """
    sources: list[Path] = []
    if bundle_dir is not None and (bundle_dir / "skills").is_dir():
        sources.append(bundle_dir / "skills")
    host = home / ".codex" / "skills"
    if host.is_dir():
        sources.append(host)
    return sources


def select_codex_skill_dirs(
    skills_filter: str | list[str],
    sources: list[Path],
) -> dict[str, Path]:
    """
    Resolve skill name → directory for a Codex skill source list.

    The single source of truth for "which skills does this Codex session
    expose", shared by :func:`_populate_codex_skills` (which symlinks the
    result into ``$CODEX_HOME/skills/``) and the slash-command menu's
    Codex skill source — so the menu and the actually-linked set cannot
    diverge.

    :param skills_filter: ``"all"`` selects every skill found in
        *sources*; ``"none"`` selects nothing; a ``list[str]`` selects
        only the named skills present in some source. Names not present
        are silently skipped.
    :param sources: Ordered skill-dir roots (each containing
        ``<name>/SKILL.md`` subdirs). The first source that contains a
        given skill name wins.
    :returns: Ordered mapping of selected skill name → absolute dir.
    """
    if skills_filter == "none":
        return {}
    available: dict[str, Path] = {}
    for source in sources:
        if not source.is_dir():
            continue
        try:
            children = sorted(source.iterdir())
        except OSError as exc:
            # An unreadable source (permission denied, races) must not abort
            # skill discovery / session startup — skip it and continue.
            logger.warning("could not list codex skill source %s (%s); skipping", source, exc)
            continue
        for child in children:
            if not child.is_dir():
                continue
            if not (child / "SKILL.md").is_file():
                continue
            available.setdefault(child.name, child)

    if skills_filter == "all":
        names = list(available.keys())
    elif isinstance(skills_filter, list):
        names = [n for n in skills_filter if n in available]
    else:
        return {}
    return {n: available[n] for n in names}


def _populate_codex_skills(
    target_dir: Path,
    skills_filter: str | list[str],
    sources: list[Path],
) -> None:
    """
    Populate *target_dir* with symlinks to skill directories.

    Codex auto-discovers skills under ``$CODEX_HOME/skills/<name>/``.
    Our executor already overrides ``CODEX_HOME`` to a per-conversation
    temp directory (so the user's real ``~/.codex/`` is never touched),
    which means by default Codex sees zero skills. This helper populates
    the temp ``skills/`` subdir based on the agent spec's ``skills:``
    field, sourcing skill directories from ``sources`` (typically the
    user's ``~/.codex/skills/`` plus any ``<bundle>/skills/``). Skill
    selection is delegated to :func:`select_codex_skill_dirs`.

    :param target_dir: ``<temp_codex_home>/skills/`` — the directory
        Codex will scan. Created if it doesn't exist (unless
        ``skills_filter == "none"``, in which case the directory is
        intentionally left absent so Codex sees nothing).
    :param skills_filter: ``"all"`` exposes every skill found in
        *sources*; ``"none"`` exposes none; ``list[str]`` exposes only
        the named skills. Names not present in any source are silently
        skipped — matches the SDK semantics where a missing host skill
        produces no error, just no exposure.
    :param sources: Ordered list of directories to scan. The first
        source that contains a given skill name wins (so callers should
        list bundled skills before host skills if they want bundle
        overrides, or vice versa).
    """
    if skills_filter == "none":
        return

    target_dir.mkdir(parents=True, exist_ok=True)
    selected = select_codex_skill_dirs(skills_filter, sources)

    for name, skill_dir in selected.items():
        link_path = target_dir / name
        if link_path.exists() or link_path.is_symlink():
            continue
        try:
            # Resolve to absolute so the symlink doesn't break when
            # the source was a relative path (relative symlinks resolve
            # against the link's parent, not the original cwd).
            link_path.symlink_to(skill_dir.resolve())
        except OSError as exc:
            # Filesystems without symlink support (e.g. some Windows
            # configs) — fall back to a copy. Don't crash the harness
            # boot over a skill-discovery convenience.
            logger.warning(
                "could not symlink skill %r into %s (%s); copying instead",
                name,
                target_dir,
                exc,
            )
            try:
                shutil.copytree(skill_dir, link_path)
            except OSError as copy_exc:
                # Copy fallback can also fail (unreadable source, race) — skip
                # this one skill rather than abort the whole session boot.
                logger.warning(
                    "could not copy skill %r into %s (%s); skipping",
                    name,
                    target_dir,
                    copy_exc,
                )


def populate_codex_skills_from_bundle(
    codex_home: Path,
    bundle_dir: Path | None,
    skills_filter: str | list[str],
) -> None:
    """
    Populate a CODEX_HOME's ``skills/`` from a bundle + host skills.

    Shared by the wrapped ``codex`` executor and the ``codex-native``
    launch path so both expose the same skill surface. Builds the source
    list in priority order — the agent's own ``<bundle>/skills/`` before
    host-installed ``~/.codex/skills/`` (so a bundled skill shadows a
    host skill of the same name) — and delegates to
    :func:`_populate_codex_skills`, which honours ``skills_filter``
    (``"all"`` / ``"none"`` / list of names).

    :param codex_home: The CODEX_HOME whose ``skills/`` subdir Codex
        scans, e.g. a per-conversation temp dir or the per-bridge native
        home. ``<codex_home>/skills`` is created unless the filter is
        ``"none"``.
    :param bundle_dir: Materialized agent-bundle root, or ``None`` when
        the launch has no bundle. Its ``skills/`` subdir is used as the
        first (highest-priority) source when present.
    :param skills_filter: The spec's ``skills_filter``: ``"all"`` /
        ``"none"`` / a list of skill names.
    :returns: None.
    """
    skill_sources = codex_skill_sources(bundle_dir, Path.home())
    _populate_codex_skills(codex_home / "skills", skills_filter, skill_sources)


def _is_omnigent_private_codex_home(path: Path) -> bool:
    """
    Return whether *path* is an Omnigent-created private ``CODEX_HOME``.

    Omnigent launches Codex with private homes for session state
    isolation. Those homes are not the user's source of truth for auth;
    nested launches must not treat them as the real login directory.

    :param path: Candidate ``CODEX_HOME`` path, e.g.
        ``"/home/user/.omnigent/codex-native/<hash>/codex-home"``.
    :returns: ``True`` when *path* matches a native bridge home or the
        wrapped executor's temporary ``omnigent-codex-home-*`` naming.
    """
    expanded = path.expanduser()
    parts = expanded.parts
    if (
        expanded.name == "codex-home"
        and len(parts) >= 4
        and parts[-3] == "codex-native"
        and parts[-4] == ".omnigent"
    ):
        return True
    return expanded.name.startswith("omnigent-codex-home-")


def _private_codex_home_config_source(path: Path) -> Path | None:
    """
    Infer the original config source from a private Codex home.

    A parent Omnigent launch bridges ``auth.json`` and ``config.toml`` into
    its private home as symlinks. If a nested launch inherits that private
    ``CODEX_HOME``, those symlink targets are the only durable record of a
    custom parent source.

    :param path: Private ``CODEX_HOME`` path, e.g.
        ``"/home/user/.omnigent/codex-native/<hash>/codex-home"``.
    :returns: The shared parent directory of bridged config symlink targets,
        or ``None`` if the private home has no usable source symlink.
    """
    source_dirs: set[Path] = set()
    for filename in _CODEX_HOME_SYMLINK_FILES:
        config_file = path / filename
        if not config_file.is_symlink():
            continue
        with suppress(OSError):
            source_dirs.add(config_file.resolve().parent)
    if len(source_dirs) == 1:
        return next(iter(source_dirs))
    return None


def _resolve_codex_home_config_source(source_dir: Path, home_codex_home: Path) -> Path:
    """
    Resolve the single Codex home to read auth/config from.

    User-supplied ``CODEX_HOME`` remains authoritative, except when it
    points at an Omnigent private home from a parent session. In that
    nested case, the private home is session state, not the login source,
    so the user's normal ``~/.codex`` directory is the intended source.

    :param source_dir: Primary candidate from ``CODEX_HOME`` or
        ``home_codex_home``, e.g. ``Path("/custom/codex")``.
    :param home_codex_home: The user's default Codex home, e.g.
        ``Path.home() / ".codex"``.
    :returns: Exactly one source directory to use for both
        ``auth.json`` and ``config.toml``.
    """
    if source_dir != home_codex_home and _is_omnigent_private_codex_home(source_dir):
        private_source = _private_codex_home_config_source(source_dir)
        if private_source is not None:
            return private_source
        return home_codex_home
    return source_dir


def _codex_home_config_source_from_env() -> Path:
    """
    Return the Codex home whose auth/config should be bridged.

    Codex stores subscription login state in ``CODEX_HOME``. Omnigent
    launches Codex with private per-session homes for isolation, then bridges
    only auth/config from the user's source home. Nested Omnigent processes
    can inherit a parent private home, so this resolver maps that specific
    inherited session-state home back to the user's default ``~/.codex``.

    :returns: Host Codex home to read ``auth.json`` and ``config.toml`` from,
        e.g. ``Path.home() / ".codex"`` or an explicit user ``CODEX_HOME``.
    """
    home_codex_home = Path.home() / ".codex"
    return _resolve_codex_home_config_source(
        Path(os.environ.get("CODEX_HOME") or str(home_codex_home)),
        home_codex_home,
    )


def _populate_codex_home_config(
    target_dir: Path,
    source_dir: Path,
    *,
    minimal_config: bool | None = None,
    inject_hooks: bool = False,
    extend_model_catalog: bool = False,
) -> None:
    """
    Bridge user config files from the real ``CODEX_HOME`` into the temp one.

    The executor overrides ``CODEX_HOME`` to a per-conversation temp
    directory so session data (conversation history, etc.) stays isolated
    from the user's ``~/.codex/``. However, the codex CLI also reads
    authentication tokens (``auth.json``), provider configuration
    (``config.toml``) and instructions (``AGENTS.md``, ``AGENTS.override.md``)
    from ``$CODEX_HOME``. This helper bridges those files into the temp directory:

    - ``auth.json`` is **symlinked** so OAuth token refreshes written to
      the real home propagate to running sessions without delay.
    - ``config.toml`` is **copied** so an in-TUI ``/model`` command writes
      only to the session's own private copy and never mutates the shared
      ``~/.codex/config.toml``. This keeps model selection and cost-policy
      enforcement isolated between concurrent sessions. Hook-trust keys inside
      the copy are rewritten to reference the private home's paths so Codex
      recognises previously-trusted hooks without an interactive prompt.
    - ``hooks.json`` is **symlinked** (when present) so the user's hooks are
      available at the same path within the private home that the rewritten
      trust keys reference.
    - ``AGENTS.md``, ``AGENTS.override.md`` are **symlinked** so instructions
      are respected.

    :param target_dir: The per-conversation temp ``CODEX_HOME``
        directory. Must already exist.
    :param source_dir: The primary ``CODEX_HOME`` directory
        (typically ``$CODEX_HOME`` or ``~/.codex``). Missing files are
        skipped.
    :param minimal_config: Copy only auth and provider-routing config when
        ``True``. ``None`` preserves the environment-controlled behavior.
    :param inject_hooks: Skip the ``hooks.json`` symlink because the caller
        generates a merged regular file (user hooks + Omnigent hooks) at that
        path instead — see :func:`write_codex_hooks_file`. Left ``False`` when
        no hooks are injected, so the user's file stays symlinked and a
        mid-session edit to it still takes effect.
    :param extend_model_catalog: Replace codex's bundled model catalog with
        its own catalog plus the gateway-only arms. Costs a ``codex debug
        models`` probe, so it is reserved for Smart Routing sessions whose
        turns/spawns can land on such an arm.
    """
    if not source_dir.is_dir():
        return

    if minimal_config is None:
        minimal_config = os.environ.get(_CODEX_MINIMAL_CONFIG_ENV, "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
    symlink_files: tuple[str, ...] = _CODEX_HOME_SYMLINK_FILES
    if not minimal_config:
        symlink_files += _CODEX_HOME_GLOBAL_INSTRUCTION_FILES
    if inject_hooks:
        # The generated hooks file owns this path — a symlink to the user's
        # home would either shadow it or (worse) be written through.
        symlink_files = tuple(name for name in symlink_files if name != _CODEX_HOOKS_FILENAME)
    for filename in symlink_files:
        source_file = source_dir / filename
        if not source_file.is_file():
            continue
        link_path = target_dir / filename
        if link_path.exists() or link_path.is_symlink():
            continue
        try:
            link_path.symlink_to(source_file)
        except OSError as exc:
            logger.warning(
                "could not symlink %r into %s (%s); copying instead",
                filename,
                target_dir,
                exc,
            )
            shutil.copy2(source_file, link_path)

    if not minimal_config:
        for reldir in _CODEX_HOME_SYMLINK_DIRS:
            source_subdir = source_dir / reldir
            if not source_subdir.is_dir():
                continue
            link_path = target_dir / reldir
            if link_path.exists() or link_path.is_symlink():
                continue
            link_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                link_path.symlink_to(source_subdir.resolve())
            except OSError as exc:
                # Symlink unsupported (some Windows configs) or a race — skip
                # the dedupe rather than abort session boot; codex re-populates
                # its own private cache, costing disk but staying correct.
                logger.warning(
                    "could not symlink %r into %s (%s); codex will repopulate it",
                    str(reldir),
                    target_dir,
                    exc,
                )

    for filename in _CODEX_HOME_COPY_FILES:
        source_file = source_dir / filename
        if not source_file.is_file():
            continue
        dest_path = target_dir / filename
        if dest_path.exists() or dest_path.is_symlink():
            continue
        if minimal_config and filename == "config.toml":
            import tomlkit

            # The title worker needs custom-provider routing, but copying the
            # full user config also starts unrelated MCPs/plugins and can exceed
            # its timeout. auth.json alone cannot supply these provider tables.
            source_config = tomlkit.parse(source_file.read_text())
            minimal_document = tomlkit.document()
            for key in ("model_provider", "model_providers", "profiles"):
                if key in source_config:
                    minimal_document[key] = source_config[key]
            dest_path.write_text(tomlkit.dumps(minimal_document))
            continue
        shutil.copy2(source_file, dest_path)
        if filename == "config.toml":
            _normalize_copied_codex_effort(dest_path)
            if extend_model_catalog:
                # Routed turns and spawns can land on an arm codex's bundled
                # catalog has no entry for, which it then refuses client-side.
                catalog_path = write_codex_model_catalog(
                    target_dir, codex_path=_find_codex_cli(), source_home=source_dir
                )
                if catalog_path is not None:
                    set_codex_model_catalog_path(dest_path, catalog_path)


def materialize_codex_provider_config(
    codex_home: Path,
    config_overrides: Iterable[str],
) -> list[str]:
    """Move generated provider definitions into a private Codex config.

    Codex accepts provider tables through ``-c``/``--config``, but those
    tables can contain static credentials or credential-bearing auth
    commands. Persist them in the session-owned ``config.toml`` instead so
    process arguments contain only non-secret routing and behavior overrides.

    :param codex_home: Private session ``CODEX_HOME`` directory.
    :param config_overrides: Pending Codex config override strings.
    :returns: Overrides safe to retain in subprocess arguments.
    """
    codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(codex_home, 0o700)
    config_path = codex_home / "config.toml"

    provider_overrides: list[str] = []
    argv_overrides: list[str] = []
    for override in config_overrides:
        if override.lstrip().startswith(_CODEX_PROVIDER_CONFIG_PREFIX):
            provider_overrides.append(override)
        else:
            argv_overrides.append(override)
    if not provider_overrides:
        if config_path.is_file() and not config_path.is_symlink():
            os.chmod(config_path, 0o600)
        return argv_overrides

    import tomlkit

    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    document = tomlkit.parse(existing) if existing else tomlkit.document()
    providers = document.get("model_providers")
    if providers is None:
        document["model_providers"] = tomlkit.table()
        providers = document["model_providers"]
    if not isinstance(providers, MutableMapping):
        raise ValueError("Codex model_providers config must be a TOML table")

    for override in provider_overrides:
        fragment = tomlkit.parse(override)
        generated = fragment.get("model_providers")
        if not isinstance(generated, MutableMapping) or not generated:
            raise ValueError("Codex provider override must define model_providers")
        for provider_name, provider_config in generated.items():
            providers[provider_name] = provider_config

    fd, tmp_name = tempfile.mkstemp(prefix="config.toml.", dir=str(codex_home))
    try:
        os.chmod(tmp_name, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(tomlkit.dumps(document))
        os.replace(tmp_name, config_path)
        os.chmod(config_path, 0o600)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(tmp_name)
    return argv_overrides


# Bridge directory holding the ``subagent_router.json`` advertisement. Its
# presence in the codex process env is what turns generated routing hooks on:
# without an endpoint to ask there is nothing to enforce, so the user's
# ``hooks.json`` keeps being symlinked untouched.
CODEX_ROUTER_DIR_ENV_VAR = "OMNIGENT_CODEX_SUBAGENT_ROUTER_DIR"
# Session the spawns belong to, baked into the generated hook commands.
CODEX_ROUTER_SESSION_ID_ENV_VAR = "OMNIGENT_CODEX_SUBAGENT_ROUTER_SESSION_ID"
_CODEX_ROUTER_HOOK_MODULE = "omnigent.inner.hook_scripts.codex_router_hook"
# Codex flattens the spawn tool name (``collaborationspawn_agent`` on
# 0.145.x), so the matcher is a regex suffix and never a bare literal.
_CODEX_SPAWN_AGENT_MATCHER = r".*spawn_agent"
# Kept just above the hook's own request budget so codex's kill is the
# outermost bound: the hook fails open on its timeout, codex only steps in
# if the hook itself wedged.
_CODEX_ROUTER_HOOK_TIMEOUT_SECONDS = int(_ROUTER_REQUEST_TIMEOUT_S + _ROUTER_HOOK_HEADROOM_S)
# Codex release the ``PreToolUse`` spawn gate is verified against. Older CLIs
# spell the flattened spawn tool name differently (or lack ``PreToolUse``
# entirely), so the matcher above never fires and routing silently no-ops.
# Checked here, at the registration site, rather than as a launch floor: an
# older codex must still launch, just without the spawn gate.
_CODEX_ROUTING_HOOK_MIN_VERSION = (0, 145, 0)


def codex_routing_hook_skip_reason(codex_cli_version: tuple[int, int, int] | None) -> str | None:
    """
    Explain why the routing spawn gate cannot be registered, if it cannot.

    An unparseable version (``None``) counts as supported, matching the
    policy-hook gate: a flaky ``codex --version`` probe must not silently
    drop routing when the CLI is probably new enough.

    :param codex_cli_version: Parsed ``codex --version``, e.g. ``(0, 139, 0)``.
    :returns: A log-ready reason, or ``None`` when the hook may be registered.
    """
    if codex_cli_version is None or codex_cli_version >= _CODEX_ROUTING_HOOK_MIN_VERSION:
        return None
    spelled = ".".join(str(part) for part in codex_cli_version)
    minimum = ".".join(str(part) for part in _CODEX_ROUTING_HOOK_MIN_VERSION)
    return (
        f"codex {spelled} predates PreToolUse hooks (need >= {minimum}); "
        "smart routing spawn gate disabled"
    )


def _codex_router_hook_command(
    subcommand: str,
    bridge_dir: Path,
    *,
    session_id: str | None,
    python_executable: str | None,
    extra_args: Iterable[str] = (),
) -> str:
    """
    Build the shell command codex runs for one routing hook event.

    Runs python in isolated mode (``-I``). Codex executes hooks with the
    session's workspace as cwd, and ``-m`` would otherwise put that
    workspace first on ``sys.path``: a workspace containing a directory
    named ``omnigent`` (any checkout of this project) shadows the installed
    package, the hook dies on ``ModuleNotFoundError``, and codex discards
    the failure — the routing gate silently fails open.

    :param subcommand: Hook-script subcommand, e.g. ``"route-subagent"``.
    :param bridge_dir: Session bridge directory holding the router
        advertisement.
    :param session_id: Omnigent session id, or ``None`` when the
        advertisement is expected to carry it.
    :param python_executable: Python to run; ``None`` uses
        :data:`sys.executable`.
    :param extra_args: Extra flags, e.g. ``("--harness", "codex-native")``.
    :returns: A shell-escaped command string.
    """
    argv = [
        python_executable or sys.executable,
        "-I",
        "-m",
        _CODEX_ROUTER_HOOK_MODULE,
        subcommand,
        "--bridge-dir",
        str(bridge_dir),
    ]
    if session_id:
        argv.extend(["--session-id", session_id])
    argv.extend(extra_args)
    return shlex.join(argv)


def codex_router_hooks_settings(
    bridge_dir: Path,
    *,
    session_id: str | None = None,
    harness: str = "codex",
    python_executable: str | None = None,
) -> dict[str, Any]:
    """
    Build the Omnigent half of a routing ``hooks.json`` payload.

    One event: a ``PreToolUse`` gate on the spawn tool (matched by regex
    because codex flattens the name) that asks the runner which model the
    spawn may use and rewrites / denies accordingly.

    :param bridge_dir: Session bridge directory.
    :param session_id: Omnigent session id baked into the commands.
    :param harness: Harness label sent to the endpoint, e.g. ``"codex"``.
    :param python_executable: Python for the hook commands.
    :returns: A ``hooks.json``-shaped dict.
    """

    def hook(subcommand: str, timeout: int, extra_args: Iterable[str] = ()) -> dict[str, Any]:
        return {
            "type": "command",
            "command": _codex_router_hook_command(
                subcommand,
                bridge_dir,
                session_id=session_id,
                python_executable=python_executable,
                extra_args=extra_args,
            ),
            "timeout": timeout,
        }

    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": _CODEX_SPAWN_AGENT_MATCHER,
                    "hooks": [
                        hook(
                            "route-subagent",
                            _CODEX_ROUTER_HOOK_TIMEOUT_SECONDS,
                            ("--harness", harness),
                        )
                    ],
                }
            ],
        }
    }


def merge_codex_user_hooks(payload: dict[str, Any], user_hooks_path: Path) -> dict[str, Any]:
    """
    Merge the user's ``hooks.json`` entries into a generated payload.

    Omnigent's entries stay in first position per event so the routing
    gate runs before user hooks; events the user declares alone are added
    wholesale. A missing or malformed user file leaves *payload*
    unchanged — routing must not break because the user's hooks file is
    bad.

    :param payload: Payload from :func:`codex_router_hooks_settings`.
    :param user_hooks_path: The user's real ``hooks.json``.
    :returns: The merged payload.
    """
    try:
        user_data = json.loads(user_hooks_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return payload
    user_hooks = user_data.get("hooks", {}) if isinstance(user_data, dict) else {}
    if not isinstance(user_hooks, dict) or not user_hooks:
        return payload
    return merge_codex_hook_payloads([payload, {"hooks": user_hooks}])


def merge_codex_hook_payloads(payloads: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """
    Merge ``hooks.json``-shaped payloads, earlier ones first per event.

    Codex loads exactly one hooks file per ``CODEX_HOME``, so every
    generator (policy hooks, routing hooks, the user's own hooks) has to
    share a single payload; order decides which hook gates first.

    :param payloads: Payloads to merge, most privileged first.
    :returns: The merged payload.
    """
    merged_hooks: dict[str, Any] = {}
    for payload in payloads:
        hooks = payload.get("hooks") or {}
        if not isinstance(hooks, Mapping):
            continue
        for event, entries in hooks.items():
            if not isinstance(entries, list):
                continue
            existing = merged_hooks.get(event)
            merged_hooks[event] = list(existing) + list(entries) if existing else list(entries)
    return {"hooks": merged_hooks}


def write_codex_hooks_file(
    codex_home: Path,
    payloads: Sequence[Mapping[str, Any]],
    *,
    user_hooks_source: Path | None = None,
) -> Path:
    """
    Write the private CODEX_HOME's single ``hooks.json`` (atomically).

    The one writer for every hook generator: *payloads* are merged in
    order (Omnigent's stay in first position per event) and the user's
    hooks are appended last. A symlink to the user's file is replaced by
    the merged regular file, and is the merge source when
    *user_hooks_source* is not given.

    :param codex_home: Private per-session ``CODEX_HOME``.
    :param payloads: ``hooks.json``-shaped payloads, most privileged first.
    :param user_hooks_source: The user's real ``hooks.json`` to merge.
    :returns: Path of the written file.
    """
    codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = codex_home / _CODEX_HOOKS_FILENAME
    payload = merge_codex_hook_payloads(payloads)
    merge_source = user_hooks_source
    if merge_source is None and path.is_symlink() and path.exists():
        merge_source = path.resolve()
    if merge_source is not None and merge_source.is_file():
        payload = merge_codex_user_hooks(payload, merge_source)
    if path.is_symlink() or path.exists():
        path.unlink()
    fd, tmp_name = tempfile.mkstemp(prefix=f"{_CODEX_HOOKS_FILENAME}.", dir=str(codex_home))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return path


def write_codex_router_hooks_file(
    codex_home: Path,
    bridge_dir: Path,
    *,
    session_id: str | None = None,
    harness: str = "codex",
    python_executable: str | None = None,
    user_hooks_source: Path | None = None,
) -> Path:
    """
    Write a ``hooks.json`` holding only the routing hooks (plus user hooks).

    Used by harnesses that register no other hooks; the native app-server
    merges the routing payload with its policy hooks instead.

    :param codex_home: Private per-session ``CODEX_HOME``.
    :param bridge_dir: Session bridge directory.
    :param session_id: Omnigent session id baked into the hook commands.
    :param harness: Harness label sent to the endpoint.
    :param python_executable: Python for the hook commands.
    :param user_hooks_source: The user's real ``hooks.json`` to merge.
    :returns: Path of the written file.
    """
    return write_codex_hooks_file(
        codex_home,
        [
            codex_router_hooks_settings(
                bridge_dir,
                session_id=session_id,
                harness=harness,
                python_executable=python_executable,
            )
        ],
        user_hooks_source=user_hooks_source,
    )


def codex_router_bridge_dir(env: Mapping[str, str] | None = None) -> Path | None:
    """
    Read the routing bridge directory from a process environment.

    :param env: Environment to read; ``None`` uses :data:`os.environ`.
    :returns: Bridge directory, or ``None`` when routing is off for this
        session (no endpoint advertised, so nothing to enforce).
    """
    source = os.environ if env is None else env
    raw = (source.get(CODEX_ROUTER_DIR_ENV_VAR) or "").strip()
    return Path(raw) if raw else None


def codex_router_session_id(env: Mapping[str, str] | None = None) -> str | None:
    """
    Read the routing session id from a process environment.

    :param env: Environment to read; ``None`` uses :data:`os.environ`.
    :returns: Session id, or ``None`` when unset.
    """
    source = os.environ if env is None else env
    return (source.get(CODEX_ROUTER_SESSION_ID_ENV_VAR) or "").strip() or None


# Set for a session whose turns or spawns can land on a gateway arm codex's
# bundled catalog has no entry for, i.e. any Smart Routing session. Carried in
# the process env (not a launch argument) so the wrapped executor and the native
# app-server read the same signal, and absent everywhere else — a plain codex
# session keeps codex's own catalog and never pays the probe.
CODEX_EXTENDED_CATALOG_ENV_VAR = "OMNIGENT_CODEX_EXTENDED_MODEL_CATALOG"


def codex_extended_catalog_env(enabled: bool) -> dict[str, str]:
    """
    Build the env that asks a codex process for the extended model catalog.

    :param enabled: ``True`` for a Smart Routing session (pinned or
        auto-harness).
    :returns: Env-var overrides, empty when the catalog stays codex's own.
    """
    return {CODEX_EXTENDED_CATALOG_ENV_VAR: "1"} if enabled else {}


def codex_extended_catalog_requested(env: Mapping[str, str] | None = None) -> bool:
    """
    Report whether this codex process should extend the model catalog.

    :param env: Environment to read; ``None`` uses :data:`os.environ`.
    :returns: ``True`` when the launch asked for the extended catalog.
    """
    source = os.environ if env is None else env
    return (source.get(CODEX_EXTENDED_CATALOG_ENV_VAR) or "").strip() == "1"


#: Omnigent's own per-session signals for a codex launch: the subagent-router
#: rendezvous, its session id, and the extended-catalog request. The runner sets
#: them in the harness process env, and the executor reads them back out of
#: ``_clean_codex_env``'s filtered copy — so they must be allowed through it or
#: both features silently never engage on the wrapped ``codex`` harness.
_CODEX_OMNIGENT_LAUNCH_ENV_VARS: tuple[str, ...] = (
    CODEX_ROUTER_DIR_ENV_VAR,
    CODEX_ROUTER_SESSION_ID_ENV_VAR,
    CODEX_EXTENDED_CATALOG_ENV_VAR,
)


# Catalog file written into the private codex-home, naming the models the
# session's ``spawn_agent`` may target. See :func:`extended_model_catalog`.
_CODEX_MODEL_CATALOG_FILENAME = "model_catalog.json"
# Entry a gateway-only model is cloned from: the cheapest current arm, so an
# unset field inherits a sane current-generation value rather than a frozen one.
_CATALOG_CLONE_SOURCE_SLUG = CODEX_CATALOG_CLONE_SOURCE_SLUG


def extended_model_catalog(
    catalog: dict[str, Any],
    *,
    clone_source: str = _CATALOG_CLONE_SOURCE_SLUG,
) -> dict[str, Any] | None:
    """
    Add the routed arms codex's own catalog has no entry for.

    Codex validates ``spawn_agent``'s ``model`` against this catalog before
    the request leaves the CLI, so an arm absent from it cannot be spawned
    however servable the gateway makes it — that is what blocked GLM
    subagents. Each missing arm is cloned from *clone_source* and re-slugged
    to the id the gateway serves it as, with its own effort ladder so codex
    clamps the spawn instead of refusing it.

    :param catalog: ``codex debug models`` output, i.e.
        ``{"models": [{"slug": ..., ...}, ...]}``.
    :param clone_source: Slug whose entry supplies every field the added
        arms do not override.
    :returns: A new catalog including the added arms, or ``None`` when
        *catalog* is unusable or has nothing to add (so the caller can leave
        codex on its own bundled catalog).
    """
    models = catalog.get("models")
    if not isinstance(models, list) or not models:
        return None
    by_slug = {m.get("slug"): m for m in models if isinstance(m, dict)}
    template = by_slug.get(clone_source)
    if template is None:
        return None
    added: list[dict[str, Any]] = []
    for bare, slug in EXTENDED_CATALOG_MODELS.items():
        if slug in by_slug:
            continue
        efforts = EXTENDED_MODEL_EFFORTS.get(bare, ())
        entry = copy.deepcopy(template)
        entry.update(
            {
                "slug": slug,
                "display_name": slug.rsplit(".", 1)[-1],
                "visibility": "list",
                "default_reasoning_level": EXTENDED_MODEL_DEFAULT_EFFORT.get(bare, "medium"),
                "supported_reasoning_levels": [
                    level
                    for level in template.get("supported_reasoning_levels", [])
                    if isinstance(level, dict) and level.get("effort") in efforts
                ],
                # Upsell/nux metadata describes the cloned arm, not this one.
                "availability_nux": None,
                "upgrade": None,
            }
        )
        added.append(entry)
    if not added:
        return None
    return {**catalog, "models": [*models, *added]}


# Cached ``codex debug models`` result, keyed by (binary, CODEX_HOME). The
# catalog is a property of the installed CLI, not of a session, so a successful
# probe is paid once per host process rather than once per session.
_MODEL_CATALOG_CACHE: dict[tuple[str, str, int, int], dict[str, Any]] = {}

# Failures are cached only briefly, keyed the same way and holding the
# monotonic time the negative expires. Caching them forever turned one
# transient 10s timeout — a loaded host, a cold binary — into "this host has no
# catalog" for the life of the process, silently dropping the gateway-only arms
# from every later session's ``spawn_agent``. Caching them not at all would pay
# the full timeout per session on a genuinely broken CLI.
_MODEL_CATALOG_FAILURE_TTL_S = 60.0
_MODEL_CATALOG_FAILURES: dict[tuple[str, str, int, int], float] = {}

# Both caches are host-process globals reached from worker threads (every
# caller populates a codex home through ``asyncio.to_thread``), and the probe
# they memoize is a ~10 s subprocess. Held across the probe so two sessions
# booting together pay it once: the loser waits for the winner's result instead
# of shelling out again, which is also what keeps the dict mutations atomic.
_MODEL_CATALOG_LOCK = threading.Lock()


def _model_catalog_cache_key(codex_path: str, source_home: Path) -> tuple[str, str, int, int]:
    """
    Key the catalog cache so an in-place codex upgrade re-probes.

    The catalog IS the installed binary's, and `npm i -g @openai/codex` (or a
    Homebrew upgrade) replaces it at the same path — so path plus home alone
    served the old codex's models for the rest of the host process. The
    binary's mtime and size are in the key too; an unreadable path degrades to
    a sentinel, which just means "cache as before".

    :param codex_path: The codex binary.
    :param source_home: ``CODEX_HOME`` the probe resolves config from.
    :returns: The cache key.
    """
    try:
        stat = os.stat(codex_path)
    except OSError:
        return (codex_path, str(source_home), -1, -1)
    return (codex_path, str(source_home), stat.st_mtime_ns, stat.st_size)


def _valid_model_catalog(catalog: object) -> bool:
    """
    Report whether a probe result is shaped like a codex model catalog.

    ``model_catalog_json`` REPLACES codex's bundled catalog, so a
    half-readable payload would not degrade the session — it would narrow or
    empty the set of models codex will accept. Validated before it is allowed
    to become that file: anything unexpected is treated as a probe failure and
    the session keeps codex's own catalog.

    :param catalog: Decoded ``codex debug models`` output.
    :returns: ``True`` when every entry carries a usable ``slug``.
    """
    if not isinstance(catalog, dict):
        return False
    models = catalog.get("models")
    if not isinstance(models, list) or not models:
        return False
    for entry in models:
        if not isinstance(entry, dict):
            return False
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            return False
    return True


def read_codex_model_catalog(
    codex_path: str,
    source_home: Path,
    *,
    timeout: float = 10.0,
) -> dict[str, Any] | None:
    """
    Ask the codex CLI for its own model catalog, once per host process.

    Read from the CLI rather than pinned in this repo so the catalog tracks
    whatever codex version is installed: it is ~300 kB of vendor metadata
    (per-model prompts included) that a pinned copy would silently freeze.

    Blocking (it shells out with a timeout), so callers on the event loop must
    reach it through a thread — see :func:`write_codex_model_catalog`.

    :param codex_path: The codex binary.
    :param source_home: ``CODEX_HOME`` to resolve config from.
    :param timeout: Seconds to wait; a slow probe must not delay session boot.
    :returns: ``{"models": [...]}``, or ``None`` on any failure.
    """
    cache_key = _model_catalog_cache_key(codex_path, source_home)
    with _MODEL_CATALOG_LOCK:
        cached = _MODEL_CATALOG_CACHE.get(cache_key)
        if cached is not None:
            return cached
        failed_until = _MODEL_CATALOG_FAILURES.get(cache_key)
        if failed_until is not None:
            if time.monotonic() < failed_until:
                return None
            del _MODEL_CATALOG_FAILURES[cache_key]
        catalog = _probe_codex_model_catalog(codex_path, source_home, timeout=timeout)
        if catalog is None:
            _MODEL_CATALOG_FAILURES[cache_key] = time.monotonic() + _MODEL_CATALOG_FAILURE_TTL_S
            return None
        _MODEL_CATALOG_CACHE[cache_key] = catalog
        return catalog


def _probe_codex_model_catalog(
    codex_path: str,
    source_home: Path,
    *,
    timeout: float,
) -> dict[str, Any] | None:
    """Run ``codex debug models``, returning ``None`` on any failure."""
    try:
        completed = subprocess.run(
            [codex_path, "debug", "models"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "CODEX_HOME": str(source_home)},
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not read the codex model catalog (%s)", exc)
        return None
    if completed.returncode != 0:
        logger.warning(
            "codex debug models exited %s: %s", completed.returncode, completed.stderr[:200]
        )
        return None
    try:
        catalog = json.loads(completed.stdout)
    except ValueError as exc:
        logger.warning("could not parse the codex model catalog (%s)", exc)
        return None
    if not _valid_model_catalog(catalog):
        logger.warning(
            "codex debug models returned an unusable catalog; keeping codex's bundled one"
        )
        return None
    return cast(dict[str, Any], catalog)


def write_codex_model_catalog(
    target_dir: Path,
    *,
    codex_path: str | None,
    source_home: Path,
) -> Path | None:
    """
    Give the session a model catalog its ``spawn_agent`` can route across.

    ``model_catalog_json`` REPLACES codex's bundled catalog rather than
    merging into it (probed: a one-entry file leaves ``spawn_agent`` with
    exactly that one model), so the file is codex's own catalog plus the
    gateway-only arms — never a hand-written list.

    Every failure returns ``None`` and leaves the session on codex's bundled
    catalog: a spawn that cannot reach GLM beats a session that will not
    start.

    :param target_dir: The per-session private ``CODEX_HOME``.
    :param codex_path: The codex binary, or ``None`` when unresolved.
    :param source_home: ``CODEX_HOME`` the probe should resolve config from.
    :returns: The written catalog path, or ``None`` when nothing was written.
    """
    if codex_path is None:
        return None
    catalog = read_codex_model_catalog(codex_path, source_home)
    if catalog is None:
        return None
    extended = extended_model_catalog(catalog)
    if extended is None:
        return None
    path = target_dir / _CODEX_MODEL_CATALOG_FILENAME
    try:
        path.write_text(json.dumps(extended), encoding="utf-8")
    except OSError as exc:
        logger.warning("could not write %s (%s)", path, exc)
        return None
    return path


# ``model_catalog_json`` assignment appended to the private config copy. A
# top-level key, so it goes before the first table header.
_CATALOG_KEY_RE = re.compile(r"^\s*model_catalog_json\s*=")


def set_codex_model_catalog_path(config_path: Path, catalog_path: Path) -> bool:
    """
    Point the session's private ``config.toml`` at *catalog_path*.

    :param config_path: The copied ``config.toml`` inside the private home.
    :param catalog_path: Catalog written by
        :func:`write_codex_model_catalog`.
    :returns: ``True`` when the key was written, ``False`` when the config
        already sets one (the user's choice wins) or the write failed.
    """
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        logger.warning("could not read %s (%s)", config_path, exc)
        return False
    for line in lines:
        if line.lstrip().startswith("["):
            break
        if _CATALOG_KEY_RE.match(line):
            return False
    assignment = f"model_catalog_json = {json.dumps(str(catalog_path))}\n"
    # Before the first table header, so the key stays top-level.
    insert_at = next(
        (i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines)
    )
    lines.insert(insert_at, assignment)
    try:
        config_path.write_text("".join(lines), encoding="utf-8")
    except OSError as exc:
        logger.warning("could not write %s (%s)", config_path, exc)
        return False
    return True


# Top-level ``model_reasoning_effort = "<value>"`` assignment, tolerating
# leading whitespace and a trailing comment. Only applied to lines *before*
# the first table header so keys inside ``[profiles.*]`` etc. are never
# rewritten (they may target other providers with different ladders).
_EFFORT_KEY_RE = re.compile(r'^(\s*model_reasoning_effort\s*=\s*")([^"]*)("\s*(?:#.*)?)$')


def _normalize_copied_codex_effort(config_path: Path) -> None:
    """Rewrite a deprecated top-level ``model_reasoning_effort`` in the
    session's private copy of ``config.toml``.

    The ChatGPT desktop app manages ``~/.codex/config.toml`` on machines
    where it is installed and writes ``model_reasoning_effort = "ultra"`` —
    a value the codex CLI maps to the retired ``max`` wire value, which the
    OpenAI Responses API rejects with ``invalid_value: 'max'`` (its ladder
    tops out at ``xhigh``). Because this executor copies the user's config
    verbatim into every per-session ``CODEX_HOME``, that one app-written key
    fails **every** codex turn on such machines.

    Values already in :data:`CODEX_EFFORTS` are left untouched, as are
    values with no known alias (codex surfaces its own error for those) and
    anything below the first table header. Only the session's private copy
    is modified — never the user's real ``~/.codex/config.toml``.

    :param config_path: The copied ``config.toml`` inside the per-session
        ``CODEX_HOME``. Unreadable/unwritable files are skipped (best
        effort — the copy already succeeded, so this only degrades back to
        the pre-normalization behavior).
    """
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return
    lines = text.splitlines(keepends=True)
    changed = False
    array_depth = 0  # net unclosed '[' from a top-level multiline array value
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        if array_depth == 0:
            # At top-level statement position a leading '[' is a real table
            # header ([table] / [[array-of-tables]]) -- top-level keys end here.
            if content.lstrip().startswith("["):
                break
            match = _EFFORT_KEY_RE.match(content)
            if match is not None:
                value = match.group(2)
                if value not in CODEX_EFFORTS:
                    replacement = EFFORT_ALIASES.get(value)
                    if replacement is not None:
                        line_ending = line[len(content) :]
                        lines[index] = (
                            f"{match.group(1)}{replacement}{match.group(3)}{line_ending}"
                        )
                        changed = True
                continue  # an effort-key line never opens an array
        # Track array nesting (this is a bracket-counting heuristic, not a
        # full TOML parser, but sufficient for this narrow config shape) so
        # bracketed *array content* (which may start with '[') is not
        # mistaken for a table header.
        array_depth += content.count("[") - content.count("]")
        if array_depth < 0:
            array_depth = 0
    if changed:
        try:
            config_path.write_text("".join(lines), encoding="utf-8")
        except OSError:
            logger.warning("could not normalize model_reasoning_effort in %s", config_path)


def _databricks_codex_base_url(host: str) -> str:
    """Return the Unity AI Gateway Codex Responses base URL for *host*."""
    return f"{host.rstrip('/')}/ai-gateway/codex/v1"


def _databricks_codex_auth_command(host: str, profile: str | None = None) -> str:
    """Return the Databricks CLI ``auth.command`` for Codex.

    :param host: Databricks workspace host, e.g.
        ``"https://example.databricks.com"``.
    :param profile: Optional ``~/.databrickscfg`` profile name, e.g. ``"oss"``.
        Preferred over ``--host`` when known; see
        :func:`~omnigent.inner.databricks_executor.databricks_bearer_token_command`,
        which owns the command's shape for every harness.
    :returns: Shell command that prints a bearer token.
    """
    from .databricks_executor import databricks_bearer_token_command

    return databricks_bearer_token_command(host, profile)


def _databricks_codex_config_overrides(
    *,
    model: str,
    base_url: str,
    auth_command: str,
    auth_refresh_interval_ms: int | None = None,
) -> list[str]:
    """Return TOML-fragment overrides for the Codex per-conversation config.

    :param model: Model id to pin, e.g. ``"databricks-gpt-5-5"``.
    :param base_url: Provider base URL from ucode state or the legacy profile
        path, e.g. ``"https://example.databricks.com/ai-gateway/codex/v1"``.
    :param auth_command: Shell command from ucode state or the legacy profile
        path that prints
        a bearer token, e.g. ``"databricks auth token --host ..."``.
    :param auth_refresh_interval_ms: Refresh cadence in milliseconds,
        e.g. ``900000``.
    :returns: Codex TOML-fragment override strings.
    """
    provider_name = "omnigent_databricks"
    auth_command_json = json.dumps(auth_command)
    return [
        f"model={json.dumps(model)}",
        f'model_provider="{provider_name}"',
        "model_supports_reasoning_summaries=true",
        (
            "model_providers.omnigent_databricks="
            '{name="Omnigent Databricks",'
            f"base_url={json.dumps(base_url)},"
            'auth={command="sh",'
            f'args=["-c",{auth_command_json}],'
            "timeout_ms=5000,"
            f"refresh_interval_ms={auth_refresh_interval_ms or _GATEWAY_AUTH_REFRESH_MS}"
            "},"
            'wire_api="responses"}'
        ),
    ]


def _provider_codex_config_overrides(
    *,
    model: str | None,
    base_url: str,
    auth_command: str,
    wire_api: str,
) -> list[str]:
    """Return Codex config overrides routing through a generic provider.

    The OSS counterpart to :func:`_databricks_codex_config_overrides`: it
    points Codex at a ``configure harnesses`` provider (key / gateway / local
    serving the ``openai`` surface) by registering an ``omnigent_provider``
    ``model_provider`` with the provider's base URL, a bearer-token auth
    command (``printf`` for a static key, or the provider's ``auth_command``),
    and the provider's wire protocol — so a native Codex terminal routes
    exactly like the in-process codex harness.

    :param model: Model id to pin, e.g. ``"qwen/qwen3.7-plus"``. ``None``
        omits the ``model`` override (Codex keeps its own default model name
        while still routing through the provider).
    :param base_url: The provider's openai-family base URL, e.g.
        ``"https://openrouter.ai/api/v1"``.
    :param wire_api: The provider's configured wire protocol —
        ``"responses"`` (OpenAI / LiteLLM) or ``"chat"`` (OpenRouter and
        most OSS-model gateways). codex >= 0.137 no longer accepts ``"chat"``
        in its config (it hard-fails config load with ``wire_api = "chat" is
        no longer supported``), so a ``"chat"`` value is coerced to
        ``"responses"`` — the only wire codex still speaks — before being
        emitted. See the inline note for the OpenRouter caveat.
    :returns: Codex TOML-fragment override strings.
    """
    provider_name = "omnigent_provider"
    auth_command_json = json.dumps(auth_command)
    # codex >= 0.137 removed the chat/completions wire from its config schema:
    # any provider block carrying wire_api="chat" makes codex hard-fail config
    # load ("wire_api = \"chat\" is no longer supported"), which broke OSS /
    # OpenRouter provider routing outright. "responses" is the only value codex
    # still accepts (matching _databricks_codex_config_overrides above), so
    # coerce here rather than emit a value codex rejects. Providers that genuinely
    # only serve chat/completions (e.g. OpenRouter) will surface that mismatch at
    # request time against a config codex can at least load, instead of failing
    # to start at all.
    effective_wire_api = "responses" if wire_api == "chat" else wire_api
    overrides: list[str] = []
    if model:
        overrides.append(f"model={json.dumps(model)}")
    overrides.append(f'model_provider="{provider_name}"')
    overrides.append(
        f"model_providers.{provider_name}="
        '{name="Omnigent Provider",'
        f"base_url={json.dumps(base_url)},"
        'auth={command="sh",'
        f'args=["-c",{auth_command_json}],'
        "timeout_ms=5000,"
        f"refresh_interval_ms={_GATEWAY_AUTH_REFRESH_MS}"
        "},"
        f'wire_api="{effective_wire_api}"}}'
    )
    return overrides


def _parse_optional_int(value: str | None) -> int | None:
    """Parse an optional integer env-var value.

    :param value: Raw env-var value, e.g. ``"900000"``.
    :returns: Parsed integer, or ``None`` when unset or invalid.
    """
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        logger.warning("Ignoring invalid integer value %r", value)
        return None


def _tool_signature(tools: list[ToolSpec]) -> str:
    normalized = [
        {
            # Signature is used only as a cache key; missing fields
            # are normalised to ``None`` so tool specs that differ
            # only by absent keys still produce distinct signatures
            # from ones that explicitly set empty strings.
            "name": tool.get("name"),
            "description": tool.get("description"),
            "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
        }
        for tool in tools
    ]
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def _session_key(messages: list[Message]) -> str:
    if messages:
        last = messages[-1]
        if last.get("session_id"):
            return str(last["session_id"])
        metadata = last.get("metadata", {})
        if isinstance(metadata, dict) and metadata.get("session_id"):
            return str(metadata["session_id"])
    return "default"


def _extract_latest_user_content(
    messages: list[Message],
) -> str | list[CodexParams]:
    """
    Extract the latest user message content.

    Returns a plain string for text-only messages. When the
    message carries multimodal content blocks, returns the
    block list so the caller can pass structured input to the
    Codex app-server.

    :param messages: Conversation history.
    :returns: A string prompt or a list of content block dicts.
    """
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            if content is None:
                return ""
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return content
            return json.dumps(content)
    return ""


def _build_initial_prompt(
    messages: list[Message],
) -> str | list[CodexParams]:
    """
    Build the initial prompt for a fresh Codex thread.

    For single-message or single-user-message inputs, returns
    the latest user content directly (may be multimodal). For
    multi-turn history, serializes prior turns as text and
    returns a plain string.

    :param messages: Conversation history.
    :returns: A string prompt or a list of content block dicts.
    """
    user_messages = [msg for msg in messages if msg.get("role") == "user"]
    if len(messages) <= 1 or len(user_messages) <= 1:
        return _extract_latest_user_content(messages)

    lines = ["Conversation so far:"]
    for msg in messages:
        role = str(msg.get("role", "user")).replace("_", " ")
        raw_content = msg.get("content")
        if raw_content is None:
            content = ""
        elif isinstance(raw_content, str):
            content = raw_content
        else:
            content = json.dumps(raw_content, ensure_ascii=True)
        lines.append(f"{role}: {content}")
    lines.append("")
    lines.append("Respond to the latest user message, using the conversation above as context.")
    return "\n".join(lines)


def _prompt_for_turn(messages: list[Message], *, is_new_thread: bool) -> str | list[CodexParams]:
    """
    Choose the prompt payload for a Codex turn.

    A fresh Codex thread must receive the full replayable
    transcript so the harness can preserve parent history on
    sub-agent calls. Resumed threads should only receive the
    latest user message.

    :param messages: Conversation history.
    :param is_new_thread: ``True`` when starting a fresh Codex
        thread.
    :returns: A string prompt or a list of content block dicts.
    """
    if is_new_thread:
        return _build_initial_prompt(messages)
    return _extract_latest_user_content(messages)


def _to_codex_input_items(
    blocks: list[CodexParams],
) -> list[CodexParams]:
    """
    Convert Responses API content blocks to Codex app-server
    ``turn/start`` input items.

    The Codex app-server accepts ``{"type": "text", ...}`` for
    text and ``{"type": "image", "image_url": "..."}`` for
    images. This maps the Responses API ``input_image`` /
    ``input_text`` block types to the Codex wire format.

    :param blocks: Responses API content block dicts (may include
        ``input_text``, ``input_image``, ``input_file``).
    :returns: Codex input item dicts.
    """
    items: list[CodexParams] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type in ("input_text", "output_text", "text"):
            text = block.get("text")
            if isinstance(text, str) and text:
                items.append({"type": "text", "text": text})
        elif block_type == "input_image":
            url = block.get("image_url")
            if isinstance(url, str) and url:
                items.append({"type": "image", "url": url})
        elif block_type == "input_file":
            # The Codex CLI does not support ``input_file`` blocks; it only
            # accepts ``text``, ``image``, ``localImage``, ``skill``, and
            # ``mention``.  Fall back to inline text: decode the base64
            # payload from the ``file_data`` data URI so the model can still
            # reason about the file content.
            file_data: str = block.get("file_data", "")
            if file_data.startswith("data:"):
                try:
                    meta, b64 = file_data.split(",", 1)
                    mime = meta.split(";")[0].replace("data:", "")
                    if not mime.startswith("text/"):
                        continue  # binary files (PDF, etc.) can't be inlined as text
                    text = base64.b64decode(b64).decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    text = ""
            else:
                text = file_data
            if text:
                items.append({"type": "text", "text": text})
    return items


def _completed_agent_message_text(
    item: CodexParams,
    message_buffers: dict[str, str],
) -> tuple[str | None, str]:
    """Return the phase + final text for a completed Codex agent message.

    :param item: The ``params["item"]`` dict from ``item/completed``.
    :param message_buffers: Per-item accumulated delta text keyed by item id.
    :returns: ``(phase, text)`` where ``phase`` may be ``None``.
    """
    raw_completed_id = item.get("id")
    if not isinstance(raw_completed_id, str):
        return None, ""
    completed_item_id = raw_completed_id
    raw_completed_text = item.get("text")
    completed_text: str = raw_completed_text if isinstance(raw_completed_text, str) else ""
    phase = item.get("phase") if isinstance(item.get("phase"), str) else None
    return phase, message_buffers.get(completed_item_id, completed_text)


def _latest_buffered_agent_message(message_buffers: dict[str, str]) -> str:
    """Return the most recent buffered assistant text from Codex deltas.

    ``turn/completed`` can arrive without a terminal ``item/completed``. In
    that case the best available response is the last accumulated delta text.

    :param message_buffers: Per-item accumulated delta text keyed by item id.
    :returns: The most recent buffered assistant text, or ``""`` when absent.
    """
    if not message_buffers:
        return ""
    return next(reversed(message_buffers.values()))


def _sandbox_mode(spec: OSEnvSpec | None) -> str:
    if spec is None:
        return "read-only"
    sandbox = spec.sandbox or OSEnvSandboxSpec()
    if sandbox.type == "none":
        return "danger-full-access"
    if sandbox.write_paths:
        return "workspace-write"
    return "read-only"


def _dynamic_tool_specs(tools: list[ToolSpec]) -> list[CodexParams]:
    specs: list[CodexParams] = []
    for tool in tools:
        raw_name = tool.get("name")
        # Codex App Server's ``dynamicTools`` protocol requires a
        # non-empty ``name``; skip tool specs missing one rather
        # than registering a tool the server will reject.
        if not isinstance(raw_name, str) or not raw_name:
            continue
        raw_desc = tool.get("description")
        # Description is optional to the server; pass ``""`` only at
        # this JSON-RPC boundary where the wire format expects ``str``.
        desc: str = raw_desc if isinstance(raw_desc, str) else ""
        specs.append(
            {
                "name": raw_name,
                "description": desc,
                "inputSchema": tool.get("parameters", {"type": "object", "properties": {}}),
            }
        )
    return specs


def _result_text(result: CodexToolResult) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False)
    except TypeError:
        return str(result)


def _dynamic_tool_result_payload(result: CodexToolResult) -> CodexParams:
    classification = classify_tool_result(result)
    return {
        "success": classification.status == ToolCallStatus.SUCCESS,
        "contentItems": [
            {
                "type": "inputText",
                "text": _result_text(result),
            }
        ],
    }


@dataclass
class _PendingToolResult:
    """Tracks a dynamic tool invocation pending a Codex result event.

    :param name: The tool name Codex asked Omnigent to run.
    :param result: The raw tool result payload, or ``None`` if the tool
        hasn't completed yet.
    :param status: Classification of ``result`` (success / error / blocked).
    :param error: Human-readable error message from the tool, or ``None``
        when the call succeeded.
    :param duration_ms: Wall-clock time spent running the tool, in ms.
    """

    name: str
    result: CodexToolResult | None = None
    status: ToolCallStatus = ToolCallStatus.SUCCESS
    error: str | None = None
    duration_ms: float = 0.0


class _CodexAppServerSession:
    def __init__(
        self,
        *,
        codex_path: str,
        cwd: str | None,
        env: dict[str, str],
        tool_executor: CodexToolExecutor | None,
        codex_config_overrides: list[str] | None = None,
        disable_native_tools: bool = False,
        bundle_dir: Path | None = None,
        skills_filter: str | list[str] = "all",
    ) -> None:
        self._codex_path = codex_path
        self._cwd = cwd
        self._env = env
        self._tool_executor = tool_executor
        self._codex_config_overrides = list(codex_config_overrides or [])
        self._disable_native_tools = disable_native_tools
        self._bundle_dir = bundle_dir
        self._skills_filter = skills_filter
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending_requests: dict[int, asyncio.Future[CodexMessage]] = {}
        self._events: asyncio.Queue[CodexMessage] = asyncio.Queue()
        self._next_id = 1
        self._started = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self.thread_id: str | None = None
        self.active_turn_id: str | None = None
        # Last reasoning effort applied via ``thread/settings/update`` on the
        # current thread. Effort is not part of the executor's session
        # signature, so a change must be re-applied per turn; this is reset on
        # a fresh thread so it is re-sent. ``turn/start`` carries no ``effort``
        # field (it is silently dropped), hence the separate settings update.
        self._applied_effort: str | None = None
        self._recent_stderr: list[str] = []
        # Auth-class gateway rejection tracking, read off the CLI's stderr so a
        # turn that emits no events fails fast with the real cause instead of
        # stalling to the idle watchdog. ``_pending`` holds a parsed fatal
        # (401/403) rejection; ``_saw_retries_exhausted`` records a final
        # ``Reconnecting N/N``. Fast-fail arms (``_fatal_gateway_error``) only
        # when BOTH are seen, so a blip the CLI recovers from can't kill a
        # healthy turn. All three reset at turn start.
        self._pending_fatal_gateway_error: _CodexGatewayError | None = None
        self._saw_retries_exhausted = False
        self._fatal_gateway_error: _CodexGatewayError | None = None
        self._recent_events: list[CodexMessage] = []
        self._process_cwd: Path | None = None
        # Private CODEX_HOME so the subprocess never writes to the user's ~/.codex/.
        self._codex_home_dir: Path | None = None
        # Most recent ``thread/tokenUsage/updated`` payload's ``last``
        # turn breakdown, mapped to the wire shape. Consumed (and cleared)
        # on the next ``turn/completed`` so each TurnComplete carries the
        # usage for the turn that just finished.
        self._last_turn_usage: dict[str, object] | None = None

    async def start(self) -> None:
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        codex_home_root = Path(tempfile.gettempdir())
        if self._cwd and self._cwd != "/":
            try:
                codex_home_root = Path(self._cwd) / ".codex-tmp"
                codex_home_root.mkdir(parents=True, exist_ok=True)
            except OSError:
                # The cwd may be on a read-only filesystem — e.g. macOS
                # root ``/`` inherited from a runner whose working
                # directory was never explicitly set.  Fall back to the
                # system temp directory so the codex home is still writable.
                codex_home_root = Path(tempfile.gettempdir())
        self._codex_home_dir = Path(
            tempfile.mkdtemp(prefix="omnigent-codex-home-", dir=str(codex_home_root))
        )
        # Populate the per-conversation CODEX_HOME's ``skills/`` subdir
        # based on the spec's ``skills:`` field. Codex auto-discovers
        # skills under ``$CODEX_HOME/skills/<name>/SKILL.md``; without
        # this step the temp CODEX_HOME has no skills directory at all,
        # so even ``skills: all`` would expose nothing. The shared helper
        # is the same one the codex-native launch path uses, so both
        # expose an identical skill surface.
        populate_codex_skills_from_bundle(
            self._codex_home_dir,
            self._bundle_dir,
            self._skills_filter,
        )
        # Bridge the user's authentication and provider config into the
        # temp CODEX_HOME. The codex CLI reads ``auth.json`` (OAuth tokens
        # for subscription auth) and ``config.toml`` (model provider
        # definitions) from ``$CODEX_HOME``; without this step a freshly-
        # created temp dir has neither, causing 401 Unauthorized errors
        # for subscription-authenticated users.
        config_source = _codex_home_config_source_from_env()
        # When the runner advertises a subagent-routing endpoint, the user's
        # hooks.json is merged into a generated file registering the routing
        # hooks instead of being symlinked in untouched. Only an auto-harness
        # Smart Routing session gets that endpoint, so a plain or pinned session
        # keeps the symlink — and with it mid-session edits to the user's file.
        router_bridge_dir = codex_router_bridge_dir(self._env)
        if router_bridge_dir is not None:
            # Probed only on the routing path so a plain session never pays the
            # subprocess. A CLI too old for the spawn gate drops the hooks and
            # keeps the symlinked home, so routing no-ops instead of blocking.
            skip_reason = codex_routing_hook_skip_reason(
                await _codex_cli_version(self._codex_path)
            )
            if skip_reason is not None:
                logger.warning("%s", skip_reason)
                router_bridge_dir = None
        # Off the loop: this copies/symlinks a home AND (on the routing path)
        # shells out to ``codex debug models`` with a 10s timeout. Run inline it
        # stalled every other session sharing this event loop for that long.
        await asyncio.to_thread(
            _populate_codex_home_config,
            self._codex_home_dir,
            config_source,
            inject_hooks=router_bridge_dir is not None,
            extend_model_catalog=codex_extended_catalog_requested(self._env),
        )
        self._codex_config_overrides = materialize_codex_provider_config(
            self._codex_home_dir,
            self._codex_config_overrides,
        )
        if router_bridge_dir is not None:
            write_codex_router_hooks_file(
                self._codex_home_dir,
                router_bridge_dir,
                session_id=codex_router_session_id(self._env),
                user_hooks_source=config_source / _CODEX_HOOKS_FILENAME,
            )
        # Override CODEX_HOME so Codex stores its data (including conversation
        # history) in a private temp directory rather than the user's ~/.codex/.
        # This prevents subagent sessions from polluting the user's Codex history.
        proc_env = {**self._env, "CODEX_HOME": str(self._codex_home_dir)}
        try:
            argv = [self._codex_path, "app-server"]
            for override in self._codex_config_overrides:
                argv.extend(["-c", override])
            self._proc = await _create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=proc_env,
                **_proc.spawn_kwargs(),
                cwd=self._cwd or os.getcwd(),
            )
            self._reader_task = asyncio.create_task(self._reader_loop())
            self._stderr_task = asyncio.create_task(self._stderr_loop())
            await self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "omnigent",
                        "version": "0.1",
                    },
                    "capabilities": {
                        "experimentalApi": True,
                    },
                },
            )
            self._started = True
            if router_bridge_dir is not None:
                # App-server threads run persisted-trusted hooks only, so the
                # routing hooks need the trust handshake to be enforced.
                # Imported here: the app-server module imports this one.
                from omnigent.codex_native_app_server import trust_codex_router_hooks

                try:
                    await trust_codex_router_hooks(self._request, cwd=self._cwd or os.getcwd())
                except Exception:  # noqa: BLE001 - never block session startup
                    logger.warning(
                        "codex subagent-routing hook trust failed; "
                        "routing will not be enforced for this session",
                        exc_info=True,
                    )
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        current_loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not current_loop:
            if self._proc is not None and self._proc.returncode is None:
                _terminate_process_tree(self._proc)
            self._pending_requests.clear()
            if self._proc is not None:
                close_subprocess_transport(self._proc)
            self._started = False
            self._proc = None
            self._reader_task = None
            self._stderr_task = None
            self._loop = None
            self.thread_id = None
            self.active_turn_id = None
            self._cleanup_process_cwd()
            return

        if self._proc is not None and self._proc.returncode is None:
            _terminate_process_tree(self._proc)
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                _kill_process_tree(self._proc)
                await self._proc.wait()
        stdin = self._proc.stdin if self._proc is not None else None
        if stdin is not None:
            with suppress(Exception):
                stdin.close()
            with suppress(Exception):
                await stdin.wait_closed()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        for future in self._pending_requests.values():
            if not future.done():
                future.cancel()
        self._pending_requests.clear()
        if self._proc is not None:
            close_subprocess_transport(self._proc)
        self._started = False
        self._proc = None
        self._reader_task = None
        self._stderr_task = None
        self._loop = None
        self.thread_id = None
        self.active_turn_id = None
        self._recent_events.clear()
        self._cleanup_process_cwd()

    def _cleanup_process_cwd(self) -> None:
        if self._codex_home_dir is not None:
            shutil.rmtree(self._codex_home_dir, ignore_errors=True)
            self._codex_home_dir = None

    def _record_event(self, message: CodexMessage) -> None:
        self._recent_events.append(message)
        if len(self._recent_events) > 20:
            self._recent_events.pop(0)

    def _format_recent_events(self) -> list[CodexParams]:
        formatted: list[CodexParams] = []
        for message in self._recent_events[-8:]:
            params = message.get("params")
            item = params.get("item") if isinstance(params, dict) else None
            item_payload: CodexParams = item if isinstance(item, dict) else {}
            formatted.append(
                {
                    "method": message.get("method"),
                    "turnId": params.get("turnId") if isinstance(params, dict) else None,
                    "itemType": item_payload.get("type"),
                    "itemPhase": item_payload.get("phase"),
                    "itemId": item_payload.get("id"),
                    "callId": params.get("callId") if isinstance(params, dict) else None,
                    "tool": params.get("tool") if isinstance(params, dict) else None,
                }
            )
        return formatted

    async def _drain_turn_completed_tail(
        self,
        *,
        active_turn_id: str,
        message_buffers: dict[str, str],
        final_response: str,
    ) -> str:
        """Collect a trailing final-answer item that arrives after ``turn/completed``.

        Some Codex app-server turns emit ``turn/completed`` slightly before the
        terminal ``item/completed`` with ``phase="final_answer"``. Drain a
        short tail window so we don't lock in ``response=""`` when the final
        assistant item is still in flight.

        :param active_turn_id: The current turn id.
        :param message_buffers: Accumulated delta text keyed by item id.
        :param final_response: The response text accumulated so far.
        :returns: The best final response observed during the drain window.
        """
        deadline = time.monotonic() + _TURN_COMPLETED_DRAIN_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return final_response
            try:
                message = await asyncio.wait_for(self._events.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return final_response
            self._record_event(message)
            params = message.get("params", {})
            if not isinstance(params, dict):
                continue
            event_turn_id = params.get("turnId")
            if (
                isinstance(event_turn_id, str)
                and event_turn_id
                and event_turn_id != active_turn_id
            ):
                continue
            if message.get("method") == "item/agentMessage/delta":
                item_id = params.get("itemId")
                delta = params.get("delta")
                if isinstance(item_id, str) and isinstance(delta, str) and delta:
                    prior = message_buffers.get(item_id, "")
                    message_buffers[item_id] = f"{prior}{delta}"
                    if not final_response:
                        final_response = message_buffers[item_id]
                continue
            if message.get("method") != "item/completed":
                continue
            item = params.get("item", {})
            if not isinstance(item, dict) or item.get("type") != "agentMessage":
                continue
            phase, completed_text = _completed_agent_message_text(item, message_buffers)
            if phase == "commentary":
                item_id = item.get("id")
                if isinstance(item_id, str):
                    message_buffers.pop(item_id, None)
                continue
            if phase == "final_answer" or phase is None:
                final_response = completed_text
            if phase == "final_answer":
                return final_response

    async def run_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        model: str | None,
        cwd: str,
        sandbox: str,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        await self.start()
        assert self._proc is not None

        # Fresh turn: forget any prior turn's gateway-error signals and clear
        # the shared watchdog slot so a resolved earlier failure can't be
        # misattributed to this turn.
        self._pending_fatal_gateway_error = None
        self._saw_retries_exhausted = False
        self._fatal_gateway_error = None
        native_forwarder_health.note_post_success()

        is_new_thread = self.thread_id is None
        if is_new_thread:
            params: CodexParams = {
                "approvalPolicy": "never",
                "cwd": cwd,
                "model": model,
                "sandbox": sandbox,
            }
            if system_prompt:
                params["developerInstructions"] = system_prompt
            if tools:
                params["dynamicTools"] = _dynamic_tool_specs(tools)
                features: CodexParams = {"unified_exec": False}
                if self._disable_native_tools:
                    features["shell_tool"] = False
                params["config"] = {"features": features}
            response = await self._request("thread/start", params)
            thread = response.get("result", {}).get("thread", {})
            raw_thread_id = thread.get("id")
            # The Codex App Server is required to return a thread id
            # on ``thread/start``. Propagate ``None`` so the assert
            # below fails loud for a protocol violation instead of
            # silently carrying an empty-string thread id.
            self.thread_id = raw_thread_id if isinstance(raw_thread_id, str) else None
            # Fresh thread: forget the prior thread's applied effort so the
            # settings update below re-sends it for this thread.
            self._applied_effort = None

        assert self.thread_id is not None
        latest_user_content = _extract_latest_user_content(messages)
        goal_objective = _goal_objective_from_content(latest_user_content)
        prompt_messages = messages
        if goal_objective is not None:
            await self._request(
                "thread/goal/set",
                {
                    "threadId": self.thread_id,
                    "objective": goal_objective,
                },
            )
            # A reset thread still needs prior history, with the command
            # replaced by the clean objective sent to Codex.
            prompt_messages = [message.copy() for message in messages]
            for message in reversed(prompt_messages):
                if message.get("role") == "user":
                    message["content"] = goal_objective
                    break
        prompt = _prompt_for_turn(prompt_messages, is_new_thread=is_new_thread)
        if isinstance(prompt, list):
            turn_input = _to_codex_input_items(prompt)
        else:
            turn_input = [{"type": "text", "text": prompt}]
        # Newer Codex app-server builds apply reasoning effort through
        # ``thread/settings/update``. Older supported builds reject that RPC but
        # accept the same ``effort`` / ``summary`` fields on ``turn/start``.
        # Prefer the persistent thread setting and fall back only for that
        # explicit protocol-version mismatch.
        effort_via_turn_start = False
        if reasoning_effort and reasoning_effort != self._applied_effort:
            try:
                await self._request(
                    "thread/settings/update",
                    {
                        "threadId": self.thread_id,
                        "effort": reasoning_effort,
                        "summary": "detailed",
                    },
                )
            except RuntimeError as exc:
                error_text = str(exc)
                unsupported_settings_update = (
                    "thread/settings/update" in error_text and "unknown variant" in error_text
                )
                if not unsupported_settings_update:
                    raise
                logger.info(
                    "Codex app-server does not support thread/settings/update; "
                    "falling back to turn/start effort."
                )
                effort_via_turn_start = True
            else:
                self._applied_effort = reasoning_effort
        turn_params: CodexParams = {
            "threadId": self.thread_id,
            "input": turn_input,
        }
        if effort_via_turn_start:
            turn_params["effort"] = reasoning_effort
            turn_params["summary"] = "detailed"
        start_response = await self._request(
            "turn/start",
            turn_params,
        )
        if effort_via_turn_start:
            self._applied_effort = reasoning_effort
        raw_active_turn_id = start_response.get("result", {}).get("turn", {}).get("id")
        if not isinstance(raw_active_turn_id, str) or not raw_active_turn_id:
            yield ExecutorError(message="Codex App Server did not return a turn id")
            return
        active_turn_id: str = raw_active_turn_id
        self.active_turn_id = active_turn_id

        while not self._events.empty():
            queued_message = self._events.get_nowait()
            queued_turn_id: str | None = None
            queued_params = queued_message.get("params")
            if isinstance(queued_params, dict):
                raw_queued_turn_id = queued_params.get("turnId")
                if isinstance(raw_queued_turn_id, str):
                    queued_turn_id = raw_queued_turn_id
            if queued_turn_id is not None and queued_turn_id == active_turn_id:
                self._events.put_nowait(queued_message)
                break

        message_buffers: dict[str, str] = {}
        pending_tool_results: dict[str, _PendingToolResult] = {}
        final_response = ""
        observed_turn_id: str | None = None

        def _event_turn_matches(params: CodexParams) -> bool:
            nonlocal active_turn_id, observed_turn_id
            raw_event_turn_id = params.get("turnId")
            # Missing turnId means the event is not scoped to a turn
            # (e.g. global notifications); treat it as matching the
            # current turn.
            if not isinstance(raw_event_turn_id, str) or not raw_event_turn_id:
                return True
            event_turn_id: str = raw_event_turn_id
            if event_turn_id == active_turn_id:
                observed_turn_id = event_turn_id
                return True
            if observed_turn_id is None:
                # A first event that *terminates* the turn (final-answer
                # item) is almost certainly a stale tail from the prior
                # turn, not a Codex-quirk rewrite. Drop it instead of
                # adopting it as the new turn's response.
                item = params.get("item")
                if isinstance(item, dict) and item.get("phase") == "final_answer":
                    logger.info(
                        "Codex dropping stale final-answer event (turn_id=%s, active_turn_id=%s).",
                        event_turn_id,
                        active_turn_id,
                    )
                    return False
                # Diagnostic: dump recent events alongside adopt so
                # CI logs surface the full event sequence.
                logger.info(
                    "Codex turn/start returned turn_id=%s but first event used "
                    "turn_id=%s; adopting observed id. recent_events=%s",
                    active_turn_id,
                    event_turn_id,
                    self._format_recent_events(),
                )
                active_turn_id = event_turn_id
                self.active_turn_id = event_turn_id
                observed_turn_id = event_turn_id
                return True
            return False

        try:
            while True:
                event_task = asyncio.ensure_future(self._events.get())
                idle_seconds = 0.0
                seconds_since_warn = 0.0
                fatal_gateway_error: _CodexGatewayError | None = None
                # Poll on the shorter of the two intervals so a fatal gateway
                # error set mid-wait is acted on promptly; when a test shrinks
                # the warn window below the poll interval, poll at the warn
                # window so the warning cadence is preserved.
                poll_interval = min(_TURN_EVENT_POLL_SECONDS, _TURN_EVENT_WARN_SECONDS)
                try:
                    while True:
                        done, _ = await asyncio.wait({event_task}, timeout=poll_interval)
                        if event_task in done:
                            break
                        # The stderr loop sets this once the CLI has exhausted
                        # its retries on an auth-class gateway rejection. Fail
                        # fast with the real cause rather than stalling to the
                        # idle watchdog on a turn that will never emit events.
                        if self._fatal_gateway_error is not None:
                            fatal_gateway_error = self._fatal_gateway_error
                            break
                        idle_seconds += poll_interval
                        seconds_since_warn += poll_interval
                        if seconds_since_warn < _TURN_EVENT_WARN_SECONDS:
                            continue
                        seconds_since_warn = 0.0
                        pending_tool_summaries = [
                            {
                                "call_id": call_id,
                                "name": pending.name,
                                "status": pending.status.value,
                                "error": pending.error,
                            }
                            for call_id, pending in pending_tool_results.items()
                        ]
                        logger.warning(
                            "Codex turn %s has been idle for %ds "
                            "(thread_id=%s recent_events=%s recent_stderr=%s "
                            "pending_tool_results=%s); still waiting.",
                            active_turn_id,
                            int(idle_seconds),
                            self.thread_id,
                            self._format_recent_events(),
                            self._recent_stderr[-8:],
                            pending_tool_summaries,
                        )
                except BaseException:
                    event_task.cancel()
                    with suppress(BaseException):
                        await event_task
                    raise
                if fatal_gateway_error is not None:
                    event_task.cancel()
                    with suppress(BaseException):
                        await event_task
                    try:
                        await asyncio.wait_for(self.interrupt_turn(), timeout=0.5)
                    except Exception as exc:  # noqa: BLE001 — interrupt is best-effort
                        logger.debug("Codex auth-failure turn interrupt failed: %s", exc)
                    yield ExecutorError(
                        message=fatal_gateway_error.detail(model=model), retryable=False
                    )
                    return
                message = event_task.result()

                self._record_event(message)
                raw_method = message.get("method")
                method: str | None = raw_method if isinstance(raw_method, str) else None
                params = message.get("params", {})

                if method == "item/tool/call":
                    if not _event_turn_matches(params):
                        continue
                    raw_call_id = params.get("callId")
                    raw_tool_name = params.get("tool")
                    # ``item/tool/call`` without a callId/tool is a
                    # protocol violation from the Codex App Server;
                    # skip rather than registering a pending result
                    # under an empty key.
                    if not isinstance(raw_call_id, str) or not isinstance(raw_tool_name, str):
                        continue
                    call_id: str = raw_call_id
                    tool_name: str = raw_tool_name
                    tool_args = params.get("arguments", {})
                    if not isinstance(tool_args, dict):
                        tool_args = {}
                    started = time.monotonic()
                    yield ToolCallRequest(
                        name=tool_name,
                        args=tool_args,
                        metadata={"call_id": call_id},
                    )
                    result = await self._execute_dynamic_tool(tool_name, tool_args)
                    classification = classify_tool_result(result)
                    duration_ms = (time.monotonic() - started) * 1000
                    pending_tool_results[call_id] = _PendingToolResult(
                        name=tool_name,
                        result=result,
                        status=classification.status,
                        error=classification.error,
                        duration_ms=duration_ms,
                    )
                    logger.info(
                        "Codex dynamic tool result: turn_id=%s call_id=%s "
                        "tool=%s status=%s result=%s",
                        active_turn_id,
                        call_id,
                        tool_name,
                        classification.status.value,
                        _result_text(result),
                    )
                    await self._send_response(
                        int(message["id"]),
                        _dynamic_tool_result_payload(result),
                    )
                    yield ToolCallComplete(
                        name=tool_name,
                        status=classification.status,
                        result=result,
                        error=classification.error,
                        duration_ms=duration_ms,
                        metadata={"call_id": call_id},
                    )
                    continue

                if method == "item/agentMessage/delta":
                    if not _event_turn_matches(params):
                        continue
                    raw_item_id = params.get("itemId")
                    raw_delta = params.get("delta")
                    if not isinstance(raw_item_id, str) or not isinstance(raw_delta, str):
                        continue
                    if not raw_delta:
                        continue
                    item_id: str = raw_item_id
                    delta: str = raw_delta
                    prior = message_buffers.get(item_id)
                    message_buffers[item_id] = (prior if prior is not None else "") + delta
                    yield TextChunk(text=delta)
                    continue

                if method in ("item/reasoning/textDelta", "item/reasoning/summaryTextDelta"):
                    if not _event_turn_matches(params):
                        continue
                    raw_reasoning_delta = params.get("delta")
                    if not isinstance(raw_reasoning_delta, str) or not raw_reasoning_delta:
                        continue
                    yield ReasoningChunk(delta=raw_reasoning_delta, event_type="reasoning_text")
                    continue

                if method == "item/completed":
                    if not _event_turn_matches(params):
                        continue
                    item = params.get("item", {})
                    if not isinstance(item, dict):
                        continue
                    raw_item_type = item.get("type")
                    item_type: str | None = (
                        raw_item_type if isinstance(raw_item_type, str) else None
                    )
                    if item_type == "agentMessage":
                        raw_completed_id = item.get("id")
                        if not isinstance(raw_completed_id, str):
                            continue
                        completed_item_id: str = raw_completed_id
                        raw_completed_text = item.get("text")
                        completed_text: str = (
                            raw_completed_text if isinstance(raw_completed_text, str) else ""
                        )
                        if completed_item_id not in message_buffers and completed_text:
                            message_buffers[completed_item_id] = completed_text
                            yield TextChunk(text=completed_text)
                        phase, completed_text = _completed_agent_message_text(
                            item, message_buffers
                        )
                        if phase == "commentary":
                            message_buffers.pop(completed_item_id, None)
                            continue
                        if phase == "final_answer" or phase is None:
                            final_response = completed_text
                        if phase == "final_answer":
                            # Diagnostic: log response head + turn id so
                            # ghost-events surface in CI logs.
                            logger.info(
                                "Codex TurnComplete: turn_id=%s response_head=%r",
                                active_turn_id,
                                final_response[:120],
                            )
                            turn_usage = self._last_turn_usage
                            self._last_turn_usage = None
                            _notify_usage_from_dict(model=model, usage=turn_usage)
                            yield TurnComplete(response=final_response, usage=turn_usage)
                            return
                        continue
                    if item_type == "dynamicToolCall":
                        raw_dyn_call_id = item.get("id")
                        if not isinstance(raw_dyn_call_id, str):
                            continue
                        pending_tool_results.pop(raw_dyn_call_id, None)
                        continue

                if method == "thread/tokenUsage/updated":
                    self._last_turn_usage = _extract_codex_last_turn_usage(params, model)
                    continue

                if method == "turn/completed":
                    turn = params.get("turn", {})
                    raw_completed_turn_id = turn.get("id")
                    completed_turn_id: str | None = (
                        raw_completed_turn_id
                        if isinstance(raw_completed_turn_id, str) and raw_completed_turn_id
                        else None
                    )
                    if completed_turn_id is not None and completed_turn_id != active_turn_id:
                        logger.warning(
                            "Codex emitted turn/completed for unexpected "
                            "turn_id=%s while active_turn_id=%s",
                            completed_turn_id,
                            active_turn_id,
                        )
                        continue
                    if not final_response:
                        final_response = _latest_buffered_agent_message(message_buffers)
                    if not final_response:
                        final_response = await self._drain_turn_completed_tail(
                            active_turn_id=active_turn_id,
                            message_buffers=message_buffers,
                            final_response=final_response,
                        )
                    turn_usage = self._last_turn_usage
                    self._last_turn_usage = None
                    _notify_usage_from_dict(model=model, usage=turn_usage)
                    yield TurnComplete(response=final_response, usage=turn_usage)
                    return

                if method == "turn/failed":
                    if isinstance(params, dict) and params.get("willRetry") is True:
                        continue
                    turn = params.get("turn", {}) if isinstance(params, dict) else {}
                    raw_failed_turn_id = turn.get("id")
                    failed_turn_id: str | None = (
                        raw_failed_turn_id
                        if isinstance(raw_failed_turn_id, str) and raw_failed_turn_id
                        else None
                    )
                    if failed_turn_id is not None and failed_turn_id != active_turn_id:
                        continue
                    error_text = str(
                        params.get("message")
                        or turn.get("error")
                        or "Codex App Server turn failed"
                    )
                    # turn/failed is a provider/runtime-level turn error
                    # (e.g. tool exit code, transient provider issue) —
                    # mark retryable so the workflow's retry policy
                    # reissues instead of surfacing as permanent.
                    yield ExecutorError(message=error_text, retryable=True)
                    return

                if method == "error":
                    if isinstance(params, dict) and params.get("willRetry") is True:
                        continue
                    # JSON-RPC-shaped error frames from the app server
                    # carry ``code`` / ``message`` / ``data``. Some error
                    # paths populate only ``code``+``data`` and leave
                    # ``message`` empty, which used to surface as the
                    # bare fallback ``"Codex App Server error"`` —
                    # giving the user no indication of WHY codex failed.
                    # Always emit every populated field so a config
                    # mismatch (e.g. a Claude model name passed to a
                    # codex harness, an invalid profile) gets a full
                    # diagnostic trail.
                    detail = _format_codex_error_params(params)
                    if self._recent_stderr:
                        detail = f"{detail}; stderr: {' | '.join(self._recent_stderr[-5:])}"
                    # method==error from the app server is a runtime
                    # failure (often a tool-execution or provider issue,
                    # e.g. a bash printf failure); treat as retryable.
                    yield ExecutorError(message=detail, retryable=True)
                    return
        finally:
            if self.active_turn_id == active_turn_id:
                self.active_turn_id = None

    async def enqueue_message(self, content: CodexEnqueuedContent) -> bool:
        if self.thread_id is None or self.active_turn_id is None:
            return False
        if isinstance(content, str):
            text = content
        else:
            text = json.dumps(content, ensure_ascii=True)
        try:
            response = await self._request(
                "turn/steer",
                {
                    "threadId": self.thread_id,
                    "expectedTurnId": self.active_turn_id,
                    "input": [
                        {
                            "type": "text",
                            "text": text,
                        }
                    ],
                },
            )
        except Exception as exc:  # noqa: BLE001 — steer is best-effort; any failure surfaces as False
            logger.debug("Codex turn/steer failed: %s", exc)
            return False

        turn_id = str(response.get("result", {}).get("turnId") or self.active_turn_id)
        if turn_id:
            self.active_turn_id = turn_id
        return True

    async def interrupt_turn(self) -> bool:
        if self.thread_id is None or self.active_turn_id is None:
            return False
        await self._request(
            "turn/interrupt",
            {
                "threadId": self.thread_id,
                "turnId": self.active_turn_id,
            },
        )
        return True

    async def _execute_dynamic_tool(
        self,
        tool_name: str,
        tool_args: ToolArgs,
    ) -> CodexToolResult:
        if self._tool_executor is None:
            return {"error": f"No tool executor available for '{tool_name}'"}
        try:
            raw = self._tool_executor(tool_name, tool_args)
            resolved: CodexToolResult = await raw if isinstance(raw, Awaitable) else raw
            if isinstance(resolved, dict):
                return resolved
            return {"result": resolved}
        except Exception as exc:  # noqa: BLE001 — tool errors are surfaced to Codex via the JSON response envelope
            return {"error": str(exc)}

    async def _request(self, method: str, params: CodexParams) -> CodexMessage:
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[CodexMessage] = loop.create_future()
        self._pending_requests[request_id] = future
        await self._send_message(
            {
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        response = await future
        error = response.get("error")
        if error:
            raise RuntimeError(str(error))
        return response

    async def _send_response(self, request_id: int, result: CodexParams) -> None:
        await self._send_message(
            {
                "id": request_id,
                "result": result,
            }
        )

    async def _send_message(self, payload: CodexMessage) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    @staticmethod
    async def _iter_stream_chunks(stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
        while True:
            chunk = await stream.read(_STREAM_READ_CHUNK_SIZE)
            if not chunk:
                break
            yield chunk

    @staticmethod
    async def _iter_stream_lines(stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
        buffer = bytearray()
        async for chunk in _CodexAppServerSession._iter_stream_chunks(stream):
            buffer.extend(chunk)
            while True:
                newline_index = buffer.find(b"\n")
                if newline_index < 0:
                    break
                line = bytes(buffer[: newline_index + 1])
                del buffer[: newline_index + 1]
                yield line
        if buffer:
            yield bytes(buffer)

    async def _reader_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            async for line in self._iter_stream_lines(self._proc.stdout):
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                message = json.loads(raw)
                if (
                    "id" in message
                    and "method" not in message
                    and ("result" in message or "error" in message)
                ):
                    future = self._pending_requests.pop(int(message["id"]), None)
                    if future is not None and not future.done():
                        future.set_result(message)
                    continue
                await self._events.put(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — reader loop logs and exits on any unexpected error  # pragma: no cover - defensive
            logger.debug("Codex App Server reader loop ended: %s", exc)

    async def _stderr_loop(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            async for line in self._iter_stream_lines(self._proc.stderr):
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                if len(text) >= _STDERR_CHUNK_LIMIT:
                    text = text[:_STDERR_CHUNK_LIMIT] + "...[truncated]"
                self._recent_stderr.append(text)
                if len(self._recent_stderr) > 20:
                    self._recent_stderr.pop(0)
                logger.debug("codex app-server stderr: %s", text)
                self._note_stderr_gateway_error(text)
        except asyncio.CancelledError:
            raise

    def _note_stderr_gateway_error(self, text: str) -> None:
        """Attribute (and, when fatal, arm fast-fail for) a gateway rejection.

        The gateway cause is recorded into the shared watchdog slot the moment
        it is seen, so a stalled turn surfaces the real error. An auth-class
        (401/403) rejection arms fast-fail only once the CLI has also exhausted
        its own retry budget (a final ``Reconnecting N/N``); the two signals can
        arrive in either order, so both are tracked and fast-fail arms when both
        hold — a single blip the CLI recovers from never kills a healthy turn.
        """
        retry = _CODEX_STDERR_RETRY_EXHAUSTED_RE.search(text)
        if retry is not None and retry.group("n") == retry.group("total"):
            self._saw_retries_exhausted = True
        error = _parse_codex_gateway_error(text)
        if error is not None:
            native_forwarder_health.record_transport_failure(error.detail())
            if error.fatal:
                self._pending_fatal_gateway_error = error
        if self._pending_fatal_gateway_error is not None and self._saw_retries_exhausted:
            self._fatal_gateway_error = self._pending_fatal_gateway_error


@dataclass
class _CodexSessionState:
    app_session: _CodexAppServerSession | None = None
    signature: tuple[str | None, str, str, str] | None = None


class _AppSessionFactory(Protocol):
    """Constructor signature shared by ``_CodexAppServerSession`` and test fakes.

    Tests inject this via ``CodexExecutor(app_session_factory=...)`` to
    substitute a stub session (see ``tests/test_codex_executor.py``). The
    Protocol spells out the exact kwargs the factory receives.
    """

    def __call__(
        self,
        *,
        codex_path: str,
        cwd: str | None,
        env: dict[str, str],
        tool_executor: CodexToolExecutor | None,
        codex_config_overrides: list[str] | None,
        disable_native_tools: bool,
        bundle_dir: Path | None,
        skills_filter: str | list[str],
    ) -> _CodexAppServerSession: ...


def _default_app_session_factory(
    *,
    codex_path: str,
    cwd: str | None,
    env: dict[str, str],
    tool_executor: CodexToolExecutor | None,
    codex_config_overrides: list[str] | None,
    disable_native_tools: bool,
    bundle_dir: Path | None,
    skills_filter: str | list[str],
) -> _CodexAppServerSession:
    return _CodexAppServerSession(
        codex_path=codex_path,
        cwd=cwd,
        env=env,
        tool_executor=tool_executor,
        codex_config_overrides=codex_config_overrides,
        disable_native_tools=disable_native_tools,
        bundle_dir=bundle_dir,
        skills_filter=skills_filter,
    )


class CodexExecutor(Executor):
    def __init__(
        self,
        *,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        model: str | None = None,
        codex_path: str | None = None,
        app_session_factory: _AppSessionFactory | None = None,
        gateway: bool = False,
        databricks_profile: str | None = None,
        model_provider_override: str | None = None,
        gateway_host: str | None = None,
        base_url_override: str | None = None,
        gateway_auth_command: str | None = None,
        gateway_auth_refresh_interval_ms: str | None = None,
        enable_web_search: bool = True,
        disable_native_tools: bool = False,
        retry_policy: RetryPolicy | None = None,
        bundle_dir: Path | None = None,
        agent_name: str | None = None,
        skills_filter: str | list[str] = "all",
    ) -> None:
        """Create a CodexExecutor.

        :param cwd: Working directory for the Codex subprocess.
        :param os_env: Optional OS environment / sandbox spec.
        :param model: Override the model name, e.g. ``"databricks-gpt-5-4-mini"``.
        :param codex_path: Absolute path to a ``codex`` CLI binary.  When
            ``None`` the executor searches ``PATH``.
        :param app_session_factory: Test hook for injecting a stub session.
        :param gateway: When ``True``, route through a vendor-neutral gateway
            (base URL + bearer-token command + model). The Databricks AI
            gateway (Codex Responses API at ``/ai-gateway/codex/v1``) is one
            producer of this transport; generic providers are another.
        :param databricks_profile: Databricks-specific config profile from
            ``~/.databrickscfg``, e.g. ``"<your-profile>"``.  Only used on the
            Databricks producer path (deriving base URL / auth command from
            the profile when not supplied directly, and for token refresh).
            ``None`` falls back to ``DATABRICKS_CONFIG_PROFILE`` then the
            first valid profile.
        :param model_provider_override: A codex ``model_provider`` id to pin
            via a ``-c`` override, e.g. ``"openai"`` (force the built-in
            provider so a custom default in the user's ``~/.codex/config.toml``
            cannot shadow a subscription) or ``"Databricks"`` (a custom
            ``[model_providers.X]`` table from that same file, which this
            executor bridges into the per-session ``CODEX_HOME``). Set from
            ``HARNESS_CODEX_MODEL_PROVIDER``. Mutually exclusive with
            *gateway* — the gateway path pins its own generated provider.
        :param gateway_host: Gateway workspace host origin, e.g.
            ``"https://example.databricks.com"``.  Set from
            ``HARNESS_CODEX_GATEWAY_HOST`` (written by the Omnigent workflow
            layer). When set, skips profile host lookup and requires the
            gateway base URL and auth command values.
        :param base_url_override: Override the Codex gateway base URL instead
            of deriving it from the profile host.  Set from
            ``HARNESS_CODEX_GATEWAY_BASE_URL`` (written by the Omnigent workflow
            layer). Required whenever ``gateway_host`` is set.
        :param gateway_auth_command: Shell command that prints a bearer token,
            e.g.
            ``"databricks auth token --host https://example.databricks.com ..."``
            or ``"printf %s sk-or-..."``. Set from
            ``HARNESS_CODEX_GATEWAY_AUTH_COMMAND``.
        :param gateway_auth_refresh_interval_ms: Refresh cadence as a string,
            e.g. ``"900000"``. Set from
            ``HARNESS_CODEX_GATEWAY_AUTH_REFRESH_INTERVAL_MS``.
        :param enable_web_search: Leave Codex's built-in ``web_search`` tool
            enabled.  Set ``False`` to force the model to use only
            Omnigent-bridged tools.
        :param disable_native_tools: When True, disable supported native
            Codex tools for the turn.
        :param retry_policy: The spec's ``llm.retry`` budget. Threads
            ``policy.codex_cli.env()`` (e.g. ``OPENAI_MAX_RETRIES``,
            ``OPENAI_TIMEOUT``) into the Codex CLI subprocess
            environment so transient gateway failures are retried with
            spec-controlled backoff. ``None`` resolves to
            ``RetryPolicy()`` defaults — see Phase 1f of
            ``designs/RETRY_ACROSS_HARNESSES.md``.
        :param bundle_dir: The agent bundle's extracted on-disk path.
            When set, ``<bundle_dir>/skills/<dir>/SKILL.md`` files are
            symlinked into the per-conversation ``$CODEX_HOME/skills/``
            so Codex auto-discovers them. ``None`` skips bundle-skill
            wiring (host-installed ``~/.codex/skills/`` only, subject to
            ``skills_filter``).
        :param agent_name: Optional agent display name. Reserved for
            future use (e.g. namespacing bundled skills with an
            agent-specific prefix); currently unused by codex.
        :param skills_filter: Host-skill filter (``"all"`` / ``"none"``
            / ``list[str]``). ``"all"`` (default) symlinks every skill
            from sources (bundle skills + ``~/.codex/skills/``) into
            ``$CODEX_HOME/skills/``; ``"none"`` leaves the directory
            empty so Codex sees no skills; a list exposes only the
            named skills (looked up across all sources, bundle wins
            on name conflict).
        """
        self._cwd = cwd
        self._os_env_spec = os_env
        self._model_override = model
        self._model_provider_override = model_provider_override
        self._gateway = gateway
        self._databricks_profile = databricks_profile
        self._gateway_host = gateway_host.rstrip("/") if gateway_host else None
        self._base_url_override = base_url_override
        self._gateway_auth_command = gateway_auth_command
        self._gateway_auth_refresh_interval_ms = _parse_optional_int(
            gateway_auth_refresh_interval_ms
        )
        self._enable_web_search = enable_web_search
        self._disable_native_tools = disable_native_tools
        self._bundle_dir = bundle_dir
        self._agent_name = agent_name
        self._skills_filter = skills_filter
        resolved_codex = codex_path or _find_codex_cli()
        if not resolved_codex:
            raise ImportError(
                "CodexExecutor requires the 'codex' CLI on PATH. If codex is "
                "installed on a PATH the host daemon didn't inherit (e.g. an "
                f"nvm-managed bin dir), set {_CODEX_PATH_ENV}=/path/to/codex."
            )
        self._codex_path = resolved_codex
        self._env = _clean_codex_env(declared_passthrough(self._os_env_spec))
        # Retry policy → OpenAI SDK env vars (Codex uses the OpenAI
        # SDK internally). Speculative — empirical audit pending.
        self._retry_policy = retry_policy if retry_policy is not None else RetryPolicy()
        self._env.update(self._retry_policy.codex_cli.env())
        self._codex_config_overrides: list[str] = []
        if model_provider_override is not None and gateway:
            # Both would fight over model_provider in the -c overrides; the
            # AP producer must emit exactly one routing mechanism.
            raise OSError(
                "CodexExecutor received both gateway=True and "
                "model_provider_override; these routing mechanisms are "
                "mutually exclusive."
            )
        if model_provider_override is not None:
            # Pin the provider by name. json.dumps yields a valid TOML basic
            # string (proper quoting/escaping) for the -c override value.
            self._codex_config_overrides.append(
                f"model_provider={json.dumps(model_provider_override)}"
            )
        # True when the gateway transport was derived from a ~/.databrickscfg
        # profile (no gateway base URL supplied directly). Gates the
        # Databricks-specific default model in :meth:`run_turn`; the neutral
        # generic-provider gateway path leaves this False so it never selects
        # a ``databricks-*`` model.
        self._gateway_uses_databricks_profile = False
        if gateway:
            host = self._gateway_host
            # ``effective_model`` resolves to a concrete model for the codex
            # config. On the Databricks-profile-derivation branch (no gateway
            # host or base URL supplied directly) a ``databricks-*`` default is
            # legitimate Databricks behavior; on the directly-supplied neutral
            # gateway path the Omnigent producer must have resolved a model, and the
            # path never falls back to a ``databricks-*`` model.
            effective_model: str
            if host is None:
                # No gateway host supplied directly: derive the transport from
                # a Databricks profile (the Databricks producer's fallback).
                # Use the profile's own host so the base URL matches the token
                # the profile-pinned auth command mints (not a DATABRICKS_HOST
                # override that would point the base URL at another workspace).
                host = _databricks_gateway_host(databricks_profile)
                if not host:
                    raise OSError(
                        "CodexExecutor(gateway=True) requires gateway credentials via "
                        "the gateway base URL / auth command or a valid "
                        "~/.databrickscfg profile."
                    )
                host = host.rstrip("/")
                base_url = (
                    base_url_override
                    if base_url_override is not None
                    else _databricks_codex_base_url(host)
                )
                auth_command = (
                    gateway_auth_command
                    if gateway_auth_command is not None
                    else _databricks_codex_auth_command(host, databricks_profile)
                )
                # Databricks-profile path: select a gateway endpoint from the
                # catalog when no caller supplied one.
                self._gateway_uses_databricks_profile = True
                effective_model = (
                    model
                    or model_catalog.resolve_catalog_model("databricks", family="openai").model_id
                )
            else:
                if base_url_override is None:
                    raise OSError(
                        "CodexExecutor(gateway=True) with a gateway workspace host "
                        "requires HARNESS_CODEX_GATEWAY_BASE_URL."
                    )
                if gateway_auth_command is None:
                    raise OSError(
                        "CodexExecutor(gateway=True) with a gateway workspace host "
                        "requires HARNESS_CODEX_GATEWAY_AUTH_COMMAND."
                    )
                base_url = base_url_override
                auth_command = gateway_auth_command
                if model is None:
                    # Directly-supplied neutral gateway: the Omnigent producer always
                    # resolves a concrete model (spec > provider default >
                    # catalog default) before spawning. Fail loud rather than
                    # silently selecting a ``databricks-*`` default.
                    raise OSError(
                        "CodexExecutor(gateway=True) with a gateway base URL requires a "
                        "model: the Omnigent producer must resolve one before spawning."
                    )
                effective_model = model
            # ``DATABRICKS_HOST`` is read by the Databricks ``databricks auth
            # token`` fallback auth command; harmless for a generic gateway
            # whose auth command is a static ``printf %s <key>``.
            self._env["DATABRICKS_HOST"] = host
            self._codex_config_overrides.extend(
                _databricks_codex_config_overrides(
                    model=effective_model,
                    base_url=base_url,
                    auth_command=auth_command,
                    auth_refresh_interval_ms=self._gateway_auth_refresh_interval_ms,
                )
            )
        if not enable_web_search:
            # Disable Codex's built-in web_search tool so the model can only reach
            # tools exposed by Omnigent as dynamicTools. The top-level web_search
            # key accepts "live", "cached", or "disabled".
            self._codex_config_overrides.append('web_search="disabled"')
        self._tool_executor: CodexToolExecutor | None = None
        self._session_states: dict[str, _CodexSessionState] = {}
        self._app_session_factory: _AppSessionFactory = (
            app_session_factory
            if app_session_factory is not None
            else _default_app_session_factory
        )

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        return True

    def supports_live_message_queue(self) -> bool:
        return True

    async def interrupt_session(self, session_key: str) -> bool:
        state = self._session_states.get(session_key)
        if state is None or state.app_session is None:
            return False
        # Best-effort interrupt to halt the in-flight turn; a failure just
        # falls through to the close below.
        try:
            await asyncio.wait_for(
                state.app_session.interrupt_turn(),
                timeout=0.5,
            )
        except Exception as exc:  # noqa: BLE001 — interrupt is best-effort
            logger.warning(
                "Codex turn interrupt failed for session %s: %s",
                session_key,
                exc,
            )
        # Always drop the session (resets thread_id) so the next turn starts a
        # fresh thread and replays full history. A resumed thread sends only
        # the latest user message, which would bypass the runner's
        # "[System: interrupted]" marker and silently continue the abandoned
        # request. See claude_sdk_executor.interrupt_session for the rationale.
        try:
            await self.close_session(session_key)
            return True
        except Exception as exc:  # noqa: BLE001 — close failures surface via False return
            logger.warning(
                "Codex session close after interrupt failed for session %s: %s",
                session_key,
                exc,
            )
            return False

    async def enqueue_session_message(
        self, session_key: str, content: CodexEnqueuedContent
    ) -> bool:
        state = self._session_states.get(session_key)
        if state is None or state.app_session is None:
            return False
        return await state.app_session.enqueue_message(content)

    async def close_session(self, session_key: str) -> None:
        state = self._session_states.pop(session_key, None)
        if state is not None and state.app_session is not None:
            await state.app_session.close()

    async def close(self) -> None:
        keys = list(self._session_states.keys())
        for key in keys:
            await self.close_session(key)

    async def _ensure_app_session(
        self,
        state: _CodexSessionState,
        *,
        signature: tuple[str | None, str, str, str],
        effective_cwd: str,
    ) -> _CodexAppServerSession:
        if state.signature == signature and state.app_session is not None:
            return state.app_session
        if state.app_session is not None:
            await state.app_session.close()
        app_session = self._app_session_factory(
            codex_path=self._codex_path,
            cwd=effective_cwd,
            env=self._env,
            tool_executor=self._tool_executor,
            codex_config_overrides=self._codex_config_overrides,
            disable_native_tools=self._disable_native_tools,
            bundle_dir=self._bundle_dir,
            skills_filter=self._skills_filter,
        )
        state.app_session = app_session
        state.signature = signature
        return app_session

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        cfg = config or ExecutorConfig()
        session_key = _session_key(messages)
        state = self._session_states.setdefault(session_key, _CodexSessionState())
        # cfg.model (per-request /model override) wins over the spec default.
        # An unresolved default comes from the active provider catalog.
        # On the cli-config path (model_provider_override set) the codex binary
        # owns its own model list via its config.toml — omnigent does not pass a
        # model override to thread/create, letting the binary use its configured
        # default. Passing an unresolvable alias (e.g. gpt-5.6) would cause the
        # binary to call UC and get a validation error.
        model = cfg.model or self._model_override
        if self._model_provider_override is not None:
            model = None
        elif model is None:
            if self._gateway_uses_databricks_profile:
                resolution = await run_sync_on_thread(
                    model_catalog.resolve_catalog_model,
                    "databricks",
                    family="openai",
                )
                model = resolution.model_id
            else:
                # Codex's own login (ChatGPT account / API key), where codex is
                # the vocabulary authority: the bundled OpenAI catalog's newest
                # row is a bare family alias its backend rejects.
                model = CODEX_DEFAULT_MODEL
        effective_cwd = (
            self._cwd or (self._os_env_spec.cwd if self._os_env_spec else None) or os.getcwd()
        )
        signature = (
            model,
            system_prompt,
            effective_cwd,
            _tool_signature(tools),
        )
        try:
            reasoning_effort = validate_effort(
                cfg.extra.get("reasoning_effort"), "codex", CODEX_EFFORTS
            )
        except ValueError as exc:
            yield ExecutorError(message=describe_exception(exc), retryable=False)
            return

        app_session = await self._ensure_app_session(
            state,
            signature=signature,
            effective_cwd=effective_cwd,
        )
        sandbox_mode = _sandbox_mode(self._os_env_spec)
        if tools and sandbox_mode == "read-only":
            sandbox_mode = "workspace-write"

        try:
            async for event in app_session.run_turn(
                messages=messages,
                tools=tools,
                system_prompt=system_prompt,
                model=model,
                cwd=effective_cwd,
                sandbox=sandbox_mode,
                reasoning_effort=reasoning_effort,
            ):
                yield event
        except Exception as exc:  # noqa: BLE001 — executor boundary converts any error into an ExecutorError event
            yield ExecutorError(message=f"Codex executor error: {exc}")
