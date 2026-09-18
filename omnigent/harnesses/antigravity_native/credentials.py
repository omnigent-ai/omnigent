"""Resolve setup credentials for the native agy process."""

import configparser
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.onboarding.antigravity_auth import antigravity_api_key_ref
from omnigent.onboarding.gemini_gateway import (
    GEMINI_API_BASE_URL,
    GEMINI_BASE_URL_ENV,
    validate_gemini_base_url,
)
from omnigent.onboarding.provider_config import (
    DATABRICKS_KIND,
    GEMINI_FAMILY,
    KEY_KIND,
    default_provider_for_harness,
    load_config,
    resolve_secret,
)
from omnigent.util.env_credentials import getenv_nonempty_with_omnigent_prefix

if TYPE_CHECKING:
    from omnigent.inner.credential_proxy import DatabricksProfileTokenProvider


def databricks_token_source(profile: str) -> "DatabricksProfileTokenProvider":
    """Bind the workspace and refreshed identity to the selected profile."""
    from omnigent.harnesses.antigravity_native.databricks_auth import (
        ProfileAuthConfig,
        validate_workspace_host,
    )
    from omnigent.inner.credential_proxy import DatabricksProfileTokenProvider
    from omnigent.inner.databricks_executor import _read_databrickscfg_host

    if not profile.strip():
        raise OmnigentError("Enter a Databricks profile name.", code=ErrorCode.INVALID_INPUT)
    try:
        host = _read_databrickscfg_host(profile)
    except (configparser.Error, OSError, UnicodeError):
        # Parser errors can include entire lines containing credentials.
        raise OmnigentError(
            "Cannot read the Databricks profile file. Repair ~/.databrickscfg "
            "(or the file selected by DATABRICKS_CONFIG_FILE), then run omni setup again.",
            code=ErrorCode.INVALID_INPUT,
        ) from None
    if not host:
        raise OmnigentError(
            f"Databricks profile {profile!r} has no workspace host.", code=ErrorCode.INVALID_INPUT
        )
    host = validate_workspace_host(host)
    try:
        import databricks.sdk.config  # noqa: F401 - check the optional dependency at setup.
    except ImportError:
        raise OmnigentError(
            "Native agy Databricks routing requires `pip install 'omnigent[databricks]'`.",
            code=ErrorCode.INVALID_INPUT,
        ) from None
    return DatabricksProfileTokenProvider(
        profile, config_factory=lambda name: ProfileAuthConfig(name, host)
    )


@dataclass(frozen=True)
class AntigravityCredentials:
    """A credential and its endpoint resolved together for one launch."""

    api_key: str = field(repr=False)
    base_url: str
    model: str | None = None
    profile: str | None = None

    def environment(self) -> dict[str, str]:
        """Return the environment consumed by agy's Gemini API route."""
        environment = {"GEMINI_API_KEY": self.api_key, GEMINI_BASE_URL_ENV: self.base_url}
        if self.profile is not None:
            environment["OMNIGENT_AGY_DATABRICKS_PROFILE"] = self.profile
        return environment


def resolve_antigravity_credentials() -> AntigravityCredentials | None:
    """Resolve the Gemini default, legacy setup key, then ambient credentials.

    A configured but invalid provider raises instead of falling back to another
    endpoint or identity. None leaves agy's existing OAuth/ADC behavior intact.
    """
    config = load_config()
    provider = default_provider_for_harness(config, "antigravity-native")
    if provider is not None:
        if provider.kind == DATABRICKS_KIND and provider.native_gemini:
            source = databricks_token_source(provider.profile or "")
            return AntigravityCredentials(
                "omnigent-adapter-pending", source.workspace_url, profile=provider.profile
            )
        family = provider.family(GEMINI_FAMILY)
        key = (family.api_key or "").strip() if family is not None else ""
        if family is None or not key:
            raise OmnigentError(
                "The selected Gemini provider has no usable API key. Run omni setup.",
                code=ErrorCode.INVALID_INPUT,
            )
        base_url = family.base_url
        # Older key entries store Google's OpenAI model-listing URL.
        if provider.kind == KEY_KIND and base_url.rstrip("/") == (
            GEMINI_API_BASE_URL + "/v1beta/openai"
        ):
            base_url = GEMINI_API_BASE_URL
        return AntigravityCredentials(
            api_key=key,
            base_url=validate_gemini_base_url(base_url),
            model=family.default_model,
        )

    legacy_ref = antigravity_api_key_ref(config)
    if legacy_ref is not None:
        key = resolve_secret(legacy_ref).strip()
        base_url = GEMINI_API_BASE_URL
        # An adopted ambient key keeps its companion endpoint.
        if legacy_ref in {"env:GEMINI_API_KEY", "env:OMNIGENT_GEMINI_API_KEY"}:
            endpoint = getenv_nonempty_with_omnigent_prefix(GEMINI_BASE_URL_ENV)
            if endpoint is not None:
                base_url = validate_gemini_base_url(endpoint[1])
        if not key:
            raise OmnigentError(
                "The configured Gemini API key is empty.", code=ErrorCode.INVALID_INPUT
            )
        return AntigravityCredentials(key, base_url)

    key_env = getenv_nonempty_with_omnigent_prefix("GEMINI_API_KEY")
    if key_env is None:
        return None
    endpoint = getenv_nonempty_with_omnigent_prefix(GEMINI_BASE_URL_ENV)
    return AntigravityCredentials(
        key_env[1].strip(),
        validate_gemini_base_url(endpoint[1]) if endpoint is not None else GEMINI_API_BASE_URL,
    )


def antigravity_credentials_ready() -> bool:
    """Resolve configured credentials, otherwise use agy login detection."""
    from omnigent.onboarding.gemini_auth import gemini_login_detected

    try:
        return resolve_antigravity_credentials() is not None or gemini_login_detected()
    except (OmnigentError, OSError, ValueError):
        return False
