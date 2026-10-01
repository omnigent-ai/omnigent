"""Runner routes that list a session's models, model options, and resolvable skills."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine, Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnigent.harnesses.claude_native.main import ClaudeNativeUcodeConfig

import click
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.runner.app_support import (
    _client_safe_error_detail,
    _SpecEntry,
)
from omnigent.runner.native import (
    _claude_native_bridge_id_for_session,
    _CodexNativeModelOptionsNotReady,
)
from omnigent.spec.types import AgentSpec, SkillSpec
from omnigent.tools.builtins.load_skill import (
    find_skill_by_name,
    format_skill_meta_text,
)
from omnigent.util.json_types import JsonObject as _JsonObject

_logger = logging.getLogger("omnigent.runner.app")


# Claude-native session model listing: how long one request waits inline for
# the probe before answering 503-pending, and how long the probe may stay
# pending before the configured rows are served instead. Module-level so
# tests can patch the pacing.
_CLAUDE_MODEL_OPTIONS_INLINE_WAIT_S = 2.5


# How long a claude-native session's model rows are served from the runner's
# own cache before the shared catalog store is re-read, so a store refresh
# (its background re-probe) reaches the picker without a runner restart.
_CLAUDE_MODEL_OPTIONS_CACHE_TTL_S = 15.0


def register_model_option_routes(
    app: FastAPI,
    *,
    _claude_model_options_rows: dict[str, tuple[float, list[dict[str, object]]]],
    _codex_native_model_options: Callable[[str], Coroutine[Any, Any, list[_JsonObject]]],
    _opencode_native_model_options: Callable[[str], Coroutine[Any, Any, list[_JsonObject]]],
    _resolve_session_agent_spec: Callable[[str], Coroutine[Any, Any, AgentSpec | None]],
    _resolve_session_claude_launch_config: Callable[
        [str], Coroutine[Any, Any, ClaudeNativeUcodeConfig | None]
    ],
    _resolve_session_skills: Callable[[str], Coroutine[Any, Any, list[SkillSpec]]],
    _session_cursor_model_names: dict[str, dict[str, str]],
    _session_harness_name: Callable[[str], str | None],
    _session_spec_cache: dict[str, _SpecEntry | None],
    server_client: httpx.AsyncClient,
) -> None:
    """Register the ``/models``, ``/*-model-options``, and ``/skills/resolve`` routes on *app*.

    The keyword arguments are the runner app's shared session state and helpers.
    """

    @app.get("/v1/sessions/{session_id}/models")
    async def get_session_models(session_id: str) -> JSONResponse:
        spec = await _resolve_session_agent_spec(session_id)
        if spec is None:
            return JSONResponse(status_code=200, content={"workers": {}})
        from omnigent.models.model_catalog import catalog_for_spec

        try:
            catalog = await asyncio.to_thread(catalog_for_spec, spec)
        except Exception:
            _logger.exception(
                "get_session_models: catalog_for_spec failed for session=%s",
                session_id,
                extra={"session_id": session_id},
            )
            return JSONResponse(status_code=200, content={"workers": {}})
        return JSONResponse(status_code=200, content={"workers": catalog})

    @app.get("/v1/sessions/{session_id}/codex-model-options")
    async def get_session_codex_model_options(session_id: str) -> JSONResponse:
        harness = _session_harness_name(session_id)
        if harness not in ("codex-native", "opencode-native"):
            return JSONResponse(status_code=200, content={"models": []})
        if harness == "opencode-native":
            try:
                models = await _opencode_native_model_options(session_id)
                return JSONResponse(
                    status_code=200,
                    content={"models": _with_model_configuration_source(session_id, models)},
                )
            except _CodexNativeModelOptionsNotReady:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "opencode_native_model_options_failed",
                        "detail": "OpenCode-native app-server is not ready yet.",
                    },
                )
            except Exception as exc:  # noqa: BLE001 - picker failures are retryable.
                _logger.warning(
                    "OpenCode-native model list failed for %s: %s",
                    session_id,
                    exc,
                    extra={"session_id": session_id},
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "opencode_native_model_options_failed",
                        "detail": _client_safe_error_detail(
                            exc, context="opencode-native model options"
                        ),
                    },
                )
        try:
            models = await _codex_native_model_options(session_id)
            return JSONResponse(
                status_code=200,
                content={"models": _with_model_configuration_source(session_id, models)},
            )
        except _CodexNativeModelOptionsNotReady:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_model_options_failed",
                    "detail": "Codex-native model options are not ready yet.",
                },
            )
        except Exception as exc:  # noqa: BLE001 - surface Codex app-server failures to AP.
            _logger.warning(
                "Codex-native model/list failed for session=%s",
                session_id,
                exc_info=True,
                extra={"session_id": session_id},
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_model_options_failed",
                    "detail": _client_safe_error_detail(exc, context="codex-native model options"),
                },
            )

    @app.get("/v1/sessions/{session_id}/kiro-model-options")
    async def get_session_kiro_model_options(session_id: str) -> JSONResponse:
        if _session_harness_name(session_id) != "kiro-native":
            return JSONResponse(status_code=200, content={"models": []})
        from omnigent.harnesses.kiro_native.main import list_kiro_cli_model_options

        try:
            models = await asyncio.to_thread(list_kiro_cli_model_options)
        except Exception as exc:  # noqa: BLE001 - picker failures are retryable.
            _logger.warning(
                "Kiro-native model discovery failed for session=%s",
                session_id,
                exc_info=True,
                extra={"session_id": session_id},
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "kiro_native_model_options_failed",
                    "detail": _client_safe_error_detail(exc, context="kiro-native model options"),
                },
            )
        return JSONResponse(
            status_code=200,
            content={"models": _with_model_configuration_source(session_id, models)},
        )

    @app.get("/v1/sessions/{session_id}/devin-model-options")
    async def get_session_devin_model_options(session_id: str) -> JSONResponse:
        if _session_harness_name(session_id) != "devin-native":
            return JSONResponse(status_code=200, content={"models": []})
        from omnigent.harnesses.devin_native.main import list_devin_cli_model_options

        try:
            models = await asyncio.to_thread(list_devin_cli_model_options)
        except Exception as exc:  # noqa: BLE001 - picker failures are retryable.
            _logger.warning(
                "Devin-native model discovery failed for session=%s",
                session_id,
                exc_info=True,
                extra={"session_id": session_id},
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "devin_native_model_options_failed",
                    "detail": _client_safe_error_detail(exc, context="devin-native model options"),
                },
            )
        return JSONResponse(status_code=200, content={"models": models})

    @app.get("/v1/sessions/{session_id}/cursor-model-options")
    async def get_session_cursor_model_options(session_id: str) -> JSONResponse:
        if _session_harness_name(session_id) != "cursor-native":
            return JSONResponse(status_code=200, content={"models": []})
        from omnigent.harnesses.cursor_native.main import list_cursor_cli_model_options

        try:
            models = await asyncio.to_thread(list_cursor_cli_model_options)
        except Exception as exc:  # noqa: BLE001 - picker failures are retryable.
            _logger.warning(
                "Cursor-native model discovery failed for session=%s",
                session_id,
                exc_info=True,
                extra={"session_id": session_id},
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "cursor_native_model_options_failed",
                    "detail": _client_safe_error_detail(
                        exc, context="cursor-native model options"
                    ),
                },
            )
        _session_cursor_model_names[session_id] = {
            str(option["id"]): str(option["displayName"])
            for option in models
            if option.get("id") and option.get("displayName")
        }
        return JSONResponse(
            status_code=200,
            content={"models": _with_model_configuration_source(session_id, models)},
        )

    def _model_configuration_source(session_id: str) -> dict[str, str] | None:
        """Return the session's non-secret model-provider coordinates."""
        from omnigent.models.model_catalog import (
            model_configuration_source,
            resolve_model_provider,
        )

        spec_entry = _session_spec_cache.get(session_id)
        if spec_entry is None:
            return None
        spec = spec_entry.spec if hasattr(spec_entry, "spec") else spec_entry
        harness = _session_harness_name(session_id)
        provider = resolve_model_provider(spec, harness)
        return model_configuration_source(provider, harness=harness)

    def _with_model_configuration_source(
        session_id: str, rows: Sequence[Mapping[str, object]]
    ) -> list[dict[str, object]]:
        source = _model_configuration_source(session_id)
        if source is None:
            return [dict(row) for row in rows]
        return [{**row, "source": source} for row in rows]

    @app.get("/v1/sessions/{session_id}/claude-model-options")
    async def get_session_claude_model_options(session_id: str) -> JSONResponse:
        if _session_harness_name(session_id) != "claude-native":
            return JSONResponse(status_code=200, content={"models": []})
        cached = _claude_model_options_rows.get(session_id)
        if cached is not None:
            expires_at, cached_rows = cached
            if time.monotonic() < expires_at:
                return JSONResponse(status_code=200, content={"models": cached_rows})
        try:
            claude_config = await _resolve_session_claude_launch_config(session_id)
        except click.ClickException as exc:
            _logger.warning(
                "Claude-native model options unavailable for session=%s: %s",
                session_id,
                exc.message,
                extra={"session_id": session_id},
            )
            return JSONResponse(
                status_code=424,
                content={
                    "error": "claude_native_model_options_config",
                    "detail": exc.message,
                },
            )
        except Exception as exc:  # noqa: BLE001 — retryable model-options failure
            _logger.warning(
                "Claude-native model discovery failed for session=%s",
                session_id,
                exc_info=True,
                extra={"session_id": session_id},
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_model_options_failed",
                    "detail": _client_safe_error_detail(
                        exc,
                        context="claude-native model options",
                    ),
                },
            )
        from omnigent.harnesses.claude_native.main import claude_launch_catalog

        rows: list[dict[str, object]] | None
        try:
            # The store's single-flight probe survives this wait expiring
            # (ensure_catalog shields it), so a 503 here is genuinely
            # "pending", not "restarted".
            async with asyncio.timeout(_CLAUDE_MODEL_OPTIONS_INLINE_WAIT_S):
                rows = await claude_launch_catalog(claude_config)
        except TimeoutError:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_model_options_pending",
                    "detail": "the harness model probe is still resolving",
                },
            )
        if rows is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "claude_native_model_options_failed",
                    "detail": "the harness model probe failed; retrying",
                },
            )
        rows = _with_model_configuration_source(session_id, rows)
        _claude_model_options_rows[session_id] = (
            time.monotonic() + _CLAUDE_MODEL_OPTIONS_CACHE_TTL_S,
            rows,
        )
        # Executor parity: a cold launch may have recorded no picker
        # vocabulary (the store had no catalog yet), while routing decisions
        # accept picks against THIS listing. Refresh the bridge snapshot so
        # a routed turn's executor translates exactly the vocabulary served
        # here — including an authoritative empty catalog, which clears
        # stale launch values. Best-effort: the terminal may not exist yet.
        try:
            from omnigent.harnesses.claude_native.bridge import (
                bridge_dir_for_bridge_id,
                record_model_vocabulary,
            )
            from omnigent.models.claude_model_vocabulary import picker_command_values

            bridge_id = await _claude_native_bridge_id_for_session(
                server_client=server_client,
                session_id=session_id,
            )
            await asyncio.to_thread(
                record_model_vocabulary,
                bridge_dir_for_bridge_id(bridge_id),
                launch_env=None,
                launch_model=None,
                picker_values=picker_command_values(rows),
            )
        except Exception:  # noqa: BLE001 — vocabulary refresh is advisory
            _logger.debug(
                "claude-native model options: bridge vocabulary refresh skipped for session=%s",
                session_id,
                exc_info=True,
                extra={"session_id": session_id},
            )
        return JSONResponse(status_code=200, content={"models": rows})

    @app.get("/v1/sessions/{session_id}/model-options")
    async def get_session_model_options(session_id: str) -> JSONResponse:
        """One route for every harness family's session model listing.

        The runner derives the harness from the session — the four
        harness-named routes above/below remain as compatibility aliases
        for older servers (deprecated; remove in 0.11.0).
        """
        harness = _session_harness_name(session_id)
        if harness == "claude-native":
            return await get_session_claude_model_options(session_id)
        if harness in ("codex-native", "opencode-native"):
            return await get_session_codex_model_options(session_id)
        if harness == "cursor-native":
            return await get_session_cursor_model_options(session_id)
        if harness == "kiro-native":
            return await get_session_kiro_model_options(session_id)
        if harness == "devin-native":
            return await get_session_devin_model_options(session_id)
        return JSONResponse(status_code=200, content={"models": []})

    @app.post("/v1/sessions/{session_id}/skills/resolve")
    async def resolve_session_skill(session_id: str, request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "detail": "Request body must be JSON."},
            )
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "detail": "Request body must be a JSON object.",
                },
            )
        name = body.get("name")
        arguments = body.get("arguments", "")
        if not isinstance(name, str) or not name:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "detail": "'name' is required."},
            )
        if not isinstance(arguments, str):
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "detail": "'arguments' must be a string."},
            )
        skills = await _resolve_session_skills(session_id)
        skill = find_skill_by_name(skills, name)
        if skill is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "skill_not_found",
                    "detail": (f"Skill {name!r} not found for session {session_id!r}."),
                    "available": sorted(s.name for s in skills),
                },
            )
        return JSONResponse(
            status_code=200,
            content={"meta_text": format_skill_meta_text(skill, arguments)},
        )
