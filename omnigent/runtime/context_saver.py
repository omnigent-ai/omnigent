"""Context Saver settings, contracts, and broad-read classification.

The module is deliberately independent of harness payload formats. Runner and
native-hook adapters normalize their calls here so the decision policy stays in
one place.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from omnigent.policies.builtins._shell import (
    is_unresolved_invocation,
    real_invocation_tokens,
    split_command_segments,
    unwrap_shell_command,
)

_logger = logging.getLogger(__name__)

DEFAULT_FOCUSED_READ_MODEL = "databricks/context-saver-cheap"
DEFAULT_MIN_LINES = 350
DEFAULT_MAX_EXCERPT_LINES = 80
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_MAX_FILES = 4
DEFAULT_MAX_TOTAL_BYTES = 1_000_000
CONTEXT_SAVER_AVAILABLE_ENV = "_OMNIGENT_CONTEXT_SAVER_AVAILABLE"
_ENFORCED_NATIVE_HARNESSES = frozenset({"claude-native", "codex-native"})
_UNSUPPORTED_HARNESSES = frozenset({"copilot", "cursor"})


def context_saver_process_available() -> bool:
    """Return the server-propagated Context Saver hard gate for this process."""
    raw = os.environ.get(CONTEXT_SAVER_AVAILABLE_ENV)
    if raw is None:
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class FocusedReadSettings:
    """Configuration for the Focused Read technique."""

    enabled: bool = True
    min_lines: int = DEFAULT_MIN_LINES
    worker_model: str = DEFAULT_FOCUSED_READ_MODEL
    worker_provider: str | None = None
    allow_source_upload: bool = False
    max_excerpt_lines: int = DEFAULT_MAX_EXCERPT_LINES
    request_timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT_SECONDS
    max_files: int = DEFAULT_MAX_FILES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES


@dataclass(frozen=True)
class CodeOutlineSettings:
    """Reserved configuration for the future Code Outline technique."""

    enabled: bool = False
    min_lines: int = 200


ContextSaverTechniqueSettings = FocusedReadSettings | CodeOutlineSettings


@dataclass(frozen=True)
class ContextSaverSettings:
    """Effective Context Saver configuration for one session workspace."""

    enabled: bool = False
    techniques: Mapping[str, ContextSaverTechniqueSettings] = field(
        default_factory=lambda: {
            "focused_read": FocusedReadSettings(),
            "code_outline": CodeOutlineSettings(),
        }
    )

    @property
    def focused_read(self) -> FocusedReadSettings:
        """Return Focused Read settings, including defaults when absent."""
        value = self.techniques.get("focused_read")
        return value if isinstance(value, FocusedReadSettings) else FocusedReadSettings()


class ContextSaverAction(str, Enum):
    """Decision returned by every Context Saver classifier."""

    ALLOW = "allow"
    REDIRECT = "redirect"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ContextSaverDecision:
    """Typed classifier result with a stable audit reason."""

    action: ContextSaverAction
    reason: str
    paths: tuple[str, ...] = ()
    total_lines: int | None = None


def harness_supports_context_saver(harness: str | None) -> bool:
    """Return whether Omnigent can enforce the pre-read gate for a harness."""
    if not harness:
        return True
    from omnigent.harness_aliases import canonicalize_harness, is_native_harness

    canonical = canonicalize_harness(harness) or harness
    return canonical not in _UNSUPPORTED_HARNESSES and (
        not is_native_harness(canonical) or canonical in _ENFORCED_NATIVE_HARNESSES
    )


def context_saver_has_filesystem_access(
    *,
    harness: str | None,
    os_env_available: bool,
) -> bool:
    """Return whether Context Saver may read files for this agent."""
    from omnigent.harness_aliases import is_native_harness

    return harness_supports_context_saver(harness) and (
        os_env_available or is_native_harness(harness)
    )


@dataclass(frozen=True)
class ContextFile:
    """One sandbox-authorized text file supplied to a technique."""

    path: str
    content: str
    total_lines: int
    total_bytes: int


class ContextFileReader(Protocol):
    """Sandbox-aware reader injected by the runner."""

    async def read(self, path: str) -> ContextFile: ...


@dataclass(frozen=True)
class ContextReadRequest:
    """Portable input passed to a Context Saver technique."""

    paths: tuple[str, ...]
    question: str
    reader: ContextFileReader
    requested_output_budget: int
    session_id: str | None = None
    trace_id: str | None = None


@dataclass(frozen=True)
class ContextReadResult:
    """Compact result returned to the primary model."""

    technique: str
    content: str
    source_paths: tuple[str, ...]
    relevant_line_ranges: Mapping[str, tuple[tuple[int, int], ...]] = field(default_factory=dict)
    input_bytes: int = 0
    output_bytes: int = 0
    worker_input_tokens: int | None = None
    worker_output_tokens: int | None = None
    latency_ms: int | None = None
    failure: str | None = None
    worker_model_reported: str | None = None


class ContextSaverTechnique(Protocol):
    """Interface implemented by every context-saving technique."""

    name: str

    async def render(self, request: ContextReadRequest) -> ContextReadResult: ...


@dataclass(frozen=True)
class FocusedReadWorkerResult:
    """Worker text, token usage, and provider-reported model."""

    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    reported_model: str | None = None


class FocusedReadWorker(Protocol):
    """Cheap-model boundary injected into Focused Read."""

    async def focus(
        self,
        *,
        files: Sequence[ContextFile],
        question: str,
        model: str,
        allow_source_upload: bool,
        timeout_seconds: int,
        max_excerpt_lines: int,
        output_budget: int,
    ) -> FocusedReadWorkerResult: ...


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    return value


def _bool(value: object, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _positive_int(value: object, field_name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def validate_focused_read_worker_model(
    model: object,
    *,
    allow_source_upload: bool,
) -> str:
    """Validate the provider route before source text crosses the worker boundary.

    Databricks aliases remain the safe default. Other supported providers are
    available only after the user or deployment explicitly consents to sending
    source through that provider. Requiring an explicit provider prefix avoids
    the generic client's implicit ``openai`` fallback for bare model names.

    :param model: Provider-prefixed worker model, e.g. ``"openai/gpt-4o-mini"``.
    :param allow_source_upload: Explicit consent for non-Databricks providers.
    :returns: The stripped, validated model string.
    :raises ValueError: If the model is ambiguous, unsupported, or not approved.
    """
    if not isinstance(model, str) or not model.strip():
        raise ValueError(
            "context_saver.techniques.focused_read.worker_model must be a non-empty string"
        )
    normalized = model.strip()
    if "/" not in normalized:
        raise ValueError(
            "context_saver.techniques.focused_read.worker_model must include an explicit "
            "provider prefix, for example 'databricks/model' or 'openai/model'"
        )

    from omnigent.errors import OmnigentError
    from omnigent.llms.routing import parse_model_string

    try:
        routed = parse_model_string(normalized)
    except OmnigentError as exc:
        raise ValueError(
            "context_saver.techniques.focused_read.worker_model uses an unsupported provider"
        ) from exc
    if not routed.model.strip():
        raise ValueError(
            "context_saver.techniques.focused_read.worker_model must include a model name"
        )
    if routed.provider != "databricks" and not allow_source_upload:
        raise ValueError(
            "non-Databricks Context Saver workers require "
            "context_saver.techniques.focused_read.allow_source_upload: true "
            "in user or deployment configuration"
        )
    return normalized


def parse_context_saver_settings(raw: object) -> ContextSaverSettings:
    """Parse and validate a top-level ``context_saver`` config block."""
    block = _mapping(raw, "context_saver")
    techniques = _mapping(block.get("techniques"), "context_saver.techniques")
    unknown_techniques = set(techniques) - {"focused_read", "code_outline"}
    if unknown_techniques:
        names = ", ".join(sorted(unknown_techniques))
        raise ValueError(f"unknown Context Saver technique(s): {names}")
    focused = _mapping(techniques.get("focused_read"), "context_saver.techniques.focused_read")
    outline = _mapping(techniques.get("code_outline"), "context_saver.techniques.code_outline")
    outline_enabled = _bool(
        outline.get("enabled"),
        "context_saver.techniques.code_outline.enabled",
        False,
    )
    if outline_enabled:
        raise ValueError("Context Saver technique 'code_outline' is not implemented")
    allow_source_upload = _bool(
        focused.get("allow_source_upload"),
        "context_saver.techniques.focused_read.allow_source_upload",
        False,
    )
    raw_worker_provider = focused.get("worker_provider")
    if raw_worker_provider is None:
        worker_provider = None
    elif isinstance(raw_worker_provider, str) and raw_worker_provider.strip():
        worker_provider = raw_worker_provider.strip()
    else:
        raise ValueError(
            "context_saver.techniques.focused_read.worker_provider must be a non-empty string"
        )
    worker_model = validate_focused_read_worker_model(
        focused.get("worker_model", DEFAULT_FOCUSED_READ_MODEL),
        allow_source_upload=allow_source_upload,
    )
    return ContextSaverSettings(
        enabled=_bool(block.get("enabled"), "context_saver.enabled", False),
        techniques={
            "focused_read": FocusedReadSettings(
                enabled=_bool(
                    focused.get("enabled"),
                    "context_saver.techniques.focused_read.enabled",
                    True,
                ),
                min_lines=_positive_int(
                    focused.get("min_lines"),
                    "context_saver.techniques.focused_read.min_lines",
                    DEFAULT_MIN_LINES,
                ),
                worker_model=worker_model,
                worker_provider=worker_provider,
                allow_source_upload=allow_source_upload,
                max_excerpt_lines=_positive_int(
                    focused.get("max_excerpt_lines"),
                    "context_saver.techniques.focused_read.max_excerpt_lines",
                    DEFAULT_MAX_EXCERPT_LINES,
                ),
                request_timeout_seconds=_positive_int(
                    focused.get("request_timeout_seconds"),
                    "context_saver.techniques.focused_read.request_timeout_seconds",
                    DEFAULT_REQUEST_TIMEOUT_SECONDS,
                ),
                max_files=_positive_int(
                    focused.get("max_files"),
                    "context_saver.techniques.focused_read.max_files",
                    DEFAULT_MAX_FILES,
                ),
                max_total_bytes=_positive_int(
                    focused.get("max_total_bytes"),
                    "context_saver.techniques.focused_read.max_total_bytes",
                    DEFAULT_MAX_TOTAL_BYTES,
                ),
            ),
            "code_outline": CodeOutlineSettings(
                enabled=outline_enabled,
                min_lines=_positive_int(
                    outline.get("min_lines"),
                    "context_saver.techniques.code_outline.min_lines",
                    200,
                ),
            ),
        },
    )


def _deep_merge(base: Mapping[str, object], override: Mapping[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in override.items():
        prior = merged.get(key)
        if isinstance(prior, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(prior, value)
        else:
            merged[key] = value
    return merged


def resolve_context_saver_settings(
    global_block: object,
    project_block: object,
) -> ContextSaverSettings:
    """Resolve user settings with safe project-level tuning fields.

    Worker routing, provider credentials, and source-upload consent are
    user/deployment trust decisions. A checked-in project may enable Context
    Saver and tune its thresholds, but cannot redirect source to another
    provider or grant the consent needed for a non-Databricks worker.

    :param global_block: User- or deployment-level ``context_saver`` block.
    :param project_block: Project-level ``context_saver`` block.
    :returns: Validated effective settings.
    """
    global_mapping = _mapping(global_block, "context_saver")
    project_mapping = _mapping(project_block, "context_saver")
    raw_project_techniques = project_mapping.get("techniques")
    if "techniques" in project_mapping and not isinstance(raw_project_techniques, Mapping):
        raise ValueError("project context_saver.techniques must be a mapping")
    project_techniques = _mapping(
        raw_project_techniques,
        "context_saver.techniques",
    )
    raw_project_focused = project_techniques.get("focused_read")
    if "focused_read" in project_techniques and not isinstance(raw_project_focused, Mapping):
        raise ValueError("project context_saver.techniques.focused_read must be a mapping")
    project_focused = _mapping(
        raw_project_focused,
        "context_saver.techniques.focused_read",
    )
    restricted = {"worker_model", "worker_provider", "allow_source_upload"} & set(project_focused)
    if restricted:
        fields = ", ".join(sorted(restricted))
        raise ValueError(
            f"project Context Saver configuration cannot set {fields}; "
            "configure worker routing and source-upload consent in the user-level config"
        )
    return parse_context_saver_settings(_deep_merge(global_mapping, project_mapping))


def load_context_saver_settings(workspace: Path | None = None) -> ContextSaverSettings:
    """Load and resolve user and project Context Saver settings."""
    from omnigent.config import load_global_config, load_local_config

    global_block = load_global_config().get("context_saver")
    local_path = (workspace or Path.cwd()) / ".omnigent" / "config.yaml"
    project_block = load_local_config(local_path).get("context_saver")
    return resolve_context_saver_settings(global_block, project_block)


def classify_file_read(
    *,
    path: str,
    offset: object,
    limit: object,
    total_lines: int,
    settings: ContextSaverSettings,
) -> ContextSaverDecision:
    """Classify a normalized direct file read."""
    if not settings.enabled:
        return ContextSaverDecision(ContextSaverAction.ALLOW, "disabled", (path,), total_lines)
    focused = settings.focused_read
    if total_lines < focused.min_lines:
        return ContextSaverDecision(
            ContextSaverAction.ALLOW, "file_below_min_lines", (path,), total_lines
        )
    if _is_explicit_bounded_read(limit, max_lines=focused.min_lines):
        return ContextSaverDecision(
            ContextSaverAction.ALLOW, "explicit_bounded_range", (path,), total_lines
        )
    reason = "focused_read_available" if focused.enabled else "no_enabled_technique"
    return ContextSaverDecision(ContextSaverAction.REDIRECT, reason, (path,), total_lines)


def _is_explicit_bounded_read(limit: object, *, max_lines: int) -> bool:
    return isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= max_lines


@dataclass(frozen=True)
class ShellReadCandidates:
    """Broad file operands extracted from a shell command."""

    paths: tuple[str, ...]
    unknown: bool = False


_BROAD_READ_COMMANDS = frozenset({"cat", "less", "more", "bat"})
_BOUNDED_READ_COMMANDS = frozenset({"head", "tail"})
_REDIRECTION_RE = re.compile(
    r"^(?:(?:\d*)(?P<op><<<|<<-|<<|<&|<>|>>|>\||>&|<|>)"
    r"|(?P<amp_op>&>>?))(?P<target>.*)$"
)


def _shell_read_tokens(tokens: Sequence[str]) -> list[str]:
    """Remove non-read redirections and retain redirected input files."""
    arguments: list[str] = []
    input_paths: list[str] = []
    index = 0
    while index < len(tokens):
        match = _REDIRECTION_RE.fullmatch(tokens[index])
        if match is None:
            arguments.append(tokens[index])
            index += 1
            continue
        operator = match.group("op") or match.group("amp_op")
        target = match.group("target")
        if not target and index + 1 < len(tokens):
            index += 1
            target = tokens[index]
        # Other forms are output, descriptor duplication, heredocs, or here-strings.
        if operator in {"<", "<>"} and target:
            input_paths.append(target)
        index += 1
    return [*arguments, *input_paths]


def _shell_path_from_cwd(raw_path: str, cwd: Path | None) -> str:
    """Return a shell operand relative to a statically known command cwd."""
    path = Path(raw_path)
    if cwd is None or path.is_absolute() or raw_path.startswith("~") or cwd == Path("."):
        return raw_path
    return str(cwd / path)


def _head_tail_operands(
    tokens: Sequence[str],
    *,
    command: str,
    max_bounded_lines: int,
) -> tuple[str, ...]:
    """Return operands when a ``head`` or ``tail`` invocation can read broadly."""
    operands: list[str] = []
    broad = False
    options = True
    index = 1

    def _line_count_is_broad(raw_count: str) -> bool:
        if not re.fullmatch(r"[+-]?\d+", raw_count):
            return True
        if raw_count.startswith("+"):
            return True
        count = int(raw_count)
        # GNU head uses a negative count to mean "all but the last N lines".
        if command == "head" and count < 0:
            return True
        return abs(count) > max_bounded_lines

    while index < len(tokens):
        token = tokens[index]
        if options and token == "--":
            options = False
            index += 1
            continue
        if options and token in {"-n", "--lines"}:
            if index + 1 >= len(tokens):
                broad = True
                index += 1
                continue
            broad = broad or _line_count_is_broad(tokens[index + 1])
            index += 2
            continue
        if options and token in {"-c", "--bytes"}:
            # Byte windows are not comparable with the configured line budget.
            # Treat them as broad until Context Saver has a byte-output budget.
            broad = True
            index += 2 if index + 1 < len(tokens) else 1
            continue
        if options and re.fullmatch(r"-n[+-]?\d+", token):
            broad = broad or _line_count_is_broad(token[2:])
            index += 1
            continue
        if options and re.fullmatch(r"-c[+-]?\d+", token):
            broad = True
            index += 1
            continue
        if options and re.fullmatch(r"-\d+", token):
            broad = broad or int(token[1:]) > max_bounded_lines
            index += 1
            continue
        if options and token.startswith("--lines="):
            broad = broad or _line_count_is_broad(token.partition("=")[2])
            index += 1
            continue
        if options and token.startswith("--bytes="):
            broad = True
            index += 1
            continue
        if options and re.fullmatch(r"-[qv]+", token):
            index += 1
            continue
        if options and token.startswith("-"):
            broad = True
            index += 1
            continue
        operands.append(token)
        index += 1
    return tuple(operands) if broad else ()


def shell_read_candidates(
    command: str,
    *,
    max_bounded_lines: int = DEFAULT_MIN_LINES,
    initial_cwd: Path | None = None,
) -> ShellReadCandidates:
    """Extract operands of classifiable broad shell reads."""
    found: list[str] = []
    unknown = False
    pending = list(split_command_segments(command))
    nesting = 0
    shell_cwd: Path | None = Path(".") if initial_cwd is None else initial_cwd
    while pending:
        segment = pending.pop(0)
        try:
            tokens = real_invocation_tokens(_shell_read_tokens(shlex.split(segment)))
        except ValueError:
            unknown = True
            continue
        if not tokens:
            continue
        if is_unresolved_invocation(tokens):
            unknown = True
            continue
        inner = unwrap_shell_command(tokens)
        if inner is not None and nesting < 4:
            nesting += 1
            pending.extend(split_command_segments(inner))
            continue
        name = Path(tokens[0]).name.lower()
        if name == "cd":
            args = tokens[1:]
            if args[:1] == ["--"]:
                args = args[1:]
            if len(args) != 1 or args[0] == "-" or any(char in args[0] for char in "$`*?[]{}"):
                shell_cwd = None
                unknown = True
                continue
            target = Path(args[0])
            if target.is_absolute():
                shell_cwd = target
            elif shell_cwd is None:
                unknown = True
            else:
                shell_cwd = shell_cwd / target
            continue
        if name in _BOUNDED_READ_COMMANDS:
            operands = _head_tail_operands(
                tokens,
                command=name,
                max_bounded_lines=max_bounded_lines,
            )
            if shell_cwd is None and operands:
                unknown = True
            found.extend(_shell_path_from_cwd(path, shell_cwd) for path in operands)
            continue
        if name == "sed":
            operands = [token for token in tokens[1:] if not token.startswith("-")]
            if len(operands) < 2:
                continue
            script, paths = operands[0], operands[1:]
            bounded = False
            zero_delimited = False
            for token in tokens[1:]:
                if token == "--":
                    break
                if token == "--null-data" or (
                    token.startswith("-") and not token.startswith("--") and "z" in token[1:]
                ):
                    zero_delimited = True
                    break
            if not zero_delimited and any(
                token == "-n" or token.startswith("-n") for token in tokens[1:]
            ):
                match = re.fullmatch(r"(\d+)(?:,(\d+))?p", script)
                if match:
                    start = int(match.group(1))
                    end = int(match.group(2) or match.group(1))
                    bounded = start <= end and end - start + 1 <= max_bounded_lines
            if not bounded:
                if shell_cwd is None and paths:
                    unknown = True
                found.extend(_shell_path_from_cwd(path, shell_cwd) for path in paths)
            continue
        if name not in _BROAD_READ_COMMANDS:
            continue
        operands = [
            token
            for token in tokens[1:]
            if token != "--" and token != "-" and not token.startswith("-")
        ]
        if shell_cwd is None and operands:
            unknown = True
        found.extend(_shell_path_from_cwd(path, shell_cwd) for path in operands)
    return ShellReadCandidates(tuple(dict.fromkeys(found)), unknown)


def classify_shell_read(
    command: str,
    *,
    cwd: Path,
    settings: ContextSaverSettings,
    line_counter: Callable[[Path], int | None],
    initial_cwd: Path | None = None,
    is_outside_workspace: Callable[[Path], bool] | None = None,
) -> ContextSaverDecision:
    """Classify classifiable broad reads embedded in a shell command."""
    if not settings.enabled:
        return ContextSaverDecision(ContextSaverAction.ALLOW, "disabled")
    candidates = shell_read_candidates(
        command,
        max_bounded_lines=settings.focused_read.min_lines,
        initial_cwd=initial_cwd,
    )
    resolved_candidates: list[tuple[str, Path]] = []
    outside_workspace: list[str] = []
    line_counts: dict[str, int | None] = {}
    for raw_path in candidates.paths:
        try:
            path = Path(raw_path).expanduser()
        except (OSError, RuntimeError):
            line_counts[raw_path] = None
            continue
        resolved = path if path.is_absolute() else cwd / path
        if is_outside_workspace is not None and is_outside_workspace(resolved):
            outside_workspace.append(raw_path)
        else:
            resolved_candidates.append((raw_path, resolved))
    if outside_workspace:
        return ContextSaverDecision(
            ContextSaverAction.REDIRECT,
            "outside_workspace_broad_read",
            tuple(outside_workspace),
        )
    for raw_path, resolved in resolved_candidates:
        line_counts[raw_path] = line_counter(resolved)
    return classify_shell_candidates(candidates, settings=settings, line_counts=line_counts)


def classify_shell_candidates(
    candidates: ShellReadCandidates,
    *,
    settings: ContextSaverSettings,
    line_counts: Mapping[str, int | None],
) -> ContextSaverDecision:
    """Classify extracted shell operands using sandbox-authorized line counts."""
    if not settings.enabled:
        return ContextSaverDecision(ContextSaverAction.ALLOW, "disabled")
    large: list[str] = []
    total_lines = 0
    unresolved_candidate = False
    for raw_path in candidates.paths:
        count = line_counts.get(raw_path)
        if count is None:
            unresolved_candidate = True
            continue
        total_lines += count
        if count >= settings.focused_read.min_lines:
            large.append(raw_path)
    if large:
        reason = (
            "focused_read_available" if settings.focused_read.enabled else "no_enabled_technique"
        )
        return ContextSaverDecision(ContextSaverAction.REDIRECT, reason, tuple(large), total_lines)
    if candidates.unknown or unresolved_candidate:
        return ContextSaverDecision(ContextSaverAction.UNKNOWN, "unclassifiable_shell_read")
    return ContextSaverDecision(ContextSaverAction.ALLOW, "no_large_broad_read")


def count_text_lines(path: Path) -> int | None:
    """Count a local text file without retaining or returning its body."""
    try:
        count = 0
        saw_data = False
        ended_with_newline = False
        with path.open("rb") as file:
            while chunk := file.read(64 * 1024):
                if b"\x00" in chunk:
                    return None
                saw_data = True
                count += chunk.count(b"\n")
                ended_with_newline = chunk.endswith(b"\n")
        return count + int(saw_data and not ended_with_newline)
    except OSError:
        return None


def classify_native_tool_call(
    tool_name: object,
    tool_input: object,
    *,
    workspace: Path,
    settings: ContextSaverSettings,
) -> ContextSaverDecision:
    """Normalize a Codex/Claude native tool payload into the shared policy."""
    if not isinstance(tool_name, str) or not isinstance(tool_input, Mapping):
        return ContextSaverDecision(ContextSaverAction.UNKNOWN, "malformed_native_tool")
    workspace_root = workspace.resolve()

    def _resolve_workspace_path(path: Path) -> tuple[Path | None, bool]:
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError, ValueError):
            return None, False
        try:
            resolved.relative_to(workspace_root)
        except ValueError:
            return resolved, True
        return resolved, False

    def _workspace_line_counter(path: Path) -> int | None:
        resolved, outside_workspace = _resolve_workspace_path(path)
        if resolved is None or outside_workspace:
            return None
        return count_text_lines(resolved)

    def _is_outside_workspace(path: Path) -> bool:
        _, outside_workspace = _resolve_workspace_path(path)
        return outside_workspace

    normalized = tool_name.rsplit("__", 1)[-1].lower()
    if normalized in {"read", "read_file", "readfile", "view"}:
        raw_path = tool_input.get("file_path", tool_input.get("path"))
        if not isinstance(raw_path, str) or not raw_path:
            return ContextSaverDecision(ContextSaverAction.UNKNOWN, "missing_read_path")
        try:
            path = Path(raw_path).expanduser()
        except (OSError, RuntimeError):
            return ContextSaverDecision(ContextSaverAction.UNKNOWN, "unreadable_or_binary")
        resolved = path if path.is_absolute() else workspace / path
        resolved_path, outside_workspace = _resolve_workspace_path(resolved)
        if outside_workspace:
            if not settings.enabled:
                return ContextSaverDecision(ContextSaverAction.ALLOW, "disabled", (raw_path,))
            if _is_explicit_bounded_read(
                tool_input.get("limit"),
                max_lines=settings.focused_read.min_lines,
            ):
                return ContextSaverDecision(
                    ContextSaverAction.ALLOW,
                    "explicit_bounded_range",
                    (raw_path,),
                )
            return ContextSaverDecision(
                ContextSaverAction.REDIRECT,
                "outside_workspace_broad_read",
                (raw_path,),
            )
        total = count_text_lines(resolved_path) if resolved_path is not None else None
        if total is None:
            return ContextSaverDecision(ContextSaverAction.UNKNOWN, "unreadable_or_binary")
        return classify_file_read(
            path=raw_path,
            offset=tool_input.get("offset"),
            limit=tool_input.get("limit"),
            total_lines=total,
            settings=settings,
        )
    if normalized in {
        "bash",
        "commandexecution",
        "exec_command",
        "execcommand",
        "shell",
        "shell_command",
        "terminal",
    }:
        command = tool_input.get("command", tool_input.get("cmd"))
        if not isinstance(command, str):
            return ContextSaverDecision(ContextSaverAction.UNKNOWN, "missing_shell_command")
        raw_workdir = tool_input.get("workdir", tool_input.get("cwd"))
        if raw_workdir is not None and (
            not isinstance(raw_workdir, str) or not raw_workdir.strip()
        ):
            return ContextSaverDecision(ContextSaverAction.UNKNOWN, "invalid_shell_workdir")
        try:
            initial_cwd = Path(raw_workdir).expanduser() if raw_workdir else None
        except (OSError, RuntimeError):
            return ContextSaverDecision(ContextSaverAction.UNKNOWN, "invalid_shell_workdir")
        return classify_shell_read(
            command,
            cwd=workspace,
            settings=settings,
            line_counter=_workspace_line_counter,
            initial_cwd=initial_cwd,
            is_outside_workspace=_is_outside_workspace,
        )
    return ContextSaverDecision(ContextSaverAction.ALLOW, "not_a_read_tool")


def redirect_message(decision: ContextSaverDecision) -> str:
    """Build the canonical model-facing redirect without source content."""
    paths = ", ".join(decision.paths) if decision.paths else "the requested file"
    if decision.reason == "outside_workspace_broad_read":
        return (
            f"Context Saver blocked a broad read of {paths} because it cannot inspect "
            "files outside the session workspace. Use a direct read with an explicit line range."
        )
    if decision.reason == "no_enabled_technique":
        return (
            f"Context Saver blocked a broad read of {paths}. Use a direct read with an "
            "explicit line range."
        )
    return (
        f"Context Saver blocked a broad read of {paths} before file content entered the "
        "model context. Call sys_context_read with these paths and a specific question, "
        "or use a direct read with an explicit line range when exact text is needed."
    )


def redirect_tool_result(decision: ContextSaverDecision) -> str:
    """Serialize a redirect as a stable tool result."""
    return json.dumps(
        {
            "context_saver": "redirect",
            "reason": decision.reason,
            "paths": list(decision.paths),
            "total_lines": decision.total_lines,
            "message": redirect_message(decision),
        }
    )


def native_redirect_hook_output(decision: ContextSaverDecision) -> dict[str, object]:
    """Return the shared Codex/Claude ``PreToolUse`` denial shape."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": redirect_message(decision),
        }
    }


def record_context_saver_event(
    decision: ContextSaverDecision,
    *,
    harness: str,
    tool_name: str,
    outcome: str,
    input_bytes: int | None = None,
    output_bytes: int | None = None,
    worker_input_tokens: int | None = None,
    worker_output_tokens: int | None = None,
    worker_route: str | None = None,
    worker_model_reported: str | None = None,
    latency_ms: int | None = None,
) -> None:
    """Emit content-free structured observability for one decision/result."""
    _logger.info(
        "context_saver.applied",
        extra={
            "context_saver_action": decision.action.value,
            "context_saver_reason": decision.reason,
            "context_saver_harness": harness,
            "context_saver_tool": tool_name,
            "context_saver_file_count": len(decision.paths),
            "context_saver_total_lines": decision.total_lines,
            "context_saver_input_bytes": input_bytes,
            "context_saver_output_bytes": output_bytes,
            "context_saver_tokens_avoided_estimate": (
                max(0, (input_bytes - output_bytes) // 4)
                if input_bytes is not None and output_bytes is not None
                else None
            ),
            "context_saver_worker_input_tokens": worker_input_tokens,
            "context_saver_worker_output_tokens": worker_output_tokens,
            "context_saver_worker_route": worker_route,
            "context_saver_worker_model_reported": worker_model_reported,
            "context_saver_latency_ms": latency_ms,
            "context_saver_outcome": outcome,
        },
    )
