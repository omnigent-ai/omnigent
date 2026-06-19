"""CodexExecutor: run agents through the Codex App Server.

This executor keeps one long-lived ``codex app-server`` subprocess per
Omnigent session, persists the Codex thread across turns, and exposes
Omnigent tools to Codex as App Server ``dynamicTools``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeAlias

from omnigent.llms._usage_observer import notify_from_dict as _notify_usage_from_dict
from omnigent.reasoning_effort import CODEX_EFFORTS, validate_effort
from omnigent.runner.identity import OMNIGENT_SESSION_ENV_VAR
from omnigent.spec.types import RetryPolicy

from ._subprocess_lifecycle import close_subprocess_transport
from .databricks_executor import (
    _read_databrickscfg,
    _read_databrickscfg_host,
)
from .datamodel import OSEnvSandboxSpec, OSEnvSpec
from .executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    TextChunk,
    ToolArgs,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    ToolSpec,
    TurnComplete,
    classify_tool_result,
)

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
_TURN_COMPLETED_DRAIN_SECONDS = 1.0
# Wall-clock budget for the ``codex --version`` probe. A broken codex
# build that blocks (e.g. on stdin) must not stall session startup — on
# timeout the probe kills the process and reports the version as unknown.
_CODEX_VERSION_PROBE_TIMEOUT_SECONDS = 5.0
_STDERR_CHUNK_LIMIT = 65536
_STREAM_READ_CHUNK_SIZE = 65536
_OPENAI_CODEX_DEFAULT_MODEL = "gpt-5.4-mini"
# Databricks-specific default model for the Databricks-profile-derivation
# gateway path (no gateway base URL supplied directly). The neutral
# generic-provider gateway path never uses this — it requires the Omnigent producer
# to resolve a concrete model. Used only when constructing the codex config
# from ~/.databrickscfg credentials with no spec/override model.
_DATABRICKS_CODEX_DEFAULT_MODEL = "databricks-gpt-5-5"

# Files symlinked from the real CODEX_HOME into the per-session temp home.
# Symlinks (not copies) so credential refreshes in the real home propagate
# to running sessions without any action from Omnigent.
_CODEX_HOME_SYMLINK_FILES = ("auth.json",)

# Files copied (not symlinked) from the real CODEX_HOME into the per-session
# temp home. config.toml is intentionally copied so that an in-TUI ``/model``
# command writes only to the session's own private copy and never touches the
# shared ``~/.codex/config.toml``. This keeps model selection and cost-policy
# enforcement isolated between concurrent sessions.
_CODEX_HOME_COPY_FILES = ("config.toml",)

# Environment variables explicitly excluded from the codex subprocess even
# when their prefix is in the allowlist. ``OPENAI_API_KEY`` is stripped so
# the codex CLI uses its subscription auth (``auth.json``) rather than a
# developer API key that would charge separately.
_CODEX_ENV_DENY_EXACT: frozenset[str] = frozenset({"OPENAI_API_KEY"})


def _extract_codex_last_turn_usage(params: object) -> dict[str, int] | None:
    """Map a ``thread/tokenUsage/updated`` payload's ``last`` breakdown
    onto the wire shape that :class:`TurnComplete` consumes.

    Codex's ``inputTokens`` is INCLUSIVE of cached tokens (codex-rs:
    ``non_cached_input = input_tokens - cached_input_tokens``), and
    :func:`compute_llm_cost` expects ``input_tokens`` to be the *non-cached*
    portion with cache reads priced separately. So split ``cachedInputTokens``
    out into ``cache_read_input_tokens`` and keep only the remainder in
    ``input_tokens`` — otherwise cached tokens are billed at the full input
    rate. Mirrors the codex-native forwarder split.
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
    usage = {
        "input_tokens": input_total - cached,  # non-cached portion
        "output_tokens": int(last.get("outputTokens") or 0),
        "total_tokens": int(last.get("totalTokens") or 0),
    }
    if cached:
        usage["cache_read_input_tokens"] = cached
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
    if process is None or process.returncode is not None:
        return
    pid = process.pid
    if pid is not None:
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, signal.SIGTERM)
            return
    with suppress(ProcessLookupError, Exception):
        process.terminate()


def _kill_process_tree(process: _Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    pid = process.pid
    if pid is not None:
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, signal.SIGKILL)
            return
    with suppress(ProcessLookupError, Exception):
        process.kill()


def _find_codex_cli() -> str | None:
    return shutil.which("codex")


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


async def _create_subprocess_exec(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
    """
    Indirection point for ``asyncio.create_subprocess_exec``.

    Exists so tests can stub the subprocess creation without
    patching ``asyncio.create_subprocess_exec`` globally (patching
    ``omnigent.inner.codex_executor.asyncio.create_subprocess_exec``
    walks the dotted path into the real ``asyncio`` module
    singleton and leaks the mock into every other test in the
    process).

    :param args: Positional argv components forwarded to
        ``asyncio.create_subprocess_exec``.
    :param kwargs: Keyword args (``stdin``, ``stdout``, ``stderr``,
        ``env``, ``cwd``, ...) forwarded as-is.
    :returns: The spawned subprocess handle.
    """
    return await asyncio.create_subprocess_exec(*args, **kwargs)


def _clean_codex_env() -> dict[str, str]:
    """
    Build a filtered copy of ``os.environ`` for the codex subprocess.

    Uses a prefix allowlist so only known-safe categories pass through
    (proxy settings, locale, OpenAI retry knobs, etc.). Keys in
    :data:`_CODEX_ENV_DENY_EXACT` are excluded even when their prefix
    matches; ``OPENAI_API_KEY`` is stripped so the codex CLI falls
    back to subscription auth (``auth.json``) rather than a developer
    API key that would charge separately.

    :returns: Filtered environment dict.
    """
    env: dict[str, str] = {}
    allow_prefixes = (
        "OPENAI_",
        "HTTP_",
        "HTTPS_",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_",
        "REQUESTS_",
        "CODEX_HOME",
        "XDG_",
        "LANG",
        "LC_",
    )
    allow_exact = {
        "HOME",
        "PATH",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "PYTHONUTF8",
        "DATABRICKS_BEARER",  # explicit CI/integration bearer used by auth.command
        "DATABRICKS_CODEX_TOKEN",  # env_key referenced by ~/.codex/config.toml's DB provider
        OMNIGENT_SESSION_ENV_VAR,  # "inside Omnigent" marker (CLAUDE_CODE/CODEX analog)
    }
    for key, value in os.environ.items():
        if key in _CODEX_ENV_DENY_EXACT:
            continue
        if key in allow_exact or key.startswith(allow_prefixes):
            env[key] = value
    return env


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
    user's ``~/.codex/skills/`` plus any ``<bundle>/skills/``).

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
    available: dict[str, Path] = {}
    for source in sources:
        if not source.is_dir():
            continue
        for child in sorted(source.iterdir()):
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
        return

    for name in names:
        link_path = target_dir / name
        if link_path.exists() or link_path.is_symlink():
            continue
        try:
            # Resolve to absolute so the symlink doesn't break when
            # the source was a relative path (relative symlinks resolve
            # against the link's parent, not the original cwd).
            link_path.symlink_to(available[name].resolve())
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
            shutil.copytree(available[name], link_path)


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
    skill_sources: list[Path] = []
    if bundle_dir is not None:
        bundle_skills = bundle_dir / "skills"
        if bundle_skills.is_dir():
            skill_sources.append(bundle_skills)
    host_skills = Path.home() / ".codex" / "skills"
    if host_skills.is_dir():
        skill_sources.append(host_skills)
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


def _populate_codex_home_config(target_dir: Path, source_dir: Path) -> None:
    """
    Bridge user config files from the real ``CODEX_HOME`` into the temp one.

    The executor overrides ``CODEX_HOME`` to a per-conversation temp
    directory so session data (conversation history, etc.) stays isolated
    from the user's ``~/.codex/``. However, the codex CLI also reads
    authentication tokens (``auth.json``) and provider configuration
    (``config.toml``) from ``$CODEX_HOME``. This helper bridges those
    files into the temp directory:

    - ``auth.json`` is **symlinked** so OAuth token refreshes written to
      the real home propagate to running sessions without delay.
    - ``config.toml`` is **copied** so an in-TUI ``/model`` command writes
      only to the session's own private copy and never mutates the shared
      ``~/.codex/config.toml``. This keeps model selection and cost-policy
      enforcement isolated between concurrent sessions.

    :param target_dir: The per-conversation temp ``CODEX_HOME``
        directory. Must already exist.
    :param source_dir: The primary ``CODEX_HOME`` directory
        (typically ``$CODEX_HOME`` or ``~/.codex``). Missing files are
        skipped.
    """
    if not source_dir.is_dir():
        return

    for filename in _CODEX_HOME_SYMLINK_FILES:
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

    for filename in _CODEX_HOME_COPY_FILES:
        source_file = source_dir / filename
        if not source_file.is_file():
            continue
        dest_path = target_dir / filename
        if dest_path.exists() or dest_path.is_symlink():
            continue
        shutil.copy2(source_file, dest_path)


def _databricks_codex_base_url(host: str) -> str:
    """Return the Unity AI Gateway Codex Responses base URL for *host*."""
    return f"{host.rstrip('/')}/ai-gateway/codex/v1"


def _databricks_codex_auth_command(host: str, profile: str | None = None) -> str:
    """Return the legacy Databricks CLI auth helper command for Codex.

    :param host: Databricks workspace host, e.g.
        ``"https://example.databricks.com"``.
    :param profile: Optional ``~/.databrickscfg`` profile name, e.g.
        ``"oss"``. Preferred over ``--host`` when known: two profiles can
        share one host, which makes ``databricks auth token --host`` fail
        ("Use --profile to specify which profile") → empty token → 401.
        ``--profile`` is always unambiguous.
    :returns: Shell command that prints a bearer token.
    """
    # --profile is unambiguous; --host fails when two profiles share a host.
    selector = (
        f"--profile {json.dumps(profile)}" if profile else f"--host {json.dumps(host.rstrip('/'))}"
    )
    # `--force-refresh` proactively refreshes a still-valid cached token
    # (guards against a mid-session 401 on long gateway connections) but
    # only exists in Databricks CLI >= v0.296.0. Probe `--help` and pass it
    # only when supported: older CLIs reject the unknown flag → empty token
    # → silent 401. Plain `auth token` still auto-refreshes expired tokens.
    return (
        'if [ -n "${DATABRICKS_BEARER:-}" ]; then '
        'printf "%s\\n" "$DATABRICKS_BEARER"; '
        "else force=''; "
        "if databricks auth token --help 2>&1 | grep -q force-refresh; "
        "then force=--force-refresh; fi; "
        "env -u DATABRICKS_CONFIG_PROFILE "
        f"databricks auth token {selector} "
        "$force --output json | jq -r '.access_token'; fi"
    )


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
) -> str | list[dict[str, Any]]:
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
) -> str | list[dict[str, Any]]:
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


def _prompt_for_turn(
    messages: list[Message], *, is_new_thread: bool
) -> str | list[dict[str, Any]]:
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
    blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
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
    items: list[dict[str, Any]] = []
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
        self._recent_stderr: list[str] = []
        self._recent_events: list[CodexMessage] = []
        self._process_cwd: Path | None = None
        # Private CODEX_HOME so the subprocess never writes to the user's ~/.codex/.
        self._codex_home_dir: Path | None = None
        # Most recent ``thread/tokenUsage/updated`` payload's ``last``
        # turn breakdown, mapped to the wire shape. Consumed (and cleared)
        # on the next ``turn/completed`` so each TurnComplete carries the
        # usage for the turn that just finished.
        self._last_turn_usage: dict[str, int] | None = None

    async def start(self) -> None:
        if self._started:
            return
        self._loop = asyncio.get_running_loop()
        codex_home_root = Path(tempfile.gettempdir())
        if self._cwd:
            codex_home_root = Path(self._cwd) / ".codex-tmp"
            codex_home_root.mkdir(parents=True, exist_ok=True)
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
        _populate_codex_home_config(
            self._codex_home_dir,
            _codex_home_config_source_from_env(),
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
                start_new_session=(os.name == "posix"),
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
            wait_closed = getattr(stdin, "wait_closed", None)
            if callable(wait_closed):
                with suppress(Exception):
                    await wait_closed()
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
        model: str,
        cwd: str,
        sandbox: str,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        await self.start()
        assert self._proc is not None

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

        assert self.thread_id is not None
        prompt = _prompt_for_turn(messages, is_new_thread=is_new_thread)
        if isinstance(prompt, list):
            turn_input = _to_codex_input_items(prompt)
        else:
            turn_input = [{"type": "text", "text": prompt}]
        turn_params: CodexParams = {
            "threadId": self.thread_id,
            "input": turn_input,
        }
        if reasoning_effort:
            turn_params["effort"] = reasoning_effort
        start_response = await self._request(
            "turn/start",
            turn_params,
        )
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
                try:
                    while True:
                        done, _ = await asyncio.wait(
                            {event_task}, timeout=_TURN_EVENT_WARN_SECONDS
                        )
                        if event_task in done:
                            break
                        idle_seconds += _TURN_EVENT_WARN_SECONDS
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
                    self._last_turn_usage = _extract_codex_last_turn_usage(params)
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
        except asyncio.CancelledError:
            raise


@dataclass
class _CodexSessionState:
    app_session: _CodexAppServerSession | None = None
    signature: tuple[str, str, str, str] | None = None


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
            When set, ``<bundle_dir>/skills/<name>/SKILL.md`` files are
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
            raise ImportError("CodexExecutor requires the 'codex' CLI on PATH.")
        self._codex_path = resolved_codex
        self._env = _clean_codex_env()
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
                creds = _read_databrickscfg(databricks_profile)
                host = (
                    creds.host
                    if creds is not None
                    else _read_databrickscfg_host(databricks_profile)
                )
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
                # Databricks-profile path: a Databricks default is legitimate.
                self._gateway_uses_databricks_profile = True
                effective_model = model or _DATABRICKS_CODEX_DEFAULT_MODEL
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
        signature: tuple[str, str, str, str],
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
        # cfg.model (per-request /model override) wins over the spec
        # default (HARNESS_CODEX_MODEL → self._model_override). The final
        # fallback is the Databricks default only on the Databricks-profile
        # gateway path; the neutral gateway path (and the built-in path) never
        # select a ``databricks-*`` model.
        model = (
            cfg.model
            or self._model_override
            or (
                _DATABRICKS_CODEX_DEFAULT_MODEL
                if self._gateway_uses_databricks_profile
                else _OPENAI_CODEX_DEFAULT_MODEL
            )
        )
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
            yield ExecutorError(message=str(exc), retryable=False)
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
