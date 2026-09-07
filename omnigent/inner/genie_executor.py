"""Executor for the ``genie`` harness — Databricks' fork of the codex CLI.

Genie (`databricks-eng/genie-code-codex`) is codex with Databricks branding, a
`~/.genie` home (``GENIE_HOME``), OpenAI phone-home disabled, and the Databricks
AI Gateway (Codex Responses API at ``/ai-gateway/codex/v1``) as its default
provider. Its ``app-server`` speaks the identical JSON-RPC protocol codex does,
so :class:`~omnigent.inner.codex_executor.CodexExecutor` already drives it: the
gateway base URL, ``databricks auth token`` auth command, model, and profile
are all shared behavior. Genie differs from codex in exactly two ways that this
subclass encodes:

1. **Binary** — resolve/run ``genie`` (``OMNIGENT_GENIE_PATH``) rather than
   ``codex``.
2. **Home** — ``GENIE_HOME`` / ``~/.genie`` rather than ``CODEX_HOME`` /
   ``~/.codex`` (the fork ignores ``CODEX_HOME``, so a per-session private home
   must be handed to it under the right env var).

Keeping this a thin subclass — rather than a fork of the executor — is
deliberate: genie's interaction surface is codex's today, and this is the seam
where genie-native behavior can grow (one method at a time) if that changes,
without touching the shared codex driver.
"""

from __future__ import annotations

from typing import Any

from omnigent._platform import resolve_cli_binary
from omnigent.inner.codex_executor import CodexExecutor

# Env override for an explicit genie binary, mirroring codex's
# ``OMNIGENT_CODEX_PATH``. Set this when genie lives on a PATH the host daemon
# doesn't inherit.
_GENIE_PATH_ENV = "OMNIGENT_GENIE_PATH"

GENIE_HOME_ENV_VAR = "GENIE_HOME"
GENIE_HOME_DIR_NAME = ".genie"


def _find_genie_cli() -> str | None:
    """Resolve the ``genie`` CLI binary (override → ``PATH`` → global dirs)."""
    return resolve_cli_binary("genie", env_var=_GENIE_PATH_ENV)


class GenieExecutor(CodexExecutor):
    """Drive the genie CLI's ``app-server`` over the shared codex protocol.

    Identical to :class:`CodexExecutor` except it runs ``genie`` and isolates
    per-session state under ``GENIE_HOME`` / ``~/.genie``. All gateway routing
    (base URL, ``databricks auth token`` auth command, model, profile) is
    inherited unchanged.
    """

    def __init__(self, **kwargs: Any) -> None:
        # Force the genie home spelling regardless of what a caller passed, so
        # the per-session private home is handed to genie under the env var it
        # actually reads.
        kwargs["home_env_var"] = GENIE_HOME_ENV_VAR
        kwargs["home_dir_name"] = GENIE_HOME_DIR_NAME
        super().__init__(**kwargs)

    def _find_cli(self) -> str | None:
        return _find_genie_cli()

    def _cli_missing_message(self) -> str:
        return (
            "GenieExecutor requires the 'genie' CLI on PATH. If genie is "
            "installed on a PATH the host daemon didn't inherit, set "
            f"{_GENIE_PATH_ENV}=/path/to/genie."
        )
