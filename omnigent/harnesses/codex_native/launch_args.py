"""Canonical Codex launch options and private config-file profile materialization."""

from __future__ import annotations

import copy
import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import tomlkit
from tomlkit.exceptions import TOMLKitError

_CODEX_CONFIG_PATHS = (
    "agents.*.config_file",
    "experimental_compact_prompt_file",
    "log_dir",
    "model_catalog_json",
    "model_instructions_file",
    "model_providers.*.auth.cwd",
    "sandbox_workspace_write.writable_roots.[]",
    "skills.config.[].path",
    "sqlite_home",
    "js_repl_node_path",
    "js_repl_node_module_dirs.[]",
    "profiles.*.experimental_compact_prompt_file",
    "profiles.*.model_catalog_json",
    "profiles.*.model_instructions_file",
    "profiles.*.js_repl_node_path",
    "profiles.*.js_repl_node_module_dirs.[]",
    *(
        f"otel.{exporter}.{transport}.tls.{field}"
        for exporter in ("exporter", "metrics_exporter", "trace_exporter")
        for transport in ("otlp-http", "otlp-grpc")
        for field in ("ca-certificate", "client-certificate", "client-private-key")
    ),
)

_MISSING = object()

# Config keys whose values are safe to log verbatim. Every other ``-c`` value is
# masked because launch args may carry credentials or private endpoints.
_LOGGABLE_CONFIG_KEYS = frozenset(
    {
        "approval_policy",
        "approvals_reviewer",
        "default_permissions",
        "model",
        "model_provider",
        "model_reasoning_effort",
        "profile",
        "sandbox_mode",
    }
)
_ENV_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
# An option with its value attached by ``=`` (e.g. ``--remote=ws://...``), so
# the value can be redacted without treating the whole token as opaque.
_ATTACHED_OPTION = re.compile(r"^(--?[A-Za-z0-9][A-Za-z0-9-]*)=(.*)$", re.DOTALL)


def absolute_codex_path(value: str, base: Path) -> str:
    """Match Codex's lexical path normalization without resolving symlinks."""
    home_relative = value == "~" or value.startswith(("~/", f"~{os.sep}"))
    expanded = os.path.expanduser(value) if home_relative else value
    return os.path.abspath(os.path.join(base, expanded))


def _resolve_profile_paths(config: dict[str, Any], source_home: Path) -> None:
    """Normalize Codex 0.155 typed path fields, not symbolic permission keys."""

    def resolve(value: Any, segments: list[str]) -> Any:
        if not segments:
            return absolute_codex_path(value, source_home) if isinstance(value, str) else value
        segment, *remaining = segments
        if segment == "[]" and isinstance(value, list):
            return [resolve(item, remaining) for item in value]
        if isinstance(value, dict):
            for key in list(value) if segment == "*" else (segment,):
                if key in value:
                    value[key] = resolve(value[key], remaining)
        return value

    for path in _CODEX_CONFIG_PATHS:
        resolve(config, path.split("."))


def canonical_codex_launch_args(args: Sequence[str]) -> list[str]:
    """Expand aliases and attached short values without interpreting option values."""
    value_flags = {
        "-c",
        "--config",
        "-s",
        "--sandbox",
        "-a",
        "--ask-for-approval",
        "-p",
        "--profile",
        "--add-dir",
        "-m",
        "--model",
        "-C",
        "--cd",
        "-i",
        "--image",
        "--local-provider",
        "--enable",
        "--disable",
    }
    canonical: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            canonical.extend(args[index:])
            break
        if arg == "--yolo":
            canonical.append("--dangerously-bypass-approvals-and-sandbox")
        elif arg == "--not-so-yolo":
            canonical.append("--approve-for-me")
        elif arg in value_flags:
            canonical.append(arg)
            if index + 1 < len(args):
                index += 1
                canonical.append(args[index])
        elif (
            arg.startswith(("-c", "-s", "-a", "-p", "-C"))
            and len(arg) > 2
            and not arg.startswith(("-c=", "-s=", "-a="))
        ):
            canonical.extend((arg[:2], arg[2:].removeprefix("=")))
        elif arg.startswith(("--profile=", "--add-dir=", "--cd=")):
            canonical.extend(arg.split("=", 1))
        else:
            canonical.append(arg)
        index += 1
    return canonical


def codex_config_profile(args: Sequence[str]) -> str | None:
    """Read the file-profile selector, rejecting ambiguous or missing selectors."""
    canonical = canonical_codex_launch_args(args)
    profile: str | None = None
    index = 0
    while index < len(canonical):
        arg = canonical[index]
        if arg == "--":
            break
        if arg in {"--profile", "-p"}:
            if profile is not None or index + 1 == len(canonical):
                raise ValueError("Codex requires exactly one value for --profile")
            profile = canonical[index + 1]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", profile):
                raise ValueError("Invalid Codex config profile name")
        if arg in {
            "-c",
            "--config",
            "-s",
            "--sandbox",
            "-a",
            "--ask-for-approval",
            "-p",
            "--profile",
            "--add-dir",
            "-m",
            "--model",
            "-C",
            "--cd",
            "-i",
            "--image",
            "--local-provider",
            "--enable",
            "--disable",
        }:
            index += 1
        index += 1
    return profile


def without_codex_config_profile(args: Sequence[str]) -> list[str]:
    canonical = canonical_codex_launch_args(args)
    profile = codex_config_profile(canonical)
    if profile is None:
        return canonical
    for index, arg in enumerate(canonical):
        if arg in {"--profile", "-p"} and canonical[index + 1] == profile:
            return [*canonical[:index], *canonical[index + 2 :]]
    return canonical


def _merge_tables(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_tables(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


def _carry_config_edits(
    base: dict[str, Any], previous: dict[str, Any], current: dict[str, Any]
) -> None:
    for key in previous.keys() | current.keys():
        if key not in current:
            base.pop(key, None)
        elif key not in previous or current[key] != previous[key]:
            if all(isinstance(table.get(key), dict) for table in (previous, current)):
                if not isinstance(base.get(key), dict):
                    base[key] = {}
                _carry_config_edits(base[key], previous[key], current[key])
            else:
                base[key] = copy.deepcopy(current[key])


def _write_private_config(path: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _profile_base(state_path: Path, current: dict[str, Any]) -> dict[str, Any]:
    try:
        state = tomlkit.parse(state_path.read_text()).unwrap()
    except TOMLKitError as error:
        raise ValueError(f"Invalid Codex profile state: {state_path}") from error
    if not all(isinstance(state.get(key), dict) for key in ("base", "applied")):
        raise ValueError(f"Invalid Codex profile state: {state_path}")
    for key in ("base", "applied", "pending"):
        value = state.get(key)
        if isinstance(value, dict):
            value.pop("developer_instructions", None)
    base = state["base"]
    if "pending" in state:
        if not isinstance(state["pending"], dict):
            raise ValueError(f"Invalid Codex profile state: {state_path}")
        if current not in (state["applied"], state["pending"]):
            raise ValueError(
                f"Codex config changed during an incomplete profile update: {state_path}"
            )
    else:
        _carry_config_edits(base, state["applied"], current)
    return base


def validate_codex_config_profile_state(codex_home: Path) -> None:
    """Validate an existing profile journal without modifying private config."""
    state_path = codex_home / ".omnigent-config-profile.toml"
    if not state_path.exists():
        return
    config_path = codex_home / "config.toml"
    current = tomlkit.parse(config_path.read_text()).unwrap() if config_path.exists() else {}
    current.pop("developer_instructions", None)
    _profile_base(state_path, current)


def materialize_codex_config_profile(
    codex_home: Path,
    source_home: Path,
    profile: str | None,
    *,
    codex_version: tuple[int, int, int] | None,
) -> None:
    """Fold the selected user profile into the private user layer, below project/CLI.

    Codex app-server has no file-profile selector. Preserve the base separately
    so switching/removing a profile on restart is reversible. Private TUI edits
    survive profile removal; selected profiles still take precedence over them.
    Journal atomic file replacements so interrupted updates can be retried.
    Never modify the source home.
    """
    state_path = codex_home / ".omnigent-config-profile.toml"
    if profile is None and not state_path.exists():
        return
    if codex_home.resolve() == source_home.resolve():
        raise ValueError("Codex profiles require a private CODEX_HOME")
    config_path = codex_home / "config.toml"
    current = (
        tomlkit.parse(config_path.read_text()) if config_path.exists() else tomlkit.document()
    )
    current_config = current.unwrap()
    current_instructions = current_config.pop("developer_instructions", _MISSING)
    base = _profile_base(state_path, current_config) if state_path.exists() else current_config
    merged = copy.deepcopy(base)
    if profile is not None:
        if codex_config_profile(["--profile", profile]) != profile:
            raise ValueError("Invalid Codex config profile name")
        if codex_version is None or codex_version >= (0, 134, 0):
            overlay = tomlkit.parse((source_home / f"{profile}.config.toml").read_text()).unwrap()
        else:
            overlay = base.get("profiles", {}).get(profile)
            if not isinstance(overlay, dict):
                raise ValueError(f"Codex config profile {profile!r} does not exist")
        overlay = copy.deepcopy(overlay)
        profile_instructions = overlay.pop("developer_instructions", _MISSING)
        _resolve_profile_paths(overlay, source_home)
        _merge_tables(merged, overlay)
        if overlay.get("sandbox_mode") is not None and overlay.get("default_permissions") is None:
            merged.pop("default_permissions", None)
    else:
        profile_instructions = _MISSING
    rendered_config = copy.deepcopy(merged)
    effective_instructions = (
        profile_instructions if profile_instructions is not _MISSING else current_instructions
    )
    if effective_instructions is not _MISSING:
        rendered_config["developer_instructions"] = effective_instructions
    rendered = tomlkit.dumps(rendered_config)
    pending_state = tomlkit.dumps({"base": base, "applied": current_config, "pending": merged})
    final_state = tomlkit.dumps({"base": base, "applied": merged})
    _write_private_config(state_path, pending_state)
    _write_private_config(config_path, rendered)
    _write_private_config(state_path, final_state)


def redact_codex_launch_args(args: Sequence[str]) -> list[str]:
    """
    Return the resolved Codex argv with secret-bearing values masked.

    Flag names, subcommands, paths and thread ids stay intact so a log row
    shows exactly which launch was attempted. Masked: the value of every
    ``-c``/``--config`` override outside :data:`_LOGGABLE_CONFIG_KEYS`, every
    ``NAME=value`` environment assignment (an ``env`` wrapper's config args),
    and the userinfo and query of any URL, whether it is a bare argument or
    attached to an option by ``=`` (e.g. ``--remote=ws://user:pw@host?sig=x``).
    Redaction never raises: a malformed URL is masked whole rather than
    aborting the launch it is meant to record.

    :param args: Resolved argv after the host's config args and Omnigent's
        remote args are merged, e.g. ``["OPENAI_API_KEY=sk-x", "codex", "-c",
        'model="gpt-5.4"', "resume", "--remote", "ws://127.0.0.1:1", "t1"]``.
    :returns: The argv with masked values, e.g. ``["OPENAI_API_KEY=***",
        "codex", "-c", 'model="gpt-5.4"', "resume", "--remote",
        "ws://127.0.0.1:1", "t1"]``.
    """
    redacted: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"-c", "--config"} and index + 1 < len(args):
            redacted.extend((arg, _redact_config_override(args[index + 1])))
            index += 2
            continue
        if arg.startswith(("-c=", "--config=")):
            flag, _, override = arg.partition("=")
            redacted.append(f"{flag}={_redact_config_override(override)}")
        elif arg.startswith("-c") and len(arg) > 2 and not arg.startswith("--"):
            redacted.append(f"-c{_redact_config_override(arg[2:])}")
        elif _URL_SCHEME.match(arg):
            redacted.append(_redact_url(arg))
        elif (attached := _ATTACHED_OPTION.match(arg)) and _URL_SCHEME.match(attached.group(2)):
            redacted.append(f"{attached.group(1)}={_redact_url(attached.group(2))}")
        elif not arg.startswith("-") and (match := _ENV_ASSIGNMENT.match(arg)):
            redacted.append(f"{match.group(1)}=***")
        else:
            redacted.append(arg)
        index += 1
    return redacted


def _redact_config_override(override: str) -> str:
    key, separator, _ = override.partition("=")
    if not separator or key.strip() in _LOGGABLE_CONFIG_KEYS:
        return override
    return f"{key}=***"


def _redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        # urlsplit rejects some inputs (e.g. an unterminated IPv6 literal like
        # ``ws://[broken``). Mask the whole value so logging never aborts the
        # launch it records.
        return "***"
    if not parts.scheme:
        return "***"
    return urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))
