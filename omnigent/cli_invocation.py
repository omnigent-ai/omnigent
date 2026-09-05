"""How to spell an ``omnigent`` command back to the user.

When a deployment wraps the CLI (e.g. ``isaac omni``) it sets
``OMNIGENT_WRAPPER_COMMAND`` and refuses naked ``omnigent`` calls (see the
wrapper guard in :mod:`omnigent.cli`). Followup hints that tell the user to run
a command must then name the wrapper (``isaac omni stop``) rather than the
naked binary (``omnigent stop``), or they suggest exactly the command the guard
rejects. :func:`cli_invocation` centralizes that spelling: hints interpolate it
in place of the leading ``omnigent``/``omni`` token so every one honors the
configured wrapper.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

DEFAULT_CLI_NAME = "omnigent"
WRAPPER_COMMAND_ENV = "OMNIGENT_WRAPPER_COMMAND"
REQUIRE_WRAPPER_ENV = "OMNIGENT_REQUIRE_WRAPPER"
WRAPPER_BYPASS_ENV = "OMNIGENT_WRAPPER_BYPASS"


def wrapper_required(env: Mapping[str, str]) -> bool:
    """Whether the operator's wrapper gate currently refuses a naked invocation."""
    from omnigent.process_logging import env_truthy

    return env_truthy(env.get(REQUIRE_WRAPPER_ENV)) and not env_truthy(env.get(WRAPPER_BYPASS_ENV))


def cli_invocation(*, name: str = DEFAULT_CLI_NAME, env: Mapping[str, str] | None = None) -> str:
    """Return the token that invokes the CLI: the configured wrapper, else ``name``.

    ``name`` is the naked binary the hint would otherwise use (``omnigent`` or
    its ``omni`` alias); it is kept verbatim when no wrapper is configured, so a
    hint reads ``omnigent stop`` normally and ``isaac omni stop`` when
    ``OMNIGENT_WRAPPER_COMMAND=isaac omni``.
    """
    if env is None:
        env = os.environ
    return (env.get(WRAPPER_COMMAND_ENV) or "").strip() or name
