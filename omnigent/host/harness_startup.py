"""Read-only host launch settings for the selected user-owned host."""

from __future__ import annotations

import getopt
import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnigent._platform import resolve_cli_binary
from omnigent.config import load_global_config
from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_startup_config import resolve_harness_config

SUPPORTED_HARNESSES = {"claude-native", "codex-native"}


# ``env -S`` expands only the braced form.
_ENV_VARIABLE_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# ``env -S`` word separators; other whitespace, such as U+00A0, stays in a word.
_ENV_SPLIT_SEPARATORS = " \t\n"
# Newer ``env`` releases also split on these; older ones keep them in a word.
_ENV_UNMODELED_SEPARATORS = "\v\f\r"


class HarnessEnvironment(BaseModel):
    model_config = ConfigDict(strict=True)

    inherit: bool
    variables: dict[str, str]
    unset: list[str]


class HarnessStartup(BaseModel):
    """Launch metadata returned through the owner-only host API."""

    model_config = ConfigDict(strict=True)

    command: str
    resolved_path: str | None
    command_source: Literal["env", "config", "default"]
    arg_count: int = Field(ge=0)
    # None identifies an older host that reports only arg_count.
    args: list[str] | None = None
    configured_command: str | None = None
    configured_args: list[str] | None = None
    environment: HarnessEnvironment | None = None


def describe_harness_startup(harness: str) -> HarnessStartup:
    """Describe host defaults for web launches; workspace overrides may differ."""
    from omnigent.host.connect import _build_runner_env

    harness = canonicalize_harness(harness) or harness
    if harness not in SUPPORTED_HARNESSES:
        raise ValueError("launch settings are not reported for this harness")
    _, overrides = resolve_harness_config(load_global_config())
    entry = overrides.get(harness, {})
    env = _build_runner_env(
        os.environ, server_url="", runner_id="", binding_token="", workspace="", parent_pid=0
    )
    name = harness.removesuffix("-native")
    path = env.get("PATH", os.defpath)
    override = env.get(f"OMNIGENT_{name.upper()}_PATH", "").strip()
    command = entry.get("command", name)
    source: Literal["env", "config", "default"] = "config" if "command" in entry else "default"
    # Claude uses env before config; Codex uses config before a resolvable env override.
    if override and (
        harness == "claude-native"
        or (
            "command" not in entry
            and (
                shutil.which(override, path=path)
                or (os.path.isfile(override) and os.access(override, os.X_OK))
            )
        )
    ):
        command, source = override, "env"
    args = entry.get("args", [])
    environment = HarnessEnvironment(inherit=True, variables={}, unset=[])
    unwrapped = _unwrap_env(command, args, path)
    if unwrapped is not None:
        command, args, path, environment = unwrapped
        # env searches only its effective PATH, never our extra install directories.
        resolved = shutil.which(command, path=path)
    else:
        if Path(command).name == "env":
            environment = None
        resolved = resolve_cli_binary(command, which=lambda cmd: shutil.which(cmd, path=path))
    return HarnessStartup(
        command=command,
        resolved_path=resolved,
        command_source=source,
        arg_count=len(args),
        args=args,
        configured_command=entry.get("command"),
        configured_args=entry.get("args", []),
        environment=environment,
    )


def env_wrapper_environment(
    command: str, args: list[str], environ: Mapping[str, str] | None = None
) -> HarnessEnvironment | None:
    """
    Return the environment changes an ``env`` wrapper launch applies.

    :param command: Configured harness command, e.g. ``"env"`` or ``"claude"``.
    :param args: Arguments passed to *command*, e.g.
        ``["CLAUDE_CONFIG_DIR=/srv/claude", "claude"]``.
    :param environ: Environment the wrapper starts from, used to expand
        ``${NAME}`` in ``-S`` strings; ``None`` treats such strings as unparsed.
    :returns: The wrapper's ``-i``/``-``/``-u``/``-S``/assignment changes, or
        ``None`` when *command* is not a parseable ``env`` wrapper (e.g. it
        uses ``-v``) or runs another ``env``. A ``-C``/``--chdir`` directory
        is reported by :func:`env_wrapper_chdir`.
    """
    parsed = _parse_env_wrapper(command, args, environ)
    return parsed[0] if parsed is not None else None


def env_wrapper_chdir(
    command: str, args: list[str], environ: Mapping[str, str] | None = None
) -> str | None:
    """
    Return the directory an ``env -C``/``--chdir`` wrapper changes into.

    :param command: Configured harness command, e.g. ``"env"``.
    :param args: Arguments passed to *command*, e.g.
        ``["--chdir=/srv/repo", "claude"]``.
    :param environ: Environment the wrapper starts from; see
        :func:`env_wrapper_environment`.
    :returns: The last ``-C``/``--chdir`` value, e.g. ``"/srv/repo"``, or
        ``None`` when there is none or the wrapper is not parseable.
    """
    parsed = _parse_env_wrapper(command, args, environ)
    return parsed[1] if parsed is not None else None


def _parse_env_wrapper(
    command: str, args: list[str], environ: Mapping[str, str] | None
) -> tuple[HarnessEnvironment, str | None] | None:
    """
    Parse one ``env`` wrapper layer.

    :param command: Configured harness command, e.g. ``"env"``.
    :param args: Arguments passed to *command*.
    :param environ: Environment the wrapper starts from, or ``None``.
    :returns: The wrapper's environment changes and its last ``-C``
        directory (or ``None``), or ``None`` when *command* is not a
        parseable ``env`` wrapper or its command is another ``env``, whose
        changes are not modeled.
    """
    chdirs: list[str] = []
    try:
        normalized = _normalize_env_wrapper_args(args, chdirs, environ)
    except ValueError:
        return None
    unwrapped = _unwrap_env(command, normalized, os.defpath)
    if unwrapped is None or Path(unwrapped[0]).name == "env":
        return None
    return unwrapped[3], (chdirs[-1] if chdirs else None)


def _normalize_env_wrapper_args(
    args: list[str],
    chdirs: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """
    Rewrite env's legacy ``-``, bundled short options, and ``-S`` split strings.

    Split-string words are scanned again, so options inside them apply; see
    :func:`_split_env_string` for the modeled split syntax. ``-C``/``--chdir``
    options are removed and their directories recorded.

    :param args: ``env`` arguments, e.g. ``["-iS", "A=1 claude"]``.
    :param chdirs: Receives each ``-C``/``--chdir`` directory in order, or
        ``None`` to discard them.
    :param environ: Environment ``-S`` strings expand from, or ``None``.
    :returns: Equivalent arguments, e.g. ``["-i", "A=1", "claude"]``.
    :raises ValueError: If a split string has unbalanced quotes or uses
        syntax that POSIX quoting would read differently from ``env``, or an
        option is missing its value.
    """
    pending = list(args)
    normalized: list[str] = []
    recorded = chdirs if chdirs is not None else []
    while pending:
        arg = pending.pop(0)
        if arg == "-":
            # env's legacy spelling of --ignore-environment also ends its options.
            normalized.extend(["-i", "--", *pending])
            break
        if arg == "--chdir":
            if not pending:
                raise ValueError("env --chdir needs a value")
            recorded.append(pending.pop(0))
        elif arg.startswith("--chdir="):
            recorded.append(arg.partition("=")[2])
        elif arg == "--split-string":
            if not pending:
                raise ValueError("env --split-string needs a value")
            pending[:0] = _split_env_string(pending.pop(0), environ)
        elif arg.startswith("--split-string="):
            pending[:0] = _split_env_string(arg.partition("=")[2], environ)
        elif arg == "--unset" and pending:
            normalized.extend([arg, pending.pop(0)])
        elif arg.startswith("--") and arg != "--":
            normalized.append(arg)
        elif arg.startswith("-") and arg != "--":
            _normalize_short_option_cluster(arg, pending, normalized, recorded, environ)
        else:
            normalized.append(arg)
            normalized.extend(pending)
            break
    return normalized


def _normalize_short_option_cluster(
    arg: str,
    pending: list[str],
    normalized: list[str],
    chdirs: list[str],
    environ: Mapping[str, str] | None,
) -> None:
    """
    Expand one short-option cluster such as ``-iS`` or ``-uNAME``.

    :param arg: The cluster, e.g. ``"-iS"``.
    :param pending: Remaining arguments; ``-u``/``-S``/``-C`` values are taken
        from here, and split-string words are pushed back onto it.
    :param normalized: Output arguments, extended in place.
    :param chdirs: Receives a ``-C`` directory.
    :param environ: Environment ``-S`` strings expand from, or ``None``.
    :returns: None.
    :raises ValueError: If ``-u``, ``-S``, or ``-C`` is missing its value.
    """
    cluster = arg[1:]
    for position, flag in enumerate(cluster):
        if flag == "i":
            normalized.append("-i")
            continue
        if flag in ("u", "S", "C"):
            value = cluster[position + 1 :] or (pending.pop(0) if pending else None)
            if value is None:
                raise ValueError(f"env -{flag} needs a value")
            if flag == "u":
                normalized.extend(["-u", value])
            elif flag == "C":
                chdirs.append(value)
            else:
                pending[:0] = _split_env_string(value, environ)
            return
        # Leave unmodeled flags for the parser to reject.
        normalized.append(f"-{cluster[position:]}")
        return


def _split_env_string(value: str, environ: Mapping[str, str] | None) -> list[str]:
    """
    Split an ``env -S`` string the way GNU ``env`` does.

    Words split on unquoted spaces, tabs, and newlines; single quotes keep
    text literal, and ``${NAME}`` expands from *environ* outside single quotes.
    An unset name adds nothing, so a word made only of unset names is dropped,
    while a set but empty name still yields a word. Backslash escapes, ``#``
    comments, bare ``$NAME``, and ``\\v``/``\\f``/``\\r`` have ``env``-specific
    or version-dependent meanings this does not model.

    :param value: Split string, e.g. ``"CLAUDE_CONFIG_DIR=${HOME}/c claude"``.
    :param environ: Environment ``env`` expands from, or ``None`` when unknown.
    :returns: The arguments, e.g. ``["CLAUDE_CONFIG_DIR=/home/user/c", "claude"]``.
    :raises ValueError: If *value* uses an unmodeled form, unbalanced quotes,
        or an expansion without a known *environ*.
    """
    if any(char in value for char in "\\" + _ENV_UNMODELED_SEPARATORS):
        raise ValueError(f"unsupported env -S syntax: {value!r}")
    words: list[str] = []
    current: list[str] = []
    in_word = False
    quote: str | None = None
    index = 0
    while index < len(value):
        char = value[index]
        if quote is None and char in _ENV_SPLIT_SEPARATORS:
            if in_word:
                words.append("".join(current))
                current, in_word = [], False
            index += 1
            continue
        if quote is None and not in_word and char == "#":
            raise ValueError(f"unsupported env -S comment: {value!r}")
        if quote is None and char in "'\"":
            quote, in_word = char, True
        elif char == quote:
            quote = None
        elif char == "$" and quote != "'":
            match = _ENV_VARIABLE_REFERENCE.match(value, index)
            if match is None or environ is None:
                raise ValueError(f"unsupported env -S expansion: {value!r}")
            if match.group(1) in environ:
                current.append(environ[match.group(1)])
                in_word = True
            index = match.end()
            continue
        else:
            current.append(char)
            in_word = True
        index += 1
    if quote is not None:
        raise ValueError(f"unbalanced quote in env -S string: {value!r}")
    if in_word:
        words.append("".join(current))
    return words


def _unwrap_env(
    command: str, args: list[str], path: str
) -> tuple[str, list[str], str, HarnessEnvironment] | None:
    """Separate env assignments and options from the wrapped command."""
    if Path(command).name != "env":
        return None
    try:
        options, remaining = getopt.getopt(args, "iu:", ["ignore-environment", "unset="])
    except getopt.GetoptError:
        return None
    if remaining[:1] == ["-"]:
        return None  # env's legacy -i spelling; keep this wrapper opaque.
    environment = HarnessEnvironment(inherit=True, variables={}, unset=[])
    for option, value in options:
        if option in ("-i", "--ignore-environment"):
            environment.inherit = False
        else:
            environment.unset.append(value)
        if option in ("-i", "--ignore-environment") or value == "PATH":
            path = os.defpath
    for index, arg in enumerate(remaining):
        key, separator, value = arg.partition("=")
        if not separator:
            return arg, remaining[index + 1 :], path, environment
        if not key:
            return None
        environment.variables[key] = value
        if key == "PATH":
            path = value
    return None
