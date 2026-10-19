"""Exercise managed model selection in the real Pi TUI without inference.

Run with ``pytest tests/e2e/test_pi_native_model_scope_e2e.py -v``. Pi reads
only temporary configuration, uses synthetic keys, and runs in offline mode.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pexpect
import pyte
import pytest

from omnigent.harnesses.pi_native.credentials import (
    pi_native_provider_launch,
    resolve_pi_native_provider,
)
from omnigent.onboarding.providers import ModelInfo
from tests.e2e.conftest import configure_mock_llm, get_mock_requests, reset_mock_llm

pytestmark = [
    pytest.mark.skipif(shutil.which("pi") is None, reason="requires the Pi CLI"),
    pytest.mark.timeout(60),
]


class _PiTui:
    def __init__(self, process: pexpect.spawn) -> None:
        self.process = process
        self.screen = pyte.Screen(110, 34)
        self.stream = pyte.Stream(self.screen)

    def wait_for(self, predicate: Callable[[str], bool]) -> str:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                self.stream.feed(self.process.read_nonblocking(65536, timeout=0.2))
            except pexpect.TIMEOUT:
                pass
            except pexpect.EOF:
                pytest.fail("Pi exited before the expected screen:\n" + self.text())
            rendered = self.text()
            if predicate(rendered):
                return rendered
        pytest.fail("Pi did not display the expected screen:\n" + self.text())

    def text(self) -> str:
        return "\n".join(self.screen.display)

    def send(self, text: str) -> None:
        self.process.send(text)


@contextlib.contextmanager
def _managed_pi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    models: dict[str, str],
    enabled_models: list[str] | None = None,
    base_url: str = "http://127.0.0.1:9/v1",
    wire_api: str = "chat",
    reasoning_effort: str | None = None,
) -> Iterator[_PiTui]:
    global_dir = tmp_path / "global-agent"
    global_dir.mkdir()
    settings = {"enabledModels": enabled_models} if enabled_models is not None else {}
    settings_path = global_dir / "settings.json"
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setattr("omnigent.inner.pi_settings.DEFAULT_PI_AGENT_DIR", global_dir)
    config = {
        "providers": {
            "demo": {
                "kind": "gateway",
                "default": True,
                "anthropic" if wire_api == "anthropic" else "openai": {
                    "base_url": base_url,
                    "api_key": "synthetic-model-scope-key",
                    **({"wire_api": wire_api} if wire_api != "anthropic" else {}),
                    "models": models,
                },
            }
        }
    }
    provider = resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None
    launch = pi_native_provider_launch(
        tmp_path / "managed-agent", provider, reasoning_effort=reasoning_effort
    )
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "TERM": "xterm-256color",
        "LANG": "en_US.UTF-8",
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
        "ANTHROPIC_API_KEY": "synthetic-built-in-key",
        **launch.env,
    }
    executable = shutil.which("pi")
    assert executable is not None
    process = pexpect.spawn(
        executable,
        [
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            *launch.args,
        ],
        cwd=str(tmp_path),
        env=env,
        encoding="utf-8",
        dimensions=(34, 110),
    )
    try:
        tui = _PiTui(process)
        tui.wait_for(lambda text: "demo-fast" in text and "ctrl+c/ctrl+d" in text)
        yield tui
    finally:
        process.close(force=True)
    assert json.loads(settings_path.read_text(encoding="utf-8")) == settings


def test_managed_shortlist_can_select_another_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _managed_pi(
        tmp_path,
        monkeypatch,
        models={"default": "demo-fast", "balanced": "demo-balanced", "quality": "demo-quality"},
    ) as tui:
        tui.send("/model\r")
        picker = tui.wait_for(lambda text: "demo-quality [omnigent]" in text)
        assert "demo-fast [omnigent]" in picker
        assert "demo-balanced [omnigent]" in picker
        assert "[anthropic]" not in picker

        tui.send("\x1b[B\r")
        tui.wait_for(lambda text: "Scope:" not in text and "demo-balanced" in text)
        tui.send("/model\r")
        tui.wait_for(
            lambda text: any(
                "demo-balanced [omnigent]" in line and "✓" in line for line in text.splitlines()
            )
        )


def test_default_only_provider_preserves_existing_pi_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _managed_pi(
        tmp_path,
        monkeypatch,
        models={"default": "demo-fast"},
        enabled_models=["omnigent/demo-fast", "anthropic/claude-sonnet-4-5"],
    ) as tui:
        tui.send("/model\r")
        picker = tui.wait_for(lambda text: "claude-sonnet-4-5 [anthropic]" in text)
        assert "demo-fast [omnigent]" in picker


@pytest.mark.parametrize("wire_api", ["chat", "responses", "anthropic"])
def test_generic_provider_delivers_effort_through_native_tool_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_llm_server_url: str, wire_api: str
) -> None:
    """Generated model capability and selected effort reach the actual API."""
    model = f"demo-fast-{wire_api}"
    monkeypatch.setattr(
        "omnigent.onboarding.providers.find_catalog_models",
        lambda _: [ModelInfo(name=model, provider="openai", supports_reasoning=True)],
    )
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "effort-tool",
                        "name": "bash",
                        "arguments": '{"command":"printf PI_EFFORT_TOOL_RESULT"}',
                    }
                ]
            },
            {"text": "NATIVE_EFFORT_FIRST_ANSWER", "stream": True},
            {"text": "NATIVE_EFFORT_SECOND_ANSWER", "stream": True},
        ],
        key=model,
    )
    with _managed_pi(
        tmp_path,
        monkeypatch,
        models={"default": model},
        base_url=mock_llm_server_url if wire_api == "anthropic" else f"{mock_llm_server_url}/v1",
        wire_api=wire_api,
        reasoning_effort="high",
    ) as tui:
        tui.send("Run the tool, then answer.\r")
        tui.wait_for(lambda text: "NATIVE_EFFORT_FIRST_ANSWER" in text)
        tui.send("Reply again.\r")
        tui.wait_for(lambda text: "NATIVE_EFFORT_SECOND_ANSWER" in text)
    requests = get_mock_requests(mock_llm_server_url, key=model)
    assert len(requests) == 3
    efforts = [
        r.get("reasoning_effort")
        if wire_api == "chat"
        else r.get("output_config" if wire_api == "anthropic" else "reasoning", {}).get("effort")
        for r in requests
    ]
    assert efforts == ["high"] * 3
    history = json.dumps(requests[-1].get("messages", requests[-1].get("input")))
    assert "PI_EFFORT_TOOL_RESULT" in history and "NATIVE_EFFORT_FIRST_ANSWER" in history
