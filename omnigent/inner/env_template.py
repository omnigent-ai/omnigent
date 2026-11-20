"""Launch-time env templates (``os_env.sandbox.env_template``).

A reference resolves only from Omnigent's context variables or from a name the
launch path's own env filter already passes; any other reference drops the entry.
"""

from __future__ import annotations

import logging
import re
import string
from collections.abc import Callable, Mapping
from pathlib import Path

from omnigent.runner.identity import RUNNER_AUTH_SECRET_ENV_VARS, RUNNER_WORKSPACE_ENV_VAR

_logger = logging.getLogger(__name__)

ENV_TEMPLATE_FIELD = "os_env.sandbox.env_template"

SESSION_WORKSPACE_TEMPLATE_VAR = "OMNIGENT_SESSION_WORKSPACE"
RUNNER_WORKSPACE_TEMPLATE_VAR = RUNNER_WORKSPACE_ENV_VAR
# Names the runner resolved for a harness child, so the child's CLI filter admits them.
RESOLVED_TEMPLATE_NAMES_ENV_VAR = "OMNIGENT_ENV_TEMPLATE_NAMES"

_RESERVED_TARGET_PREFIXES: tuple[str, ...] = ("OMNIGENT_", "HARNESS_")
_RESERVED_TARGET_NAMES: frozenset[str] = frozenset({"PATH", "HOME"}) | RUNNER_AUTH_SECRET_ENV_VARS

_POSIX_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Helpers re-resolve on every request, so each misconfiguration warns once per process.
_reported_drops: set[tuple[str, tuple[str, ...]]] = set()


def validate_env_template(raw: object) -> dict[str, str] | None:
    """
    Validate the ``env_template:`` mapping from an agent spec.

    :param raw: Raw YAML value, e.g.
        ``{"EDATA_OUTPUT_DIR": "${OMNIGENT_SESSION_WORKSPACE}/data"}``, or ``None``.
    :returns: A copy of the mapping, or ``None`` when *raw* is ``None``.
    :raises ValueError: On a non-mapping, a bad or reserved key, a non-string
        value, or malformed ``$`` syntax.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{ENV_TEMPLATE_FIELD} must be a mapping, got {type(raw).__name__}")
    validated: dict[str, str] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not _POSIX_ENV_NAME_RE.fullmatch(name):
            raise ValueError(
                f"{ENV_TEMPLATE_FIELD} keys must be POSIX environment variable names "
                f"(letters/digits/underscore, not starting with a digit): {name!r}"
            )
        if name in _RESERVED_TARGET_NAMES or name.startswith(_RESERVED_TARGET_PREFIXES):
            raise ValueError(f"{ENV_TEMPLATE_FIELD} cannot define reserved variable {name!r}")
        if not isinstance(value, str):
            raise ValueError(
                f"{ENV_TEMPLATE_FIELD}.{name} must be a string, got {type(value).__name__}"
            )
        if not string.Template(value).is_valid():
            raise ValueError(
                f"{ENV_TEMPLATE_FIELD}.{name} has malformed '$' syntax: use ${{NAME}}, "
                f"$NAME, or $$ for a literal dollar sign"
            )
        validated[name] = value
    return validated


def workspace_template_context(
    *,
    session_workspace: str | Path | None,
    runner_workspace: str | Path | None,
) -> dict[str, str]:
    """
    Build the Omnigent-provided context variables.

    :param session_workspace: The session's workspace; ``None`` falls back to
        *runner_workspace*.
    :param runner_workspace: The runner's launch directory, or ``None``.
    :returns: Only the variables that have values, e.g.
        ``{"OMNIGENT_RUNNER_WORKSPACE": "/repo", "OMNIGENT_SESSION_WORKSPACE": "/repo"}``.
    """
    runner = str(runner_workspace) if runner_workspace else ""
    session = str(session_workspace) if session_workspace else runner
    context: dict[str, str] = {}
    if runner:
        context[RUNNER_WORKSPACE_TEMPLATE_VAR] = runner
    if session:
        context[SESSION_WORKSPACE_TEMPLATE_VAR] = session
    return context


def resolve_env_template(
    template: Mapping[str, str] | None,
    *,
    source_env: Mapping[str, str],
    is_allowed: Callable[[str], bool],
    context: Mapping[str, str],
) -> dict[str, str]:
    """
    Resolve an env template against values the launch path may already see.

    :param template: Validated template, e.g. ``{"CACHE_DIR": "${HOME}/.cache"}``.
    :param source_env: Environment references are read from.
    :param is_allowed: The launch path's own name filter.
    :param context: Values that always resolve, from :func:`workspace_template_context`.
    :returns: Resolved variables; entries with a disallowed, unset, or empty
        reference are omitted and logged by name only.
    """
    if not template:
        return {}
    resolved: dict[str, str] = {}
    for name, raw_value in template.items():
        value_template = string.Template(raw_value)
        values: dict[str, str] = {}
        missing: list[str] = []
        for reference in value_template.get_identifiers():
            if context.get(reference):
                values[reference] = context[reference]
            elif (
                reference not in RUNNER_AUTH_SECRET_ENV_VARS
                and is_allowed(reference)
                and source_env.get(reference)
            ):
                values[reference] = source_env[reference]
            else:
                missing.append(reference)
        if missing:
            _report_drop(name, missing)
            continue
        resolved[name] = value_template.substitute(values)
    return resolved


def _report_drop(name: str, missing: list[str]) -> None:
    key = (name, tuple(sorted(missing)))
    level = logging.DEBUG if key in _reported_drops else logging.WARNING
    _reported_drops.add(key)
    _logger.log(
        level,
        "Dropping %s.%s: references %s are unset or not visible to the sandbox",
        ENV_TEMPLATE_FIELD,
        name,
        ", ".join(key[1]),
    )


def declared_env_template(os_env: object | None) -> dict[str, str] | None:
    """Return ``os_env.sandbox.env_template`` from a duck-typed ``OSEnvSpec``, or ``None``."""
    sandbox = getattr(os_env, "sandbox", None) if os_env is not None else None
    template = getattr(sandbox, "env_template", None) if sandbox is not None else None
    return dict(template) if template else None


def resolved_template_names(source: Mapping[str, str]) -> frozenset[str]:
    """Names the runner resolved for this process, from ``OMNIGENT_ENV_TEMPLATE_NAMES``."""
    names = source.get(RESOLVED_TEMPLATE_NAMES_ENV_VAR, "").split(",")
    return frozenset(name for name in names if name)
