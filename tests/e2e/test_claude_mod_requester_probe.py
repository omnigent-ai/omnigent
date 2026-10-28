"""Real native CLI/mod contract probe, with a local scripted provider and no grants."""

from __future__ import annotations

import json
import os
import sys
from contextlib import suppress
from pathlib import Path

import httpx
import pexpect
import pytest

from dev.claude_requester_probe.analyze import summarize
from omnigent.harnesses.claude_native.execution import ClaudeExecutionIdentity
from tests._helpers.terminal_screen import TerminalScreen
from tests.e2e.conftest import (
    configure_mock_llm,
    get_mock_message_requests,
    set_fallback_mock_llm,
)
from tests.e2e.test_private_session_orchestration import _CLAUDE_ISOLATION_REASON

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(_CLAUDE_ISOLATION_REASON is not None, reason=_CLAUDE_ISOLATION_REASON or "")
@pytest.mark.timeout(120)
@pytest.mark.parametrize("queue_only", [False, True], ids=["enter", "queue-submit"])
def test_native_mod_captures_typed_input_and_tool_binding(
    tmp_path: Path, isolated_mock_llm_server_url: str, queue_only: bool
) -> None:
    """Type during a held provider request; observe IDs without inferring consumption."""
    mock = isolated_mock_llm_server_url
    config = tmp_path / "config"
    evidence = tmp_path / "events"
    workspace = tmp_path / "workspace"
    for directory in (config, evidence, workspace):
        directory.mkdir()
    (config / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "customApiKeyResponses": {"approved": ["local-test-key"]},
                "projects": {str(workspace): {"hasTrustDialogAccepted": True}},
            }
        )
    )
    settings = config / "settings.json"
    settings.write_text(json.dumps({"skipDangerousModePermissionPrompt": True}))
    receipt = tmp_path / "mcp-receipt.json"
    mcp_script = tmp_path / "mcp_server.py"
    mcp_script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from mcp.server.fastmcp import Context, FastMCP\n"
        "server = FastMCP('probe')\n"
        "@server.tool()\n"
        "def echo(ctx: Context) -> str:\n"
        "    meta = ctx.request_context.meta\n"
        "    Path(sys.argv[1]).write_text(meta.model_dump_json() if meta else '{}')\n"
        "    return 'probe-mcp-ran'\n"
        "server.run()\n"
    )
    mcp_config = tmp_path / "mcp.json"
    mcp_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "probe": {
                        "command": sys.executable,
                        "args": [str(mcp_script), str(receipt)],
                    }
                }
            }
        )
    )
    configure_mock_llm(
        mock,
        [
            {
                "block": True,
                "tool_calls": [
                    {
                        "call_id": "toolu_probe_first",
                        "name": "Bash",
                        "arguments": json.dumps({"command": "printf probe-tool-ran"}),
                    },
                    {"call_id": "toolu_probe_mcp", "name": "mcp__probe__echo", "arguments": "{}"},
                ],
            },
            {"text": "probe-turn-completed"},
        ],
        required_tools=["Bash", "mcp__probe__echo"],
    )
    set_fallback_mock_llm(mock, key="default", text="probe-turn-completed")
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "TMPDIR"}}
    env.update(
        {
            "TERM": "xterm-256color",
            "CLAUDE_CONFIG_DIR": str(config),
            "ANTHROPIC_BASE_URL": mock,
            "ANTHROPIC_API_KEY": "local-test-key",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "OMNIGENT_CLAUDE_PROBE_DIR": str(evidence),
            "ENABLE_TOOL_SEARCH": "false",
        }
    )
    child = pexpect.spawn(
        "claude",
        [
            "--plugin-dir",
            str(ROOT / "dev/claude_requester_probe"),
            "--settings",
            str(settings),
            "--dangerously-skip-permissions",
            "--model",
            "claude-sonnet-4-20250514",
            "--debug-file",
            str(tmp_path / "claude-debug.log"),
            "--mcp-config",
            str(mcp_config),
            "--strict-mcp-config",
        ],
        cwd=str(workspace),
        env=env,
        encoding="utf-8",
        dimensions=(40, 120),
        timeout=30,
    )
    screen = TerminalScreen(child)

    def events() -> list[dict]:
        records = []
        for path in evidence.glob("*.json"):
            # A writer can still be finishing this individual record.
            with suppress(json.JSONDecodeError):
                records.append(json.loads(path.read_text()))
        return records

    try:
        screen.wait_for(lambda text: "❯" in text, timeout=30, what="native prompt")
        screen.wait_for(
            lambda _: (
                'MCP server "probe": Successfully connected'
                in (tmp_path / "claude-debug.log").read_text()
            ),
            timeout=30,
            what="MCP fixture connection",
        )
        child.send("PROBE_FIRST: run the requested harmless tool.")
        child.send("\r")
        with httpx.Client(base_url=mock, timeout=5, trust_env=False) as client:
            screen.wait_for(
                lambda _: client.get("/gate/pending").json()["pending"],
                timeout=30,
                what="held model request",
            )
            child.send("PROBE_STEERING: add this instruction while working.")
            child.send("\x18\r" if queue_only else "\r")
            screen.wait_for(
                lambda _: len([e for e in events() if e["kind"] == "prompt.submit"]) >= 2,
                timeout=15,
                what="second native prompt submission",
            )
            client.post("/gate/release").raise_for_status()
        screen.wait_for(
            lambda _: any(e["kind"] == "turn.complete" for e in events()),
            timeout=30,
            what="native turn completion",
        )
        report = summarize(events())
        (tmp_path / "report.json").write_text(json.dumps(report, indent=2))
        assert any(c["tool_use_id"] == "toolu_probe_first" for c in report["calls"]), report
        assert all(c["correlation"] == "observed" for c in report["calls"]), report
        identity = ClaudeExecutionIdentity.from_mcp_meta(json.loads(receipt.read_text()))
        assert identity is not None and identity.tool_use_id == "toolu_probe_mcp"
        assert any(c["tool_use_id"] == identity.tool_use_id for c in report["calls"]), report
        assert report["authorization"] == "not_established"
        assert report["submissions"][0]["active_turn_id"] is None
        assert any(s["active_turn_id"] for s in report["submissions"][1:]), report
        assert report["submissions"][1]["wait"] is queue_only, report
        screen.wait_for(
            lambda _: any(
                "PROBE_STEERING" in json.dumps(request["body"].get("messages"))
                for request in get_mock_message_requests(mock)
            ),
            timeout=30,
            what="queued input reaching the provider",
        )
    finally:
        (tmp_path / "terminal.txt").write_text(screen.text())
        child.close(force=True)
