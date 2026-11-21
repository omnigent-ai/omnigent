"""The pinned environment a code host instance's CLI runs in.

A run gets only an allowlist: ``PATH`` (pinned, never the host's), the variables an
instance names to pass through from the host, and fixed values from its config.
Nothing else in the host environment reaches the CLI, and token and diagnostic
variables never do: diagnostic output can print the credentials a request carries.
Refusals name the rule and the variable's position in the config, never its value.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

# The PATH a run gets unless the instance pins another in ``env_set``.
DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# Token variables the GitHub CLI reads ahead of its own sign-in; never forwarded.
TOKEN_VARIABLES = frozenset(
    {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}
)
# A name containing one of these is treated as holding a secret.
SECRET_NAME_PARTS = ("TOKEN", "SECRET", "PASSWORD", "KEY")
# Variables that make the CLI, git or curl print requests, which can carry credentials.
DIAGNOSTIC_VARIABLES = frozenset(
    {"GH_DEBUG", "DEBUG", "DEBUG_HTTP", "HTTP_DEBUG", "GIT_CURL_VERBOSE"}
)
# Every git trace setting, e.g. GIT_TRACE, GIT_TRACE_CURL or GIT_TRACE2_EVENT.
GIT_TRACE_PREFIX = "GIT_TRACE"
# GODEBUG is diagnostic only when it turns on Go's HTTP/2 request logging.
GO_HTTP2_DEBUG = "http2debug"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvironmentRuleError(ValueError):
    """An instance's environment config breaks a rule; the message names the rule."""


def _diagnostic_rule(name: str, value: str | None) -> str | None:
    """Return what makes *name*, set to *value*, a diagnostic variable, if anything.

    A ``None`` value means it isn't known yet, as for a name passed from the host.
    """
    upper = name.upper()
    if upper in DIAGNOSTIC_VARIABLES:
        return upper
    if upper.startswith(GIT_TRACE_PREFIX):
        return f"a {GIT_TRACE_PREFIX} setting"
    if upper == "GODEBUG" and value is not None and GO_HTTP2_DEBUG in value.lower():
        return f"GODEBUG with {GO_HTTP2_DEBUG}"
    return None


def _refuse_diagnostic(where: str, rule: str) -> EnvironmentRuleError:
    return EnvironmentRuleError(
        f"{where} is refused by the diagnostic-variable rule: {rule} turns on diagnostic "
        "output, which can print credentials"
    )


def _check_name(name: object, where: str) -> str:
    """Return *name* when a config may name it, else raise naming the rule it breaks."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise EnvironmentRuleError(f"{where} is refused: a variable name must be an identifier")
    upper = name.upper()
    if upper in TOKEN_VARIABLES:
        raise EnvironmentRuleError(
            f"{where} is refused by the token-variable rule: GitHub CLI tokens never reach "
            "an instance's CLI"
        )
    for part in SECRET_NAME_PARTS:
        if part in upper:
            raise EnvironmentRuleError(
                f"{where} is refused by the secret-name rule: names containing {part} are "
                "treated as secrets. Rename the variable if it doesn't hold one"
            )
    if (diagnostic := _diagnostic_rule(name, None)) is not None:
        raise _refuse_diagnostic(where, diagnostic)
    if upper.startswith(("GH_", "GITHUB_")):
        raise EnvironmentRuleError(
            f"{where} is refused by the CLI-settings rule: GH_ and GITHUB_ settings come "
            "from the instance, not the environment"
        )
    return name


def check_environment(
    passed: object, fixed: object, *, instance_id: str
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Return an instance's pass-through names and fixed values, once they keep the rules.

    :param passed: The config's ``env``: names copied from the host environment.
    :param fixed: The config's ``env_set``: names mapped to fixed, non-secret values.
    :param instance_id: Names the instance in refusals.
    :raises EnvironmentRuleError: Naming the rule and the entry's position.
    """
    if passed is None:
        passed = []
    if fixed is None:
        fixed = {}
    if not isinstance(passed, list):
        raise EnvironmentRuleError(f"instance {instance_id} env must be a list of names")
    if not isinstance(fixed, dict):
        raise EnvironmentRuleError(f"instance {instance_id} env_set must be a table")
    names: list[str] = []
    for index, name in enumerate(passed):
        where = f"instance {instance_id} env[{index}]"
        _check_name(name, where)
        if name == "PATH":
            raise EnvironmentRuleError(
                f"{where} is refused by the pinned-PATH rule: set PATH in env_set instead"
            )
        if name in names:
            raise EnvironmentRuleError(f"{where} is refused: it is listed twice")
        names.append(name)
    values: dict[str, str] = {}
    for position, (name, value) in enumerate(fixed.items()):
        where = f"instance {instance_id} env_set entry {position + 1}"
        _check_name(name, where)
        if name in names:
            raise EnvironmentRuleError(f"{where} is refused: it is also passed through in env")
        if not isinstance(value, str) or "\0" in value:
            raise EnvironmentRuleError(f"{where} is refused: its value must be a string")
        if (diagnostic := _diagnostic_rule(name, value)) is not None:
            raise _refuse_diagnostic(where, diagnostic)
        values[name] = value
    return tuple(names), values


def pinned_environment(
    passed: Sequence[str], fixed: Mapping[str, str], host_environment: Mapping[str, str]
) -> dict[str, str]:
    """Return the whole environment an instance's CLI runs with.

    :param passed: Names from :func:`check_environment`, copied when the host has them.
    :param fixed: Fixed values from :func:`check_environment`; ``PATH`` here replaces
        :data:`DEFAULT_PATH`.
    :param host_environment: The host process's environment.

    Token and diagnostic variables are dropped whatever is passed in, including a
    ``GODEBUG`` the host sets with ``http2debug``.
    """
    environment = {"PATH": DEFAULT_PATH}
    for name in passed:
        if name in host_environment:
            environment[name] = host_environment[name]
    environment.update(fixed)
    for name in [*environment]:
        if name in TOKEN_VARIABLES or _diagnostic_rule(name, environment[name]) is not None:
            del environment[name]
    return environment
