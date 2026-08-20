"""
Interactive provider and model selection for ``omnigent create``.

Prompts the user to pick a provider, supply credentials, and
select a chat-capable model. Uses Rich for polished terminal output.
"""

from __future__ import annotations

from dataclasses import dataclass

import click
from rich.console import Console

from omnigent.env_credentials import getenv_nonempty_with_omnigent_prefix
from omnigent.onboarding.providers import (
    PROVIDER_ENV_VARS,
    AuthField,
    get_provider_config,
)

console = Console()


@dataclass
class ProviderSelection:
    """
    Result of the provider/model selection flow.

    :param provider: Provider name, e.g. ``"anthropic"``.
    :param model: Full litellm model string, e.g.
        ``"anthropic/claude-sonnet-4-20250514"``.
    :param credentials: Credential key-value pairs collected from
        the user, e.g. ``{"api_key": "sk-ant-..."}``.
    """

    provider: str
    model: str
    credentials: dict[str, str]


def resolve_provider_from_model(model_string: str) -> ProviderSelection:
    """
    Build a :class:`ProviderSelection` from a ``--model`` flag value.

    Parses the litellm ``provider/model_name`` format and reads
    credentials from environment variables.

    :param model_string: Model in litellm format, e.g.
        ``"anthropic/claude-sonnet-4-20250514"``.
    :returns: A :class:`ProviderSelection` with credentials from env.
    :raises click.ClickException: If the model string is malformed or
        the required env var is not set.
    """
    if "/" not in model_string:
        raise click.ClickException(
            f"Model must be in provider/model_name format, got: {model_string!r}"
        )

    provider, _ = model_string.split("/", 1)
    credentials = _read_credentials_from_env(provider)
    return ProviderSelection(
        provider=provider,
        model=model_string,
        credentials=credentials,
    )


# ---------------------------------------------------------------------------
# Non-interactive credential resolution
# ---------------------------------------------------------------------------


def _read_credentials_from_env(provider: str) -> dict[str, str]:
    """
    Read credentials from environment variables for non-interactive mode.

    For the ``openai`` provider, also picks up ``OPENAI_BASE_URL``
    when set (same convention the OpenAI SDK reads, same behavior
    as the interactive wizard) so onboarding can target an
    OpenAI-compatible gateway like Databricks serving-endpoints.

    :param provider: Provider name, e.g. ``"anthropic"``.
    :returns: Dict with credential fields from env vars.
    :raises click.ClickException: If required env vars are missing.
    """
    env_var = PROVIDER_ENV_VARS.get(provider)
    if env_var:
        resolved = getenv_nonempty_with_omnigent_prefix(env_var)
        if resolved is not None:
            _actual_env_var, value = resolved
            creds: dict[str, str] = {"api_key": value}
            if provider == "openai":
                base_url = getenv_nonempty_with_omnigent_prefix("OPENAI_BASE_URL")
                if base_url is not None:
                    creds["base_url"] = base_url[1]
            return creds
        raise click.ClickException(
            f"Non-interactive mode requires {env_var} or "
            f"OMNIGENT_{env_var} for provider {provider!r}."
        )

    # Complex providers — check default auth mode fields.
    config = get_provider_config(provider)
    default_mode = next(
        (m for m in config.auth_modes if m.mode_id == config.default_mode),
        config.auth_modes[0],
    )
    return _collect_env_credentials(provider, default_mode.fields)


def _collect_env_credentials(
    provider: str,
    fields: list[AuthField],
) -> dict[str, str]:
    """
    Collect required credential values from environment variables.

    :param provider: Provider name for error messages.
    :param fields: Auth fields to check.
    :returns: Dict of field name to env var value.
    :raises click.ClickException: If any required field is missing.
    """
    credentials: dict[str, str] = {}
    missing: list[str] = []
    for field in fields:
        if not field.required:
            continue
        env_name = field.name.upper()
        resolved = getenv_nonempty_with_omnigent_prefix(env_name)
        if resolved is not None:
            credentials[field.name] = resolved[1]
        else:
            missing.append(env_name)

    if missing:
        raise click.ClickException(
            f"Non-interactive mode requires these env vars for provider "
            f"{provider!r}: {', '.join(missing)}"
        )
    return credentials
