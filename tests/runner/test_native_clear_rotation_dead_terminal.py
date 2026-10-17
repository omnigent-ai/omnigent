"""Regression: native /clear rotation guards must respect terminal liveness.

The transfer-inbound guards that decide "a terminal will be transferred in, so
skip auto-create" (``_claude_native_terminal_arrives_via_transfer`` and its
codex/antigravity mirrors in ``omnigent/runner/native/orchestration.py``) must
only count a *live* sibling terminal: the transfer they defer to skips dead
entries (``resource_registry.transfer_terminal`` →
``if not entry.instance.running: continue``). A guard that ignores liveness
lets a registered-but-dead ``<harness>:main`` report "inbound", auto-create is
skipped, and the subsequent transfer 404s — leaving the rotated-to session with
no terminal at all instead of the fresh pane auto-create would have made.

The reported user surface is the native terminal pane after ``/clear``; driving a
real native CLI rotation to a registered-but-dead terminal is not reproducible in
headless CI, so this drives the real runner ASGI routes with the real guard and
real registry — the same lane as
``tests/runner/test_app_sessions_native_terminals_autocreate.py``, whose existing
coverage models a "dead" terminal only as *absent* (never registered-but-dead).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.harnesses.antigravity_native.bridge import (
    ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
    AntigravityNativeBridgeState,
)
from omnigent.harnesses.antigravity_native.bridge import (
    prepare_bridge_dir as prepare_antigravity_bridge_dir,
)
from omnigent.harnesses.antigravity_native.bridge import (
    write_bridge_state as write_antigravity_bridge_state,
)
from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native.bridge import BRIDGE_ID_LABEL_KEY, prepare_bridge_dir
from omnigent.harnesses.codex_native.bridge import (
    CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
    CodexNativeBridgeState,
)
from omnigent.harnesses.codex_native.bridge import (
    prepare_bridge_dir as prepare_codex_bridge_dir,
)
from omnigent.harnesses.codex_native.bridge import (
    write_bridge_state as write_codex_bridge_state,
)
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner import create_runner_app
from omnigent.runner.native import orchestration
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient

# Two distinct 32-hex session ids: the new (rotated-to) session and the
# superseded sibling that still holds the (now dead) terminal registration.
NEW_SESSION = "2d1b1a96e3e08f2cd43c0cc4b695ac5d"
OLD_SESSION = "3bb59abc6e20b834cbb2269f28880895"
SHARED_BRIDGE = "bridge_shared"


class _GuardServerClient:
    """Answers the snapshot/labels/items GETs the create-session guard issues.

    A real stub — not ``MagicMock`` — so an unexpected call shape fails loudly.
    """

    def __init__(self, labels: dict[str, str]) -> None:
        self._labels = labels

    async def get(self, url: str, **kwargs: Any) -> Any:
        del kwargs

        class _Response:
            def __init__(self, payload: dict[str, Any]) -> None:
                self.status_code = 200
                self._payload = payload

            def json(self) -> dict[str, Any]:
                return self._payload

        if url.endswith("/items"):
            return _Response({"data": [], "has_more": False})
        if url.endswith("/labels"):
            return _Response({"labels": self._labels})
        return _Response({"id": NEW_SESSION, "labels": self._labels})


def _seed_claude_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    prepare_bridge_dir(OLD_SESSION, bridge_id=SHARED_BRIDGE, workspace=tmp_path)


def _seed_codex_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT", tmp_path / "codex-native"
    )
    seed_dir = prepare_codex_bridge_dir(SHARED_BRIDGE)
    write_codex_bridge_state(
        seed_dir,
        CodexNativeBridgeState(
            session_id=OLD_SESSION,
            socket_path="ws://127.0.0.1:9876",
            thread_id="thread_old",
            codex_home=str(tmp_path / "codex-home"),
        ),
    )


def _seed_antigravity_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.harnesses.antigravity_native.bridge._BRIDGE_ROOT",
        tmp_path / "antigravity-native",
    )
    seed_dir = prepare_antigravity_bridge_dir(SHARED_BRIDGE)
    write_antigravity_bridge_state(
        seed_dir,
        AntigravityNativeBridgeState(session_id=OLD_SESSION, conversation_id="cascade_old"),
    )


@dataclass
class _HarnessCase:
    """A native harness whose /clear-rotation guard shares the liveness bug.

    :param harness_name: Executor harness key, e.g. ``"claude-native"``.
    :param terminal_name: Registry terminal name, e.g. ``"claude"``.
    :param bridge_label_key: Session-label key carrying the shared bridge id.
    :param auto_create_attr: ``orchestration`` attribute to stub and record.
    :param seed_bridge: Seeds the shared bridge's active session = OLD_SESSION.
    :param guard_attr: ``orchestration`` transfer-inbound guard to call.
    """

    harness_name: str
    terminal_name: str
    bridge_label_key: str
    auto_create_attr: str
    seed_bridge: Callable[[Path, pytest.MonkeyPatch], None]
    guard_attr: str


_HARNESS_CASES = [
    _HarnessCase(
        "claude-native",
        "claude",
        BRIDGE_ID_LABEL_KEY,
        "_auto_create_claude_terminal",
        _seed_claude_bridge,
        "_claude_native_terminal_arrives_via_transfer",
    ),
    _HarnessCase(
        "codex-native",
        "codex",
        CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
        "_auto_create_codex_terminal",
        _seed_codex_bridge,
        "_codex_native_terminal_arrives_via_transfer",
    ),
    _HarnessCase(
        "antigravity-native",
        "antigravity",
        ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
        "_auto_create_antigravity_terminal",
        _seed_antigravity_bridge,
        "_antigravity_native_terminal_arrives_via_transfer",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _HARNESS_CASES, ids=[c.harness_name for c in _HARNESS_CASES])
async def test_dead_registered_sibling_terminal_still_auto_creates(
    case: _HarnessCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registered-but-dead ``<harness>:main`` must not suppress auto-create.

    The shared bridge names OLD_SESSION as active and OLD_SESSION owns a
    ``<harness>:main`` registry entry whose ``instance.running`` is ``False``.
    Nothing can transfer in (``transfer_terminal`` skips dead entries), so the
    guard must let the new session bootstrap its own terminal. A guard that
    ignores liveness reports "inbound" and skips auto-create, which — combined
    with the transfer 404 asserted below — strands the new session with no
    terminal.
    """
    case.seed_bridge(tmp_path, monkeypatch)

    terminal_registry = TerminalRegistry()
    dead = TerminalInstance(
        name=case.terminal_name,
        session_key="main",
        socket_path=tmp_path / f"{case.terminal_name}.sock",
        private_dir=tmp_path / case.terminal_name,
        running=False,
    )
    terminal_registry._by_conversation[OLD_SESSION] = {(case.terminal_name, "main"): dead}

    created: list[str] = []

    async def _recording_auto_create(
        session_id: str, resource_registry: Any, publish_event: Any, **_kwargs: Any
    ) -> None:
        del resource_registry, publish_event
        created.append(session_id)

    monkeypatch.setattr(
        f"omnigent.runner.native.orchestration.{case.auto_create_attr}",
        _recording_auto_create,
    )

    native_spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": case.harness_name}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return native_spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=_GuardServerClient({case.bridge_label_key: SHARED_BRIDGE}),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            "/v1/sessions",
            json={"session_id": NEW_SESSION, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
        )
    assert resp.status_code == 201, resp.text

    assert created == [NEW_SESSION], (
        f"{case.harness_name}: a registered-but-dead {case.terminal_name}:main under the "
        f"active sibling must not suppress auto-create (nothing can transfer in); "
        f"got created={created}"
    )


@pytest.mark.asyncio
async def test_transfer_of_dead_registered_terminal_404s(tmp_path: Path) -> None:
    """The transfer the guard defers to 404s a registered-but-dead terminal.

    This is the downstream half of the failure: skipping auto-create is only
    safe if the transfer actually delivers the pane, but ``transfer_terminal``
    skips non-running entries, so the route answers 404 and the rotated-to
    session ends up with no terminal.
    """
    terminal_registry = TerminalRegistry(conversation_link_base_url="http://127.0.0.1:8000")
    dead = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "claude.sock",
        private_dir=tmp_path / "claude",
        running=False,
    )
    terminal_registry._by_conversation[OLD_SESSION] = {("claude", "main"): dead}

    app = create_runner_app(
        terminal_registry=terminal_registry,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    terminal_id = terminal_resource_id("claude", "main")
    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{OLD_SESSION}/resources/terminals/{terminal_id}/transfer",
            json={"target_session_id": NEW_SESSION},
        )

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True], ids=["dead", "live"])
@pytest.mark.parametrize("case", _HARNESS_CASES, ids=[c.harness_name for c in _HARNESS_CASES])
async def test_transfer_inbound_guard_tracks_terminal_liveness(
    case: _HarnessCase,
    running: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard reports inbound only when the sibling terminal is live.

    Calls the guard directly: a live ``<harness>:main`` under the bridge's
    active sibling is a genuine rotation (inbound), while a registered-but-dead
    one can never transfer, so it must read as not-inbound.
    """
    case.seed_bridge(tmp_path, monkeypatch)

    terminal_registry = TerminalRegistry()
    terminal_registry._by_conversation[OLD_SESSION] = {
        (case.terminal_name, "main"): TerminalInstance(
            name=case.terminal_name,
            session_key="main",
            socket_path=tmp_path / f"{case.terminal_name}.sock",
            private_dir=tmp_path / case.terminal_name,
            running=running,
        )
    }

    guard = getattr(orchestration, case.guard_attr)
    inbound = await guard(
        server_client=_GuardServerClient({case.bridge_label_key: SHARED_BRIDGE}),
        session_id=NEW_SESSION,
        resource_registry=SessionResourceRegistry(terminal_registry=terminal_registry),
    )

    assert inbound is running, (
        f"{case.harness_name}: guard must report inbound only for a live "
        f"{case.terminal_name}:main (running={running}); got inbound={inbound}"
    )
