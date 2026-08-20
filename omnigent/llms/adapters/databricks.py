"""
Databricks Model Serving adapter.

Extends the OpenAI-compatible adapter with Databricks-specific
authentication. When ``connection_params`` omits ``base_url``, the
adapter resolves credentials via the databricks-sdk ``Config`` object,
which handles OAuth token refresh transparently on every call. Falls
back to :func:`~omnigent.runtime.credentials.databricks.resolve_databricks_workspace`
when the SDK is unavailable.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.llms.adapters.openai import OpenAICompatibleAdapter
from omnigent.runtime.credentials.databricks import resolve_databricks_workspace


class DatabricksAdapter(OpenAICompatibleAdapter):
    """
    Adapter for Databricks Model Serving.

    Credentials are resolved in the following order:

    1. ``connection_params`` passed at call time (from the ``connection:``
       block in the agent spec's ``llm:`` config) — used when present.
    2. A cached ``databricks.sdk.config.Config`` object whose
       ``authenticate()`` method is called on every request. The SDK
       handles OAuth token refresh transparently — no manual expiry
       tracking needed.
    3. :func:`~omnigent.runtime.credentials.databricks.resolve_databricks_workspace`
       as a final fallback when the SDK is unavailable.

    An :class:`~omnigent.errors.OmnigentError` is raised only when
    all paths fail.
    """

    # Cache Config per profile so we don't reconstruct it on every call,
    # while still supporting multiple profiles in the same process.
    _sdk_configs: dict[str | None, Any]  # profile → databricks.sdk.config.Config

    def __init__(self) -> None:
        super().__init__()
        self._sdk_configs = {}

    def _get_sdk_config(self, profile: str | None) -> Any:
        """Return a cached ``Config`` for *profile*, creating it if needed."""
        if profile not in self._sdk_configs:
            try:
                from databricks.sdk.config import Config

                sdk_profile = profile or os.environ.get("DATABRICKS_CONFIG_PROFILE")
                self._sdk_configs[profile] = Config(profile=sdk_profile)
            except Exception:
                self._sdk_configs[profile] = None
        return self._sdk_configs[profile]

    def _resolve_via_sdk(self, profile: str | None) -> dict[str, str] | None:
        """
        Resolve credentials via the SDK ``Config``.

        Calls ``Config.authenticate()`` on every invocation so the SDK
        can refresh OAuth tokens transparently.

        :returns: ``{"base_url": ..., "api_key": ...}`` or ``None``.
        """
        cfg = self._get_sdk_config(profile)
        if cfg is None:
            return None
        try:
            headers = cfg.authenticate()
        except Exception:
            # Token refresh or auth failure — fall back to configparser path.
            return None
        token = (headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if not token or not cfg.host:
            return None
        return {
            "base_url": cfg.host.rstrip("/") + "/serving-endpoints",
            "api_key": token,
        }

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]] | None,
        stream: bool,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Build the Chat Completions payload without ``stream_options``.

        Databricks model serving rejects ``stream_options`` with a 400 error
        (the field is an OpenAI extension that Databricks does not support).
        This override builds the standard payload and removes the key.

        :param messages: Chat Completions messages.
        :param model: Model name, e.g. ``"databricks-kimi-k2-6"``.
        :param tools: Tool schemas or ``None``.
        :param stream: Whether to enable streaming.
        :param extra: Additional kwargs (temperature, etc.).
        :returns: The request payload dict without ``stream_options``.
        """
        payload = super()._build_payload(messages, model, tools, stream, extra)
        payload.pop("stream_options", None)
        return payload

    async def chat_completions(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]] | None,
        stream: bool,
        extra: dict[str, Any],
        *,
        connection_params: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any] | AsyncIterator[dict[str, Any]]:
        """
        Send a Chat Completions request to Databricks Model Serving.

        :param messages: Chat Completions format messages.
        :param model: Model name, e.g. ``"databricks-gpt-5-4"``.
        :param tools: Tool schemas or ``None``.
        :param stream: Enable streaming.
        :param extra: Additional kwargs.
        :param connection_params: Optional. When provided, must contain
            ``"base_url"``; ``"api_key"`` is also expected. When absent
            or missing ``"base_url"``, credentials are resolved via the
            cached SDK ``Config`` (handles OAuth refresh) then falls back
            to :func:`~omnigent.runtime.credentials.databricks.resolve_databricks_workspace`.
        :param timeout: Request timeout in seconds. ``None`` uses
            the module default.
        :returns: Response dict or async iterator of chunk dicts.
        :raises OmnigentError: If ``connection_params`` lacks
            ``"base_url"`` and all credential resolution paths fail.
        """
        if not connection_params or "base_url" not in connection_params:
            profile = (connection_params or {}).get("profile")
            resolved = self._resolve_via_sdk(profile)
            if resolved is None:
                # SDK unavailable or auth failed — fall back to configparser path.
                try:
                    creds = resolve_databricks_workspace(None)
                except OSError as exc:
                    raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
                resolved = {
                    "base_url": creds.host + "/serving-endpoints",
                    "api_key": creds.token,
                }
            connection_params = {**resolved, **(connection_params or {})}
        return await super().chat_completions(
            messages,
            model,
            tools,
            stream,
            extra,
            connection_params=connection_params,
            timeout=timeout,
        )
