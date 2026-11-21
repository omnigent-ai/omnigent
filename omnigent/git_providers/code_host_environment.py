"""The pinned environment a code host instance's CLI runs in.

A run gets only an allowlist: ``PATH`` (pinned, never the host's), the variables an
instance names to pass through from the host, and fixed values from its config.
Nothing else in the host environment reaches the CLI, and token variables never do.
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
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvironmentRuleError(ValueError):
    """An instance's environment config breaks a rule; the message names the rule."""


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
    """
    environment = {"PATH": DEFAULT_PATH}
    for name in passed:
        if name in host_environment:
            environment[name] = host_environment[name]
    environment.update(fixed)
    for name in TOKEN_VARIABLES:
        environment.pop(name, None)
    return environment
