"""DatabricksExecutor: real LLM execution via the Databricks FM API.

Uses the OpenAI-compatible chat completions API served by Databricks
Model Serving.  Works with any model hosted on the serving endpoint
(Claude, Llama, DBRX, etc.).

Environment:
    DATABRICKS_CONFIG_PROFILE – optional Databricks profile selector
    ~/.databrickscfg          – host + token profile for Databricks access
    (or)
    OPENAI_API_KEY + OPENAI_BASE_URL – direct override for any OpenAI-compatible
                                        endpoint (useful for local dev / testing)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Generator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeAlias

import httpx

if TYPE_CHECKING:
    from openai import OpenAI, Stream
    from openai.types.chat import ChatCompletionChunk

from .async_utils import run_sync_on_thread
from .executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    TextChunk,
    ToolCallRequest,
    ToolSpec,
    TurnComplete,
    iterate_blocking_stream,
)

logger = logging.getLogger(__name__)


# OpenAI chat.completions.create(**kwargs) builds up a heterogeneous
# request body dynamically from cfg.extra; the SDK accepts many typed
# parameters but the splat site needs an open dict.
OpenAIKwargs: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

_API_CALL_TIMEOUT_SECONDS = 30.0
_STREAM_IDLE_TIMEOUT_SECONDS = 60.0

_SESSION_ONLY_EXECUTOR_EXTRA_KEYS = {
    "new_user_messages_flushed",
    "stepwise_internal_turns",
}


@dataclass
class _DatabricksSessionState:
    active_stream: Stream[ChatCompletionChunk] | None = None
    interrupt_requested: bool = False


@dataclass(frozen=True)
class DatabricksCredentials:
    """Resolved Databricks workspace credentials.

    ``host`` is the workspace URL; ``token`` is a bearer usable as
    ``Authorization: Bearer <token>``. Callers that receive an instance
    can rely on both fields being non-empty — the credential readers
    return ``None`` when nothing is configured rather than a sentinel
    object with empty strings.
    """

    host: str
    token: str


def _read_databrickscfg(profile: str | None = None) -> DatabricksCredentials | None:
    """
    Resolve Databricks ``(host, bearer_token)`` for *profile* using the
    databricks-sdk's unified credential resolver.

    The SDK supports every ``auth_type`` shipped in ``~/.databrickscfg``
    (``pat``, ``databricks-cli`` / OAuth-U2M, service-principal OAuth,
    Azure CLI, env-OIDC, metadata-service, etc.) and always returns a
    freshly-minted bearer — critically, for OAuth profiles it mints a
    new access token rather than returning the stale ``token`` field
    that the CLI may have left behind. Prior to this fix, the function
    read the ``token`` field directly, which silently broke
    ``auth_type: databricks-cli`` profiles (every Databricks-backed
    harness returned HTTP 403).

    :param profile: Databricks config profile name. When ``None``, the SDK
        itself honors the standard resolution order
        (``DATABRICKS_CONFIG_PROFILE`` env var, then the ``DEFAULT``
        section). Example values: ``None``, ``"DEFAULT"``, ``"<your-profile>"``.
        When a named profile is given but cannot be found on the current
        machine (e.g. the spec was authored locally with ``profile: myprofile``
        but now runs on a Databricks App container with no
        ``~/.databrickscfg``), the function falls back to the ambient
        credential chain (``DATABRICKS_HOST`` + ``DATABRICKS_TOKEN`` env
        vars, metadata-service OIDC, etc.) before giving up.
    :returns: :class:`DatabricksCredentials` with ``host`` (workspace URL,
        e.g. ``"https://example.databricks.com"``) and
        ``token`` (bearer usable as ``Authorization: Bearer <token>``),
        or ``None`` when no credentials are configured.

    :raises: Never. SDK-level failures are swallowed and fall back to
        the legacy file-reading path (``_read_databrickscfg_file_fallback``)
        so exotic setups that predate the SDK's support matrix continue
        to work for plain-PAT configurations.

    .. note::

       Executors that need per-request token refresh should use
       :func:`_resolve_databricks_auth` instead, which returns an
       httpx Auth callback backed by ``Config.authenticate()``.
       This function still returns a static snapshot and is used
       by non-executor callers that only need a one-shot credential
       check.
    """
    try:
        from databricks.sdk.config import Config
    except ImportError:
        # databricks-sdk should always be present (pinned in pyproject.toml),
        # but if it isn't we gracefully degrade to file reading.
        return _read_databrickscfg_file_fallback(profile)

    # ``None`` means "let the SDK decide" (env var / DEFAULT section).
    sdk_profile = profile or os.environ.get("DATABRICKS_CONFIG_PROFILE")
    try:
        cfg = Config(profile=sdk_profile)
        headers = cfg.authenticate()
    except ValueError as profile_exc:
        # ValueError is what Config raises for every user-facing resolution
        # failure (missing profile, malformed file, no credentials in env,
        # unknown auth_type, etc.). Anything else (e.g. network errors
        # fetching OAuth tokens) should propagate.
        logger.debug(
            "databricks-sdk credential resolution failed for profile %r: %s",
            sdk_profile,
            profile_exc,
        )
        if sdk_profile is not None:
            # Profile not found; fall back to ambient credentials
            # (env vars, OIDC) so the spec works on App servers too.
            logger.debug(
                "profile %r not found; trying ambient Databricks credentials", sdk_profile
            )
            try:
                cfg = Config()
                headers = cfg.authenticate()
            except ValueError:
                return _read_databrickscfg_file_fallback(profile)
        else:
            return _read_databrickscfg_file_fallback(profile)

    host = cfg.host
    auth = headers.get("Authorization")
    if not host or not auth or not auth.startswith("Bearer "):
        # Non-Bearer auth schemes (e.g. Basic) or missing host/auth are
        # treated as unresolved. None of our harnesses support non-Bearer
        # today.
        return None
    return DatabricksCredentials(host=host, token=auth.removeprefix("Bearer "))


def _read_databrickscfg_file_fallback(profile: str | None = None) -> DatabricksCredentials | None:
    """
    Legacy fallback: read ``host`` and ``token`` directly from
    ``~/.databrickscfg``.

    Only used when the databricks-sdk cannot initialize a ``Config``
    for the requested profile (see :func:`_read_databrickscfg`). This
    preserves backward compatibility for plain-PAT setups whose
    config files the SDK rejects for unrelated reasons.

    Profile resolution order:
      1. Explicit *profile* argument
      2. ``DATABRICKS_CONFIG_PROFILE`` env var
      3. ``DEFAULT`` section (if it has both host and token)
      4. First section that has both host and token

    :param profile: Databricks config profile name, or ``None`` for the
        default resolution order. Example: ``"DEFAULT"``.
    :returns: :class:`DatabricksCredentials` with both fields populated,
        or ``None`` when the file is absent or no matching section has
        both fields.
    """
    import configparser
    from pathlib import Path

    cfg_path = Path(os.environ.get("DATABRICKS_CONFIG_FILE") or (Path.home() / ".databrickscfg"))
    if not cfg_path.exists():
        return None

    config = configparser.ConfigParser()
    config.read(cfg_path)

    resolved_profile = profile or os.environ.get("DATABRICKS_CONFIG_PROFILE")
    if resolved_profile and resolved_profile in config:
        host = config[resolved_profile].get("host")
        token = config[resolved_profile].get("token")
        if host and token:
            return DatabricksCredentials(host=host, token=token)

    # Try DEFAULT section
    default = config.defaults()
    default_host = default.get("host")
    default_token = default.get("token")
    if default_host and default_token:
        return DatabricksCredentials(host=default_host, token=default_token)

    # Try first section with both host and token
    for section in config.sections():
        host = config[section].get("host")
        token = config[section].get("token")
        if host and token:
            logger.info("Using Databricks profile [%s] from ~/.databrickscfg", section)
            return DatabricksCredentials(host=host, token=token)

    return None


def _read_databrickscfg_host(profile: str | None = None) -> str | None:
    """
    Read only the workspace host from the Databricks config file.

    Codex gateway launches use the cfg profile as a host selector and delegate
    bearer refresh to ``databricks auth token --profile ...`` via Codex's
    ``auth.command``. That startup path must work even when ``databricks-sdk``
    is unavailable in the runner environment, because raw
    ``auth_type=databricks-cli`` sections often have a host but no static
    token.

    Profile resolution order is intentionally narrower than
    :func:`_read_databrickscfg_file_fallback`: an explicit missing profile
    returns ``None`` instead of falling through to another profile, because
    the generated auth command will still be pinned to the explicit profile.

    :param profile: Databricks config profile name, or ``None`` to use
        ``DATABRICKS_CONFIG_PROFILE``, ``[DEFAULT]``, then the first section
        with a host.
    :returns: Workspace host URL, or ``None`` when no host can be resolved.
    """
    import configparser
    from pathlib import Path

    cfg_path = Path(os.environ.get("DATABRICKS_CONFIG_FILE") or (Path.home() / ".databrickscfg"))
    if not cfg_path.exists():
        return None

    config = configparser.ConfigParser()
    config.read(cfg_path)

    resolved_profile = profile or os.environ.get("DATABRICKS_CONFIG_PROFILE")
    if resolved_profile:
        if resolved_profile in config:
            host = config[resolved_profile].get("host")
            return host or None
        return None

    default_host = config.defaults().get("host")
    if default_host:
        return default_host

    for section in config.sections():
        host = config[section].get("host")
        if host:
            logger.info("Using Databricks host from profile [%s] in ~/.databrickscfg", section)
            return host

    return None


class DatabricksAuthError(OSError):
    """Raised when Databricks credential resolution or token refresh fails.

    Carries an actionable message pointing the user to
    ``databricks auth login``.
    """


class _DatabricksBearerAuth(httpx.Auth):
    """httpx Auth that calls ``Config.authenticate()`` on every HTTP request.

    Unlike the snapshot approach (read a token once, set ``api_key``),
    this delegates token lifecycle to the Databricks SDK. OAuth tokens
    are refreshed transparently via the SDK's refresh-token exchange,
    so sessions that run longer than the 1-hour access-token lifetime
    survive without manual intervention.

    Inspired by ``databricks-ai-bridge``'s ``BearerAuth``.

    :param config: A ``databricks.sdk.config.Config`` instance whose
        ``authenticate()`` method returns fresh ``Authorization``
        headers on every call.
    :param profile_name: Human-readable profile name for error
        messages, e.g. ``"dev"``.
    """

    def __init__(
        self,
        config: Any,  # type: ignore[explicit-any]
        profile_name: str | None = None,
        failure_message: str | None = None,
    ) -> None:
        """
        :param config: Databricks SDK ``Config`` instance.
        :param profile_name: Profile name shown in error messages.
        :param failure_message: Full replacement error message for
            auth failures, e.g. ``"Databricks authentication failed
            for workspace https://example.databricks.com. Run: ..."``.
            ``None`` builds the default profile-flavored message.
        """
        self._config = config
        self._profile_name = profile_name
        self._failure_message = failure_message

    def _authenticate_headers(self) -> dict[str, str]:
        """
        Return fresh ``Authorization`` headers from the reused Config.

        Reusing the wrapped ``Config`` is what makes this cheap on repeat
        calls: the SDK serves the cached OAuth token from memory and only
        re-runs the Databricks CLI when the token nears expiry.

        :returns: Header dict from ``Config.authenticate()``, e.g.
            ``{"Authorization": "Bearer dapi..."}``.
        :raises DatabricksAuthError: When the SDK cannot mint a token
            (expired refresh token, revoked credentials, etc.).
        """
        try:
            return self._config.authenticate()
        except Exception as exc:
            if self._failure_message is not None:
                raise DatabricksAuthError(self._failure_message) from exc
            profile_flag = f" -p {self._profile_name}" if self._profile_name else ""
            raise DatabricksAuthError(
                f"Databricks authentication failed for profile {self._profile_name!r}. "
                f"Run: databricks auth login{profile_flag}"
            ) from exc

    def current_token(self) -> str | None:
        """
        Return the current bearer token, minting/refreshing via the SDK.

        Backed by :meth:`_authenticate_headers`, so callers that invoke
        this once per HTTP request pay the ~0.5s Databricks CLI shell-out
        only on the first call (and on token refresh), not every call.

        :returns: The bearer token string (no ``"Bearer "`` prefix), or
            ``None`` when the SDK returns a non-Bearer / empty
            ``Authorization`` header.
        :raises DatabricksAuthError: When the SDK cannot mint a token.
        """
        auth_value = self._authenticate_headers().get("Authorization", "")
        if auth_value.startswith("Bearer "):
            return auth_value.removeprefix("Bearer ")
        return None

    def auth_flow(
        self,
        request: httpx.Request,
    ) -> Generator[httpx.Request, httpx.Response, None]:
        """Inject a fresh ``Authorization: Bearer`` header.

        :param request: The outgoing httpx request.
        :yields: The request with the auth header set.
        :raises DatabricksAuthError: When the SDK cannot mint a
            token (expired refresh token, revoked credentials, etc.).
        """
        auth_value = self._authenticate_headers().get("Authorization", "")
        if auth_value:
            request.headers["Authorization"] = auth_value
        yield request


def _resolve_databricks_auth(
    profile: str | None = None,
    *,
    host: str | None = None,
) -> tuple[_DatabricksBearerAuth, str]:
    """Resolve Databricks credentials and return per-request auth + host.

    Validates that authentication succeeds at call time. On success,
    returns an httpx Auth that re-authenticates on every HTTP request
    (surviving OAuth access-token expiry transparently) and the
    workspace host URL.

    :param profile: Databricks config profile name, e.g. ``"dev"``.
        ``None`` uses the SDK's default resolution order. Mutually
        exclusive with ``host``.
    :param host: Workspace host to authenticate against, e.g.
        ``"https://example.databricks.com"``. Used by the
        ``omnigent login <apps-url>`` pointer records, which name a
        workspace rather than a profile; resolution is delegated to
        :func:`_resolve_databricks_auth_for_host`. The ambient
        profile/env fallback is NOT attempted in this mode — the
        record asked for a specific workspace, so a credential miss
        fails loud.
    :returns: ``(auth, host)`` — an httpx Auth for injection into
        ``httpx.Client``/``httpx.AsyncClient`` and the workspace URL,
        e.g. ``"https://example.cloud.databricks.com"``.
    :raises DatabricksAuthError: When credentials are missing or
        authentication fails.
    :raises ImportError: When the ``databricks-sdk`` package is not
        installed.
    :raises ValueError: When both ``profile`` and ``host`` are given.
    """
    try:
        from databricks.sdk.config import Config
    except ImportError as exc:
        raise ImportError(
            "The 'databricks-sdk' package is required for Databricks authentication. "
            "Install it with: pip install databricks-sdk"
        ) from exc

    if host is not None:
        if profile is not None:
            raise ValueError("_resolve_databricks_auth takes profile or host, not both")
        return _resolve_databricks_auth_for_host(host)

    sdk_profile = profile or os.environ.get("DATABRICKS_CONFIG_PROFILE")
    cfg = None

    try:
        cfg = Config(profile=sdk_profile)
        cfg.authenticate()
    except ValueError:
        if profile is None and sdk_profile is not None:
            # Profile name came from the DATABRICKS_CONFIG_PROFILE env var,
            # not from an explicit profile argument.  Fall back to the
            # ambient credential chain (OIDC, metadata service, etc.) so CI
            # environments that inject tokens via env vars but have no
            # ~/.databrickscfg still work.  Log a warning so the fallback is
            # visible rather than silent.
            #
            # When the profile was explicit (profile is not None), we do NOT
            # fall back — the user asked for a specific workspace and silently
            # using a different one violates the "Fail loud" principle.
            logger.warning(
                "Databricks profile %r (from DATABRICKS_CONFIG_PROFILE) not found "
                "in config file; falling back to ambient credential chain.",
                sdk_profile,
            )
            try:
                cfg = Config()
                cfg.authenticate()
            except ValueError:
                cfg = None
        else:
            cfg = None
    except Exception as exc:
        profile_flag = f" -p {profile}" if profile else ""
        raise DatabricksAuthError(
            f"Databricks authentication failed for profile {profile!r}. "
            f"Run: databricks auth login{profile_flag}"
        ) from exc

    if cfg is not None and cfg.host:
        return _DatabricksBearerAuth(cfg, profile_name=profile), cfg.host

    # SDK-based resolution failed (simple PAT profile, missing auth_type,
    # etc.). Fall back to reading ~/.databrickscfg directly — static PATs
    # don't need per-request refresh.
    creds = _read_databrickscfg(profile)
    if creds is not None:
        static_cfg = type(
            "_StaticAuth",
            (),
            {
                "authenticate": lambda _self: {"Authorization": f"Bearer {creds.token}"},
            },
        )()
        return _DatabricksBearerAuth(static_cfg, profile_name=profile), creds.host

    profile_flag = f" -p {profile}" if profile else ""
    raise DatabricksAuthError(
        f"Databricks profile {profile!r} is not authenticated. "
        f"Run: databricks auth login{profile_flag}"
    )


def _sdk_config(**kwargs: str) -> Any:  # type: ignore[explicit-any]  # SDK Config, imported lazily
    """Construct a databricks-sdk ``Config`` (test indirection point).

    The SDK probes host metadata at construction time, which makes
    offline unit tests against placeholder hosts impossible — tests
    patch this helper with a stub instead of touching the SDK module.

    :param kwargs: ``Config`` keyword arguments, e.g.
        ``profile="my-ws"`` or ``host=..., auth_type="databricks-cli"``.
    :returns: The constructed ``databricks.sdk.config.Config``.
    """
    from databricks.sdk.config import Config

    # The SDK types ``Config.__init__`` as taking a CredentialsStrategy
    # positionally; keyword config attributes are dynamically declared,
    # so the kwargs expansion is untypeable here.
    return Config(**kwargs)  # type: ignore[arg-type]


def _resolve_databricks_auth_for_host(host: str) -> tuple[_DatabricksBearerAuth, str]:
    """Resolve per-request auth for a specific workspace host.

    Prefers a ``~/.databrickscfg`` profile pinned to *host*:
    ``databricks auth login --host <host>`` saves one, and the CLI's
    host-keyed token lookup (``databricks auth token --host``) is
    unreliable across CLI versions — it can miss a grant the login
    cached under the profile name, and some builds return a cached
    grant for a *different* workspace. The profile path goes through
    the SDK's full credential chain (OAuth refresh, PAT, …) for
    exactly the requested host. The host-keyed ``databricks-cli``
    lookup remains as the last resort for cfg-less setups.

    :param host: Workspace host, e.g.
        ``"https://example.databricks.com"``.
    :returns: ``(auth, host)`` — an httpx Auth and the workspace URL.
    :raises DatabricksAuthError: When no credential source resolves
        for the host.
    """
    host_failure = (
        f"Databricks authentication failed for workspace {host}. "
        f"Run: databricks auth login --host {host}"
    )
    for profile_name in _databrickscfg_profiles_for_host(host):
        try:
            cfg = _sdk_config(profile=profile_name)
            cfg.authenticate()
        except Exception:  # noqa: BLE001 — try the next matching profile
            logger.debug("profile %r matched host %s but did not authenticate", profile_name, host)
            continue
        return _DatabricksBearerAuth(cfg, profile_name=profile_name), cfg.host or host
    try:
        host_cfg = _sdk_config(host=host, auth_type="databricks-cli")
        host_cfg.authenticate()
    except Exception as exc:
        raise DatabricksAuthError(host_failure) from exc
    return _DatabricksBearerAuth(host_cfg, failure_message=host_failure), host


def _databrickscfg_profiles_for_host(host: str) -> list[str]:
    """List ``~/.databrickscfg`` profile names whose ``host`` is *host*.

    Comparison is scheme-insensitive and ignores trailing slashes, so
    ``my-ws.cloud.databricks.com`` in the cfg matches a
    ``https://my-ws.cloud.databricks.com`` query.

    :param host: Workspace host to match, e.g.
        ``"https://example.databricks.com"``.
    :returns: Matching section names in file order (the ``DEFAULT``
        section included when it carries a matching host), or ``[]``
        when the config file is missing or unparseable.
    """
    import configparser
    from pathlib import Path

    def _norm(value: str) -> str:
        value = value.strip().rstrip("/")
        return value.split("://", 1)[-1].lower()

    cfg_path = Path(os.environ.get("DATABRICKS_CONFIG_FILE") or (Path.home() / ".databrickscfg"))
    if not cfg_path.exists():
        return []
    config = configparser.ConfigParser()
    try:
        config.read(cfg_path)
    except configparser.Error:
        return []
    wanted = _norm(host)
    matches = [
        section
        for section in config.sections()
        if _norm(config[section].get("host", "")) == wanted
    ]
    default_host = config.defaults().get("host")
    if default_host and _norm(default_host) == wanted:
        matches.append("DEFAULT")
    return matches


def _get_openai_client(profile: str | None = None) -> OpenAI:
    """Lazily import and construct the OpenAI client.

    Supports three configuration modes (in priority order):
      1. Direct OpenAI-compatible: OPENAI_BASE_URL + OPENAI_API_KEY
      2. Direct OpenAI default endpoint: OPENAI_API_KEY
      3. Databricks config file: ~/.databrickscfg
    """
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ImportError(
            "The 'openai' package is required for DatabricksExecutor. "
            "Install it with: pip install openai"
        ) from exc

    # Direct OpenAI-compatible config takes precedence
    if os.environ.get("OPENAI_BASE_URL"):
        from .open_responses_sdk import _OPENAI_KEY_PLACEHOLDER

        return OpenAI(
            base_url=os.environ["OPENAI_BASE_URL"],
            # See _OPENAI_KEY_PLACEHOLDER docstring in open_responses_sdk.
            api_key=os.environ.get("OPENAI_API_KEY", _OPENAI_KEY_PLACEHOLDER),
        )

    # Plain OpenAI key against the default api.openai.com endpoint. Use it
    # directly rather than falling through to Databricks auth, which would
    # otherwise raise a misleading "install databricks-sdk" error for a user
    # who only configured OPENAI_API_KEY. Mirrors the sibling builder in
    # open_responses_sdk._get_openai_client.
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        return OpenAI(api_key=api_key)

    from .open_responses_sdk import _OPENAI_KEY_PLACEHOLDER as _placeholder

    auth, host = _resolve_databricks_auth(profile)
    base_url = host.rstrip("/") + "/serving-endpoints"
    return OpenAI(
        base_url=base_url,
        api_key=_placeholder,
        http_client=httpx.Client(auth=auth),
    )


def _convert_tools_to_openai(tools: list[ToolSpec]) -> list[ToolSpec]:
    """Convert our tool schemas to OpenAI function-calling format.

    Our tool schema looks like:
        {"name": "sql_query", "description": "...", "parameters": {...}}

    OpenAI expects:
        {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
    """
    result: list[ToolSpec] = []
    for tool in tools:
        # ``Message`` / ``ToolSpec`` are JSON-typed ``dict[str, Any]``; the
        # OpenAI SDK requires ``name``/``description`` as plain strings. The
        # ternaries below are the boundary coercion — a missing key in the
        # tool dict surfaces to OpenAI as an empty field rather than ``None``.
        raw_name = tool.get("name")
        raw_description = tool.get("description")
        fn: ToolSpec = {
            "name": raw_name if isinstance(raw_name, str) else "",
            "description": raw_description if isinstance(raw_description, str) else "",
        }
        if "parameters" in tool:
            fn["parameters"] = tool["parameters"]
        else:
            fn["parameters"] = {"type": "object", "properties": {}}
        result.append({"type": "function", "function": fn})
    return result


def _convert_messages(
    messages: list[Message],
    system_prompt: str,
) -> list[Message]:
    """Convert our internal message format to OpenAI chat messages.

    Our internal format uses roles: user, assistant, tool_call, tool_result.
    OpenAI expects: system, user, assistant (with optional tool_calls), tool.
    """
    result: list[Message] = []
    if system_prompt:
        result.append({"role": "system", "content": system_prompt})

    i = 0
    while i < len(messages):
        msg = messages[i]
        # ``Message`` is ``dict[str, Any]``; role/content are untyped JSON.
        # Narrow to ``str``/``""`` for the role dispatch below — a missing
        # role short-circuits to the default-user branch, mirroring the
        # fall-through for unknown roles.
        raw_role = msg.get("role")
        role = raw_role if isinstance(raw_role, str) else ""
        content = msg.get("content") if msg.get("content") is not None else ""

        if role == "user":
            text = str(content) if content else "(empty)"
            result.append({"role": "user", "content": text})

        elif role == "assistant":
            text = str(content) if content else "(empty)"
            result.append({"role": "assistant", "content": text})

        elif role == "tool_call":
            # Our format stores tool_call + tool_result as adjacent pairs.
            # OpenAI expects: assistant message with tool_calls array,
            # followed by a tool message with the result.
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    content = {}
            if isinstance(content, dict):
                raw_call_tool = content.get("tool")
                tool_name = raw_call_tool if isinstance(raw_call_tool, str) else ""
                tool_args = content.get("args", {})
            else:
                tool_name = ""
                tool_args = {}

            call_id = f"call_{i}"
            result.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(tool_args)
                                if isinstance(tool_args, dict)
                                else str(tool_args),
                            },
                        }
                    ],
                }
            )

            # Consume the following tool_result if present
            if i + 1 < len(messages) and messages[i + 1].get("role") == "tool_result":
                i += 1
                # ``Message.content`` is untyped JSON; non-str values are
                # json-encoded below. ``None`` / missing surfaces as ``""``.
                raw_tr = messages[i].get("content")
                tr_content = raw_tr if raw_tr is not None else ""
                if not isinstance(tr_content, str):
                    tr_content = json.dumps(tr_content)
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": tr_content,
                    }
                )

        elif role == "tool_result":
            # Orphan tool_result without a preceding tool_call — skip or
            # treat as a user message so the LLM sees it.
            tr_content = content if isinstance(content, str) else json.dumps(content)
            result.append({"role": "user", "content": f"[tool result] {tr_content}"})

        else:
            # Unknown role — pass through as user
            result.append({"role": "user", "content": str(content)})

        i += 1

    return result


def _extract_stream_text_delta(content: Any) -> str:
    """
    Extract assistant-visible text from a Chat Completions stream delta.

    Some OpenAI-compatible Databricks models stream ``delta.content`` as
    a list of typed content blocks instead of a plain string. Kimi, for
    example, emits ``{"type": "reasoning", "summary": [...]}`` blocks
    before the final answer. The legacy executor contract only supports
    assistant-visible text chunks, so reasoning blocks are ignored while
    recognized text blocks are concatenated.

    :param content: Raw ``choice.delta.content`` value from the OpenAI SDK,
        e.g. ``"hello"`` or ``[{"type": "text", "text": "hello"}]``.
    :returns: Plain text to emit as a :class:`TextChunk`, or ``""`` when
        the delta contains no assistant-visible text.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    pieces: list[str] = []
    for block in content:
        if isinstance(block, str):
            pieces.append(block)
            continue
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"text", "output_text"}:
            text = block.get("text")
            if isinstance(text, str):
                pieces.append(text)
    return "".join(pieces)


class DatabricksExecutor(Executor):
    """Execute agent turns using Databricks-hosted LLMs (or any OpenAI-compatible API).

    This is a synchronous (non-streaming) implementation that makes a single
    chat completions call per turn and maps the response to ExecutorEvents.

    Streaming support can be added later by setting ``stream=True`` and
    yielding TextChunk events as deltas arrive.
    """

    def __init__(self, client: OpenAI | None = None, profile: str | None = None) -> None:
        """Create a DatabricksExecutor.

        Args:
            client: An OpenAI client instance.  If ``None``, one is created
                    from OPENAI_BASE_URL/API_KEY or ~/.databrickscfg.
            profile: Databricks config profile name to use from ~/.databrickscfg,
                    or ``None`` to let the SDK's standard resolution order decide.

        Raises:
            ImportError: If the ``openai`` package is not installed.
            EnvironmentError: If no credentials are configured.
        """
        self._profile = profile
        self._client = client if client is not None else _get_openai_client(profile=profile)
        self._session_states: dict[str, _DatabricksSessionState] = {}

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def max_context_tokens(self) -> int | None:
        return None  # Let the model handle truncation

    def _session_key(self, messages: list[Message]) -> str:
        if messages:
            if messages[-1].get("session_id"):
                return str(messages[-1]["session_id"])
            metadata = messages[-1].get("metadata", {})
            if isinstance(metadata, dict) and metadata.get("session_id"):
                return str(metadata["session_id"])
        return "default"

    def _get_or_create_session_state(self, session_key: str) -> _DatabricksSessionState:
        state = self._session_states.get(session_key)
        if state is None:
            state = _DatabricksSessionState()
            self._session_states[session_key] = state
        return state

    async def close_session(self, session_key: str) -> None:
        self._session_states.pop(session_key, None)

    async def interrupt_session(self, session_key: str) -> bool:
        state = self._session_states.get(session_key)
        if state is None or state.active_stream is None:
            return False
        state.interrupt_requested = True
        await run_sync_on_thread(state.active_stream.close)
        return True

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """Call the LLM with streaming and yield ExecutorEvents as they arrive.

        Text content is yielded as TextChunk events in real time.  Tool calls
        are accumulated across stream chunks and yielded as ToolCallRequest
        events once the stream signals ``finish_reason="tool_calls"``.
        """
        cfg = config or ExecutorConfig()
        model = cfg.model
        if not model:
            model = "databricks-claude-sonnet-4-6"
        session_key = self._session_key(messages)
        state = self._get_or_create_session_state(session_key)
        state.interrupt_requested = False

        client = self._client

        oai_messages = _convert_messages(messages, system_prompt)
        oai_tools = _convert_tools_to_openai(tools) if tools else None

        kwargs: OpenAIKwargs = {
            "model": model,
            "messages": oai_messages,
            "max_tokens": cfg.max_tokens,
            "temperature": cfg.temperature,
            "stream": True,
        }
        if oai_tools:
            kwargs["tools"] = oai_tools
        kwargs.update(
            {
                key: value
                for key, value in cfg.extra.items()
                if key not in _SESSION_ONLY_EXECUTOR_EXTRA_KEYS
            }
        )

        try:
            logger.debug(
                "DatabricksExecutor: streaming %s with %d messages, %d tools",
                model,
                len(oai_messages),
                len(tools),
            )
            create_fn = client.chat.completions.create
            stream = await asyncio.wait_for(
                run_sync_on_thread(create_fn, **kwargs),
                timeout=_API_CALL_TIMEOUT_SECONDS,
            )
            state.active_stream = stream
        except asyncio.TimeoutError:
            logger.error(
                "DatabricksExecutor: API call timed out after %ss", _API_CALL_TIMEOUT_SECONDS
            )
            yield ExecutorError(
                message=f"LLM API call timed out after {int(_API_CALL_TIMEOUT_SECONDS)}s"
            )
            return
        except Exception as exc:  # noqa: BLE001 — executor boundary surfaces any error as an ExecutorError event
            logger.error("DatabricksExecutor: API call failed: %s", exc)
            # Append an env-var hint when stale ``DATABRICKS_*`` is set —
            # those silently shadow the profile lookup and surface here as
            # "Unable to load OAuth Config" / 401 with no other clue.
            from omnigent.onboarding.setup import detect_conflicting_env_vars

            conflicts = detect_conflicting_env_vars()
            extra = ""
            if conflicts:
                extra = (
                    f"\nHint: these env vars are set and may be overriding "
                    f"your profile: {', '.join(conflicts)}. Unset them, or "
                    f"run via `env -u {' -u '.join(conflicts)} <command>`."
                )
            yield ExecutorError(message=f"LLM API error: {exc}{extra}")
            return

        full_text = ""
        # Tool call arguments arrive in pieces; accumulate per index.
        pending_tool_calls: dict[int, dict[str, str]] = {}

        try:
            stream_iter = iterate_blocking_stream(stream)
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        anext(stream_iter),
                        timeout=_STREAM_IDLE_TIMEOUT_SECONDS,
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError as exc:
                    raise TimeoutError(
                        f"Databricks stream was idle for {int(_STREAM_IDLE_TIMEOUT_SECONDS)}s"
                    ) from exc
                if state.interrupt_requested:
                    return
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                if delta:
                    text_delta = _extract_stream_text_delta(delta.content)
                    if text_delta:
                        yield TextChunk(text=text_delta)
                        full_text += text_delta

                if delta and delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in pending_tool_calls:
                            pending_tool_calls[idx] = {"name": "", "arguments": ""}
                        if tc_delta.function:
                            if tc_delta.function.name:
                                pending_tool_calls[idx]["name"] = tc_delta.function.name
                            if tc_delta.function.arguments:
                                pending_tool_calls[idx]["arguments"] += tc_delta.function.arguments

                if choice.finish_reason == "tool_calls":
                    for idx in sorted(pending_tool_calls):
                        tc = pending_tool_calls[idx]
                        try:
                            args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                        except (json.JSONDecodeError, TypeError):
                            args = {"raw": tc["arguments"]}
                        yield ToolCallRequest(name=tc["name"], args=args)
                    return

                if choice.finish_reason == "stop":
                    if state.interrupt_requested:
                        return
                    yield TurnComplete(response=full_text)
                    return
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 — stream boundary converts any error into an ExecutorError event
            if state.interrupt_requested:
                return
            logger.error("DatabricksExecutor: stream error: %s", exc)
            yield ExecutorError(message=f"LLM stream error: {exc}")
            return
        finally:
            state.active_stream = None

        # Stream ended without an explicit finish_reason
        if state.interrupt_requested:
            return
        if pending_tool_calls:
            for idx in sorted(pending_tool_calls):
                tc = pending_tool_calls[idx]
                try:
                    args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except (json.JSONDecodeError, TypeError):
                    args = {"raw": tc["arguments"]}
                yield ToolCallRequest(name=tc["name"], args=args)
        else:
            yield TurnComplete(response=full_text)
