"""Per-project setup commands shared by the server and the host.

A project's ``config`` may carry two command lists that prepare its sessions:

- ``env_commands`` run in the user's shell on every runner launch; the
  environment changes they make (``conda activate``, ``nvm use``) are applied
  to the agent.
- ``setup_commands`` run once per git worktree (copying ``.env``, installs).
  The agent's turns wait for them unless ``parallel_setup`` is set.

The server validates them when a project is saved and sends them to the host
with each runner launch; the host executes them (see
:mod:`omnigent.host.project_setup_runner`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

ENV_COMMANDS_KEY = "env_commands"
SETUP_COMMANDS_KEY = "setup_commands"
PARALLEL_SETUP_KEY = "parallel_setup"

#: Set on a runner whose environment carries a project's ``env_commands``
#: changes, so harnesses keep tool shells from rebuilding ``PATH``.
PROJECT_ENV_APPLIED_ENV_VAR = "OMNIGENT_PROJECT_ENV_APPLIED"

#: Upper bounds that keep a project config and its launch frame small.
MAX_COMMANDS_PER_LIST = 20
MAX_COMMAND_CHARS = 4096
#: Total seconds a host spends on one worktree's ``setup_commands``.
SETUP_BUDGET_S = 600.0


def _validated_commands(value: object, key: str) -> tuple[str, ...]:
    """
    Validate one command list from a project config or launch frame.

    :param value: Raw value, e.g. ``["conda activate proj"]``. ``None`` means unset.
    :param key: Config key for error messages, e.g. ``"env_commands"``.
    :returns: The commands with surrounding whitespace stripped.
    :raises ValueError: If the value is not a list of bounded, non-blank strings.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list of commands")
    if len(value) > MAX_COMMANDS_PER_LIST:
        raise ValueError(f"{key} allows at most {MAX_COMMANDS_PER_LIST} commands")
    commands: list[str] = []
    for command in value:
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"{key} entries must be non-empty strings")
        if len(command) > MAX_COMMAND_CHARS:
            raise ValueError(f"{key} entries must be at most {MAX_COMMAND_CHARS} characters")
        commands.append(command.strip())
    return tuple(commands)


def validate_project_setup_config(config: Mapping[str, Any]) -> None:
    """
    Reject a project config whose setup command lists are malformed.

    Other config keys stay opaque to the server.

    :param config: Project config object, e.g. ``{"env_commands": ["conda activate x"]}``.
    :raises ValueError: If ``env_commands`` or ``setup_commands`` is malformed.
    """
    _validated_commands(config.get(ENV_COMMANDS_KEY), ENV_COMMANDS_KEY)
    _validated_commands(config.get(SETUP_COMMANDS_KEY), SETUP_COMMANDS_KEY)
    _validated_flag(config.get(PARALLEL_SETUP_KEY), PARALLEL_SETUP_KEY)


def _validated_flag(value: object, key: str) -> bool:
    """
    Validate an optional boolean setting.

    :raises ValueError: If the value is set and not a boolean.
    """
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false")
    return value


def commands_digest(commands: Sequence[str]) -> str:
    """
    Stable fingerprint of a command list, recorded with a worktree's setup run.

    :param commands: Commands in run order, e.g. ``("npm ci",)``.
    :returns: Hex SHA-256 digest.
    """
    digest = hashlib.sha256()
    for command in commands:
        digest.update(command.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class ProjectSetup:
    """
    The setup commands a project applies to its sessions.

    :param env_commands: Commands whose environment changes reach the agent on
        every launch, e.g. ``("conda activate proj",)``.
    :param setup_commands: One-shot commands run once per git worktree, e.g.
        ``("npm ci",)``.
    :param parallel_setup: Let the agent's turns start while ``setup_commands``
        still run, instead of waiting for them.
    """

    env_commands: tuple[str, ...] = ()
    setup_commands: tuple[str, ...] = ()
    parallel_setup: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> ProjectSetup | None:
        """
        Build from a project config or launch-frame payload.

        :param raw: Object carrying ``env_commands`` / ``setup_commands``.
        :returns: The setup, or ``None`` when both lists are empty.
        :raises ValueError: If either list is malformed.
        """
        setup = cls(
            env_commands=_validated_commands(raw.get(ENV_COMMANDS_KEY), ENV_COMMANDS_KEY),
            setup_commands=_validated_commands(raw.get(SETUP_COMMANDS_KEY), SETUP_COMMANDS_KEY),
            parallel_setup=_validated_flag(raw.get(PARALLEL_SETUP_KEY), PARALLEL_SETUP_KEY),
        )
        return setup if setup.env_commands or setup.setup_commands else None

    def to_wire(self) -> dict[str, Any]:
        """
        Serialize for the ``host.launch_runner`` frame.

        :returns: ``{"env_commands": [...], "setup_commands": [...], "parallel_setup": bool}``.
        """
        return {
            ENV_COMMANDS_KEY: list(self.env_commands),
            SETUP_COMMANDS_KEY: list(self.setup_commands),
            PARALLEL_SETUP_KEY: self.parallel_setup,
        }
