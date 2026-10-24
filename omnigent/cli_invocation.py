"""How to spell an ``omnigent`` command back to the user.

When a deployment wraps the CLI (e.g. ``isaac omni``) it sets
``OMNIGENT_WRAPPER_COMMAND`` and refuses naked ``omnigent`` calls (see the
wrapper guard in :mod:`omnigent.cli`). Followup hints that tell the user to run
a command must then name the wrapper (``isaac omni stop``) rather than the
naked binary (``omnigent stop``), or they suggest exactly the command the guard
rejects. :func:`cli_invocation` centralizes that spelling: hints interpolate it
in place of the leading ``omnigent``/``omni`` token so every one honors the
configured wrapper. :func:`quote_hint_argument` quotes an argument of such a
hint so it pastes into the user's shell unchanged.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping

from omnigent._platform import IS_WINDOWS

DEFAULT_CLI_NAME = "omnigent"
WRAPPER_COMMAND_ENV = "OMNIGENT_WRAPPER_COMMAND"


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


def quote_hint_argument(argument: str) -> str:
    """Quote *argument* for a command hint the user will paste into a shell.

    POSIX shells glob ``?`` and ``*`` (zsh refuses an unmatched pattern with
    ``no matches found``), so an argument such as ``https://<ws>/omnigent?o=1``
    goes through :func:`shlex.quote`; arguments without shell metacharacters
    stay bare. cmd.exe and PowerShell pass those characters through verbatim,
    and cmd.exe would hand the single quotes to the CLI, so on Windows the
    argument is kept as is.
    """
    return argument if IS_WINDOWS else shlex.quote(argument)
