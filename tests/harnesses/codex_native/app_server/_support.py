"""Shared support for Codex app server tests."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    CodexNativeAppServer,
)

# Spell out expected approvals; deriving them from the production constant
# would hide an unintended expansion of the approved tool surface.
_PLAIN_TOOL_APPROVALS = {"sys_session_rename": {"approval_mode": "approve"}}


_CWD = "/home/user/repo"


_OUR_COMMAND = (
    "/venv/bin/python -m omnigent.harnesses.codex_native.hook evaluate-policy --bridge-dir /b"
)


_USER_COMMAND = "bash /home/user/.config/llm-cli/hooks/guard.sh"


def _hook(key: str, command: str, trust: str, current_hash: str = "sha256:h") -> dict[str, Any]:
    """
    Build a ``hooks/list`` hook metadata entry.

    :param key: Hook key, e.g. ``"/b/codex-home/hooks.json:pre_tool_use:0:0"``.
    :param command: Hook command string (used to identify ownership).
    :param trust: Trust status, e.g. ``"untrusted"`` / ``"trusted"``.
    :param current_hash: The hook's content hash, e.g. ``"sha256:h"``.
    :returns: A hook metadata dict shaped like ``hooks/list`` output.
    """
    return {
        "key": key,
        "command": command,
        "trustStatus": trust,
        "currentHash": current_hash,
    }


@dataclass
class _Req:
    """
    One recorded JSON-RPC request issued to the fake client.

    :param method: RPC method name, e.g. ``"hooks/list"``.
    :param params: RPC params dict.
    """

    method: str
    params: dict[str, Any]


@dataclass
class _FakeCodexClient:
    """
    Fake Codex app-server client scripted for the trust flow.

    Returns the current hook set for ``hooks/list`` and, on
    ``config/batchWrite``, flips a hook to ``trusted`` when the written
    ``trusted_hash`` matches the hook's ``currentHash`` (mirroring codex's
    real trust evaluation). ``flip_on_trust=False`` simulates a hash
    mismatch where trust never takes.

    :param hooks: Initial hook metadata (mutated as trust is written).
    :param flip_on_trust: Whether a matching trusted_hash flips trust.
    """

    hooks: list[dict[str, Any]]
    flip_on_trust: bool = True
    requests: list[_Req] = field(default_factory=list)

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Handle one scripted RPC request.

        :param method: RPC method, e.g. ``"hooks/list"`` or
            ``"config/batchWrite"``.
        :param params: RPC params.
        :returns: A response envelope matching the real app-server shape.
        """
        self.requests.append(_Req(method=method, params=params))
        if method == "hooks/list":
            return {"result": {"data": [{"cwd": _CWD, "hooks": self.hooks}]}}
        if method == "config/batchWrite":
            if self.flip_on_trust:
                written = params["edits"][0]["value"]
                for hook in self.hooks:
                    update = written.get(hook["key"])
                    if update and update.get("trusted_hash") == hook["currentHash"]:
                        hook["trustStatus"] = "trusted"
            return {"result": {"status": "ok"}}
        raise AssertionError(f"unexpected RPC method {method!r}")


def _batchwrite_calls(client: _FakeCodexClient) -> list[_Req]:
    """
    Return the config/batchWrite requests the trust flow issued.

    :param client: The fake client after the flow ran.
    :returns: Recorded batchWrite requests (empty if none issued).
    """
    return [r for r in client.requests if r.method == "config/batchWrite"]


@dataclass
class _FakeStartupClient:
    """Minimal initialized client returned by startup unit-test probes."""

    close_calls: int = 0

    async def close(self) -> None:
        self.close_calls += 1


async def _fake_wait_until_ready(self: CodexNativeAppServer) -> _FakeStartupClient:
    """
    Skip app-server socket probing in startup unit tests.

    :param self: The app-server wrapper under test.
    :returns: Initialized fake client owned by the startup flow.
    """
    return _FakeStartupClient()


async def _fake_trust_policy_hooks(
    self: CodexNativeAppServer, *, client: CodexAppServerClient | None = None
) -> None:
    """
    Skip Codex ``hooks/list`` RPCs in startup unit tests.

    :param self: The app-server wrapper under test.
    :param client: Reused initialized startup client.
    :returns: None.
    """


def _disable_codex_startup_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Patch Codex startup RPC waits for unit tests.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(CodexNativeAppServer, "_wait_until_ready", _fake_wait_until_ready)
    monkeypatch.setattr(CodexNativeAppServer, "_trust_policy_hooks", _fake_trust_policy_hooks)


def _test_app_server(
    tmp_path: Path,
    codex_home: Path,
    bridge_dir: Path,
    workspace: Path,
    env: dict[str, str] | None = None,
) -> CodexNativeAppServer:
    """
    Build a Codex app-server wrapper for startup unit tests.

    :param tmp_path: Test temp directory, e.g. ``Path("/tmp/test")``.
    :param codex_home: Private Codex home to write.
    :param bridge_dir: Bridge directory for the generated MCP args.
    :param workspace: Working directory for the subprocess.
    :param env: Process env for the app-server, carrying the routing
        signals the session class is read from. ``None`` is a plain session.
    :returns: Configured app-server wrapper.
    """
    return CodexNativeAppServer(
        codex_path=sys.executable,
        socket_path=tmp_path / "codex.sock",
        codex_home=codex_home,
        env=dict(env or {}),
        config_overrides=[],
        cwd=workspace,
        bridge_dir=bridge_dir,
        python_executable="/new/python",
    )


# --- Codex version gate + fail-open startup ---------------------------


def _set_codex_version(
    monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int] | None
) -> None:
    """
    Stub the codex version probe used by :meth:`start`.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param version: Version tuple to report, e.g. ``(0, 128, 0)``, or
        ``None`` to simulate an unparseable ``codex --version``.
    :returns: None.
    """

    async def _fake_version(_codex_path: str) -> tuple[int, int, int] | None:
        return version

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server._codex_cli_version", _fake_version
    )
