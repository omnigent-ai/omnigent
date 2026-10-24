"""Executor for the ``genie`` harness — Databricks' fork of the codex CLI.

Genie (`databricks-eng/genie-code-codex`) is codex with Databricks branding, a
`~/.genie` home (``GENIE_HOME``), OpenAI phone-home disabled, and the Databricks
AI Gateway (Codex Responses API at ``/ai-gateway/codex/v1``) as its default
provider. Its ``app-server`` speaks the identical JSON-RPC protocol codex does,
so :class:`~omnigent.inner.codex_executor.CodexExecutor` already drives it: the
gateway base URL, ``databricks auth token`` auth command, model, and profile
are all shared behavior. Genie differs from codex in four ways that this
subclass encodes:

1. **Binary** — resolve/run ``genie`` (``OMNIGENT_GENIE_PATH``) rather than
   ``codex``.
2. **Home** — ``GENIE_HOME`` / ``~/.genie`` rather than ``CODEX_HOME`` /
   ``~/.codex`` (the fork ignores ``CODEX_HOME``, so a per-session private home
   must be handed to it under the right env var).
3. **No ``-c``** — genie's CLI allowlist denies ``-c``/``--config`` (and every
   ``app-server`` flag), so config overrides are written into the private
   home's ``config.toml`` and ``genie app-server`` runs with no flags.
4. **Model ids** — genie only honors models in its bundled catalog, keyed by
   slug (``gpt-5.4-mini``), so Databricks endpoint names
   (``databricks-gpt-5-4-mini``) are mapped with :func:`genie_model_id`.

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


def genie_model_id(model: str) -> str:
    """Map a Databricks serving-endpoint name to genie's catalog slug.

    Port of genie's ``databricks_codex_model_id``: ``"databricks-gpt-5-4-mini"``
    becomes ``"gpt-5.4-mini"``. Genie silently replaces a gateway model missing
    from its bundled catalog with the provider default, so the endpoint name
    Omnigent's catalog uses would launch a different model. Ids that are not
    ``[databricks-]gpt-<n>...`` endpoint names pass through unchanged. Genie
    special-cases two legacy endpoints that are absent from its catalog either
    way; they are not special-cased here.

    :param model: Model id, e.g. ``"databricks-gpt-5-4-mini"`` or ``"gpt-5.4-mini"``.
    :returns: The genie catalog slug, e.g. ``"gpt-5.4-mini"``.
    """
    version = model.removeprefix("databricks-")
    if not version.startswith("gpt-"):
        return model
    segments = version.removeprefix("gpt-").split("-")
    if not segments[0].isdigit():
        return model
    slug = f"gpt-{segments[0]}"
    rest = segments[1:]
    for _ in range(2):
        if rest and rest[0].isdigit():
            slug += f".{rest.pop(0)}"
    if rest:
        slug += "-" + "-".join(rest)
    return slug


class GenieExecutor(CodexExecutor):
    """Drive the genie CLI's ``app-server`` over the shared codex protocol.

    Identical to :class:`CodexExecutor` except it runs ``genie``, isolates
    per-session state under ``GENIE_HOME`` / ``~/.genie``, and puts config
    overrides in that home's ``config.toml`` instead of ``-c``. All gateway routing
    (base URL, ``databricks auth token`` auth command, model, profile) is
    inherited unchanged.
    """

    def __init__(self, **kwargs: Any) -> None:
        # Force the genie home spelling regardless of what a caller passed, so
        # the per-session private home is handed to genie under the env var it
        # actually reads.
        kwargs["home_env_var"] = GENIE_HOME_ENV_VAR
        kwargs["home_dir_name"] = GENIE_HOME_DIR_NAME
        kwargs["config_overrides_in_file"] = True
        super().__init__(**kwargs)

    def _find_cli(self) -> str | None:
        return _find_genie_cli()

    def _cli_model_id(self, model: str) -> str:
        return genie_model_id(model)

    def _cli_missing_message(self) -> str:
        return (
            "GenieExecutor requires the 'genie' CLI on PATH. If genie is "
            "installed on a PATH the host daemon didn't inherit, set "
            f"{_GENIE_PATH_ENV}=/path/to/genie."
        )
