"""Router hooks tests for Codex app server."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.codex_native.app_server import (
    trust_all_codex_hooks,
    trust_codex_router_hooks,
)
from tests.harnesses.codex_native.app_server._support import (
    _CWD,
    _OUR_COMMAND,
    _USER_COMMAND,
    _batchwrite_calls,
    _FakeCodexClient,
    _hook,
    _test_app_server,
)

# --- Subagent-routing hook trust ---------------------------------------
#
# Empirically (codex-cli 0.145.0) ``--dangerously-bypass-hook-trust`` does
# NOT make hooks run under ``codex app-server``: an untrusted routing hook
# stayed silent with the flag and fired only once its
# ``hooks.state.<key>.trusted_hash`` was persisted. The routing hooks
# therefore need their own trust pass; the policy pass filters by module
# and would leave them untrusted (a silent fail-open on the spawn gate).

_ROUTER_GATE_COMMAND = (
    "/venv/bin/python -m omnigent.inner.hook_scripts.codex_router_hook "
    "route-subagent --bridge-dir /b --harness codex-native"
)


async def test_router_hooks_are_trusted_via_batchwrite() -> None:
    """Untrusted routing hooks are trusted with their currentHash."""
    client = _FakeCodexClient(
        hooks=[
            _hook("gate", _ROUTER_GATE_COMMAND, "untrusted", "sha256:gate"),
        ]
    )
    assert await trust_codex_router_hooks(client.request, cwd=_CWD) == []

    writes = _batchwrite_calls(client)
    assert len(writes) == 1
    edit = writes[0].params["edits"][0]
    assert edit["keyPath"] == "hooks.state"
    assert edit["value"] == {
        "gate": {"trusted_hash": "sha256:gate"},
    }
    assert writes[0].params["reloadUserConfig"] is True


async def test_router_hook_trust_never_touches_user_or_policy_hooks() -> None:
    """Only the routing hooks are trusted by the routing pass."""
    client = _FakeCodexClient(
        hooks=[
            _hook("gate", _ROUTER_GATE_COMMAND, "untrusted", "sha256:gate"),
            _hook("policy", _OUR_COMMAND, "untrusted", "sha256:policy"),
            _hook("theirs", _USER_COMMAND, "untrusted", "sha256:theirs"),
        ]
    )
    await trust_codex_router_hooks(client.request, cwd=_CWD)
    assert _batchwrite_calls(client)[0].params["edits"][0]["value"] == {
        "gate": {"trusted_hash": "sha256:gate"}
    }


async def test_router_hook_trust_reports_still_untrusted_without_raising() -> None:
    """A routing-trust failure is reported, never raised (policy must survive)."""
    client = _FakeCodexClient(
        hooks=[_hook("gate", _ROUTER_GATE_COMMAND, "untrusted")], flip_on_trust=False
    )
    assert await trust_codex_router_hooks(client.request, cwd=_CWD) == ["gate"]
    assert len(_batchwrite_calls(client)) == 1


async def test_router_hook_trust_noop_without_routing_hooks() -> None:
    """No routing hooks registered → no write, no failure."""
    client = _FakeCodexClient(hooks=[_hook("policy", _OUR_COMMAND, "trusted")])
    assert await trust_codex_router_hooks(client.request, cwd=_CWD) == []
    assert _batchwrite_calls(client) == []


async def test_already_trusted_router_hooks_skip_batchwrite() -> None:
    """Routing hooks already trusted issue no config write."""
    client = _FakeCodexClient(hooks=[_hook("gate", _ROUTER_GATE_COMMAND, "trusted")])
    assert await trust_codex_router_hooks(client.request, cwd=_CWD) == []
    assert _batchwrite_calls(client) == []


async def test_trust_step_covers_router_hooks_when_routing_armed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The startup trust step trusts the routing hooks alongside the policy hook."""
    from omnigent.inner.codex_executor import CODEX_ROUTER_DIR_ENV_VAR

    client = _FakeCodexClient(
        hooks=[
            _hook("policy", _OUR_COMMAND, "untrusted", "sha256:policy"),
            _hook("gate", _ROUTER_GATE_COMMAND, "untrusted", "sha256:gate"),
        ]
    )

    async def _fake_connect(self: Any) -> None:
        return None

    async def _fake_close(self: Any) -> None:
        return None

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server.CodexAppServerClient.connect", _fake_connect
    )
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server.CodexAppServerClient.close", _fake_close
    )
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server.CodexAppServerClient.request",
        lambda self, method, params: client.request(method, params),
    )

    server = _test_app_server(tmp_path, tmp_path / "codex-home", tmp_path / "bridge", Path(_CWD))
    server.env[CODEX_ROUTER_DIR_ENV_VAR] = str(tmp_path / "bridge")
    server.router_hooks_registered = True
    await server._trust_policy_hooks()

    trusted = {}
    for write in _batchwrite_calls(client):
        trusted.update(write.params["edits"][0]["value"])
    assert set(trusted) == {"policy", "gate"}


async def test_trust_all_codex_hooks_trusts_user_hooks() -> None:
    """
    The runner-owned trust-all pass covers user hooks, not just Omnigent's.

    Codex ignores ``--dangerously-bypass-hook-trust`` for the startup
    review screen on a persistent ``resume``; persisting trust for the
    merged user hooks is what keeps that screen from stranding a resumed
    web session, so this pass must reach hooks the policy trust never does.
    """
    client = _FakeCodexClient(
        hooks=[
            _hook("ours", _OUR_COMMAND, "untrusted", "sha256:ours"),
            _hook("theirs", _USER_COMMAND, "untrusted", "sha256:theirs"),
        ]
    )

    assert await trust_all_codex_hooks(client.request, cwd=_CWD) == []

    writes = _batchwrite_calls(client)
    assert len(writes) == 1
    written = writes[0].params["edits"][0]["value"]
    assert written == {
        "ours": {"trusted_hash": "sha256:ours"},
        "theirs": {"trusted_hash": "sha256:theirs"},
    }


async def test_trust_all_codex_hooks_reports_still_untrusted_without_raising() -> None:
    """
    A hash that never flips is returned, not raised.

    The trust-all pass is a UX fix (suppress the review screen), not a
    security gate, so a persistent failure only risks the screen
    reappearing and must never block startup.
    """
    client = _FakeCodexClient(
        hooks=[_hook("theirs", _USER_COMMAND, "untrusted", "sha256:theirs")],
        flip_on_trust=False,
    )

    assert await trust_all_codex_hooks(client.request, cwd=_CWD) == ["theirs"]


async def test_trust_step_trusts_user_hooks_only_when_trust_all_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    ``trust_all_hooks`` gates whether the startup step trusts user hooks.

    Runner-owned sessions enable it so a resumed web session never faces
    the interactive review screen; interactive CLI sessions leave it off so
    a human at the terminal reviews their own new or changed hooks.
    """

    async def _fake_connect(self: Any) -> None:
        return None

    async def _fake_close(self: Any) -> None:
        return None

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server.CodexAppServerClient.connect", _fake_connect
    )
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server.CodexAppServerClient.close", _fake_close
    )

    for trust_all, expected in ((True, {"policy", "theirs"}), (False, {"policy"})):
        client = _FakeCodexClient(
            hooks=[
                _hook("policy", _OUR_COMMAND, "untrusted", "sha256:policy"),
                _hook("theirs", _USER_COMMAND, "untrusted", "sha256:theirs"),
            ]
        )
        monkeypatch.setattr(
            "omnigent.harnesses.codex_native.app_server.CodexAppServerClient.request",
            lambda self, method, params, _c=client: _c.request(method, params),
        )
        server = _test_app_server(
            tmp_path, tmp_path / "codex-home", tmp_path / "bridge", Path(_CWD)
        )
        server.trust_all_hooks = trust_all
        await server._trust_policy_hooks()

        trusted: dict[str, Any] = {}
        for write in _batchwrite_calls(client):
            trusted.update(write.params["edits"][0]["value"])
        assert set(trusted) == expected


async def test_policy_hook_command_runs_python_isolated() -> None:
    """The policy hook command passes ``-I`` before ``-m``.

    Same silent fail-open as the routing hooks: codex runs hooks with the
    session workspace as cwd, so without isolation a workspace containing an
    ``omnigent`` directory shadows the installed package and the policy gate
    dies on an import error codex never reports.
    """
    import shlex

    from omnigent.harnesses.codex_native.app_server import _codex_policy_hook_command

    argv = shlex.split(_codex_policy_hook_command(Path("/b"), "/venv/bin/python"))
    assert argv[1:3] == ["-I", "-m"]
