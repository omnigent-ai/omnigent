"""Regression: an agent's configured context window must reach Qwen Code.

A qwen-native agent whose spec sets ``context_window`` (e.g. 262144) launches a
qwen TUI through the runner's auto-create path. The configured window must be
conveyed to Qwen Code on some launch channel it reads -- CLI args, the terminal
process env, or a qwen settings/config file Omnigent writes for the session.

While the bug is live the value reaches none of those channels: ``qwen_args``
carries no context flag, the terminal spec sets no context env var, and Omnigent
writes no qwen settings file, so Qwen Code falls back to its own default window
regardless of the agent's configuration.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from omnigent.harnesses.qwen_native import main as qn_main
from omnigent.harnesses.qwen_native.bridge import bridge_dir_for_session_id
from omnigent.runner.native import orchestration as orch

# 2**18; distinctive enough that an incidental match in a written file is unlikely.
_CONTEXT_WINDOW = 262144
_SESSION_ID = "conv_ctx_window_launch"


class _CapturedLaunch(Exception):
    """Stops the launch right after the terminal spec is built, for inspection."""


class _FakeRegistry:
    """Captures the ``TerminalEnvSpec`` the auto-create path hands to the terminal."""

    def __init__(self) -> None:
        self.spec: object | None = None

    async def launch_required_terminal(self, *, spec: object, **_kw: object) -> object:
        self.spec = spec
        raise _CapturedLaunch()


def _launch_surface(spec: object, *roots: Path) -> str:
    """Every string Qwen Code could read from this launch: argv, env, written files."""
    parts = [
        json.dumps(list(getattr(spec, "args", None) or [])),
        json.dumps(dict(getattr(spec, "env", None) or {})),
        str(getattr(spec, "command", "")),
    ]
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    parts.append(path.read_text(errors="replace"))
                except OSError:
                    continue
    return "\n".join(parts)


def test_qwen_native_launch_conveys_configured_context_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The configured context window must appear on qwen's launch surface.

    The agent's ``context_window`` is delivered to the runner in the session's
    native-launch snapshot (the only runner-facing channel for it). The launch
    must then forward it to Qwen Code via args, env, or a written settings file.
    """
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    for path in (workspace, home):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    monkeypatch.setattr(qn_main, "resolve_qwen_executable", lambda *a, **k: "/bin/qwen")

    async def _fake_snapshot(**_kwargs: object) -> dict[str, object]:
        return {
            "workspace": str(workspace),
            "context_window": _CONTEXT_WINDOW,
            "effective_context_window": _CONTEXT_WINDOW,
        }

    monkeypatch.setattr(orch, "_fetch_native_launch_snapshot", _fake_snapshot)

    registry = _FakeRegistry()

    async def _run() -> None:
        with pytest.raises(_CapturedLaunch):
            await orch._auto_create_qwen_terminal(
                _SESSION_ID,
                registry,
                lambda *a, **k: None,
                server_client=None,
                ensure_comment_relay=None,
            )

    asyncio.run(_run())

    assert registry.spec is not None, "qwen terminal launch was never attempted"
    bridge_dir = bridge_dir_for_session_id(_SESSION_ID)
    surface = _launch_surface(registry.spec, bridge_dir, workspace, home)
    assert str(_CONTEXT_WINDOW) in surface, (
        f"the agent's configured context_window ({_CONTEXT_WINDOW}) never reached "
        "Qwen Code's launch surface: it is absent from the CLI args, the terminal "
        "env, and every session config file Omnigent wrote. Qwen Code therefore "
        "uses its own default context window instead of the configured value.\n"
        f"args={list(getattr(registry.spec, 'args', []) or [])}\n"
        f"env={dict(getattr(registry.spec, 'env', None) or {})}"
    )
