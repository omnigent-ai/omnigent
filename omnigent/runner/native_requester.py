"""Resolve native call provenance through the existing harness control channel."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from omnigent.harnesses.codex_native.bridge import (
    CODEX_NATIVE_BRIDGE_DIR_ENV_VAR,
    build_codex_native_spawn_env,
)
from omnigent.harnesses.codex_native.execution import CodexExecutionIdentity
from omnigent.harnesses.codex_native.terminal_proxy import RegisteredTerminalInput
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager, NoLiveHarnessError
from omnigent.runtime.requester import ExecutionRequester, InputRequester, ToolRequester

if TYPE_CHECKING:
    from pathlib import Path

    from omnigent.inner.terminal import TerminalInstance
    from omnigent.terminals.registry import TerminalRegistry
    from omnigent.terminals.writer import TerminalWriterEvidence

_logger = logging.getLogger(__name__)


class CodexTerminalInputBinding:
    """Bind TUI submissions to one observed terminal and its live harness."""

    def __init__(
        self,
        manager: HarnessProcessManager,
        registry: TerminalRegistry,
        session_id: str,
        provider_url: str,
        *,
        bridge_dir: Path,
    ) -> None:
        self._manager = manager
        self._registry = registry
        self._session_id = session_id
        self._provider_url = provider_url
        self._spawn_env = build_codex_native_spawn_env(session_id)
        self._spawn_env[CODEX_NATIVE_BRIDGE_DIR_ENV_VAR] = str(bridge_dir)
        self._terminal: TerminalInstance | None = None
        self._evidence: TerminalWriterEvidence | None = None

    def bind(self, terminal: TerminalInstance) -> None:
        self._terminal = terminal
        self._evidence = terminal.writer_evidence

    def _check_incarnation(self) -> None:
        if (
            self._terminal is None
            or self._registry.get(self._session_id, "codex", "main") is not self._terminal
            or self._terminal.writer_evidence is not self._evidence
            or not self._terminal.running
        ):
            raise RuntimeError("Terminal input source changed or is not ready")

    async def register(
        self, thread_id: str, expected_turn_id: str | None
    ) -> RegisteredTerminalInput:
        self._check_incarnation()
        evidence = self._evidence
        assert evidence is not None
        snapshot = evidence.snapshot(evidence.incarnation_id)
        assert snapshot is not None
        origin = InputRequester(
            user_id=snapshot.writer.user_id,
            account_generation=snapshot.writer.account_generation,
        )
        try:
            # A new terminal submission can restart a harness retired while idle.
            client = await self._manager.get_client(
                self._session_id,
                "codex-native",
                env=self._spawn_env,
                replace_existing=False,
                validate=self._check_incarnation,
            )
            self._check_incarnation()

            async def register_origin(requester: InputRequester) -> RegisteredTerminalInput:
                response = await client.post(
                    f"/v1/sessions/{self._session_id}/native-input",
                    json={
                        "requester": requester.model_dump(),
                        "thread_id": thread_id,
                        "expected_turn_id": expected_turn_id,
                        "provider_url": self._provider_url,
                    },
                    timeout=10,
                )
                response.raise_for_status()
                client_id = InputRequester(input_id=response.json()["client_id"]).input_id

                async def refuse() -> None:
                    try:
                        refusal = await client.post(
                            f"/v1/sessions/{self._session_id}/native-input/refused",
                            json={"client_id": client_id},
                            timeout=10,
                        )
                        refusal.raise_for_status()
                    except httpx.HTTPError as exc:
                        raise RuntimeError("Native input refusal could not be recorded") from exc

                return RegisteredTerminalInput(client_id, refuse)

            registration = await register_origin(origin)
            try:
                self._check_incarnation()
            except RuntimeError:
                await registration.refuse()
                raise
            if evidence.snapshot(snapshot.incarnation_id) != snapshot:
                # Bytes can arrive during IPC. Never authenticate a mixed composer
                # using the writer seen before that await.
                replacement = await register_origin(InputRequester())
                # Keep a pending restriction continuously through the downgrade.
                await registration.refuse()
                registration = replacement
                try:
                    self._check_incarnation()
                except RuntimeError:
                    await registration.refuse()
                    raise
            return registration
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("Terminal input registration unavailable") from exc


async def resolve_codex_requester(
    manager: HarnessProcessManager | None,
    session_id: str,
    execution: CodexExecutionIdentity,
) -> ToolRequester:
    """Query only the live executor; missing or replaced processes have no authority."""
    unknown = ExecutionRequester(InputRequester()).snapshot()
    if manager is None:
        return unknown
    try:
        client = await manager.get_client(session_id, "any")
        response = await client.post(
            f"/v1/sessions/{session_id}/native-requester",
            json={"codex_execution": execution.to_wire()},
            timeout=10,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("requester") is None:
            return unknown
        return ToolRequester.model_validate(body["requester"])
    except (NoLiveHarnessError, httpx.HTTPError, ValueError):
        _logger.warning("Native requester could not be resolved for session %s", session_id)
        return unknown
