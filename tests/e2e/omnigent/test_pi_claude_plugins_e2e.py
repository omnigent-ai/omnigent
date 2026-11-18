"""Real Pi sessions load enabled Claude Code plugins and skip disabled ones."""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.conftest import get_mock_requests
from tests.e2e.omnigent._pi_mock_gateway import write_pi_gateway_config
from tests.e2e.omnigent.conftest import configure_mock_llm, reset_mock_llm

_RUN_TIMEOUT_SEC = 180
_SKILL_MARKER = "CLAUDE_PLUGIN_SKILL_MARKER_4D1"
_EXTENSION_MARKER = "CLAUDE_PLUGIN_PI_EXTENSION_MARKER_8A2"
_DISABLED_MARKER = "CLAUDE_PLUGIN_DISABLED_MARKER_0C9"

_pytest_pi_unavailable = cli_unavailable_reason("pi")
pytestmark = pytest.mark.skipif(
    _pytest_pi_unavailable is not None,
    reason=(
        "pi Claude-plugin e2e requires a runnable 'pi' CLI; "
        f"{_pytest_pi_unavailable}. Install/fix Pi to run this test."
    ),
)


def _write_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(content, encoding="utf-8")


def _seed_claude_home(home: Path) -> Path:
    """Install two enabled marketplace plugins and one disabled; return the review plugin."""
    claude = home / ".claude"
    cache = claude / "plugins" / "cache" / "market"
    review = cache / "dp" / "1.0.0"
    ext = cache / "ext" / "1.0.0"
    off = cache / "off" / "1.0.0"
    _write_files(
        review,
        {
            "skills/probe/SKILL.md": (
                f"---\nname: probe\ndescription: {_SKILL_MARKER} routing\n---\nProbe.\n"
            ),
            "commands/review.md": (
                "---\ndescription: Review\nargument-hint: '[focus]'\n"
                "allowed-tools: Bash(git:*)\n---\n"
                "Review focus: $ARGUMENTS\nPlugin root: ${CLAUDE_PLUGIN_ROOT}\n"
            ),
        },
    )
    _write_files(
        ext,
        {
            "package.json": json.dumps({"name": "ext", "pi": {"extensions": ["./marker.js"]}}),
            "marker.js": (
                "module.exports = pi => pi.on('before_agent_start', event => ({"
                f"systemPrompt: event.systemPrompt + '\\n{_EXTENSION_MARKER}'}}));\n"
            ),
        },
    )
    _write_files(
        off,
        {"skills/off/SKILL.md": f"---\nname: off\ndescription: {_DISABLED_MARKER}\n---\nOff.\n"},
    )
    installed = {
        "plugins": {
            "dp@market": [{"installPath": str(review)}],
            "ext@market": [{"installPath": str(ext)}],
            "off@market": [{"installPath": str(off)}],
        }
    }
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(installed))
    enabled = {"dp@market": True, "ext@market": True, "off@market": False}
    (claude / "settings.json").write_text(json.dumps({"enabledPlugins": enabled}))
    return review.resolve()


def test_pi_gateway_loads_enabled_claude_plugins(
    omnigent_repo_root: Path,
    omnigent_python: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """Skills, a namespaced command and a Pi extension load; a disabled plugin does not."""
    model = f"mock-claude-plugins-{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url, [{"text": "First reply."}, {"text": "Second reply."}], key=model
    )
    fake_home = tmp_path / "home"
    review_root = _seed_claude_home(fake_home)
    config_home = tmp_path / "omnigent-config"
    write_pi_gateway_config(config_home, mock_url=mock_llm_server_url, model=model)
    env = dict(mock_credentials_env)
    env.update(
        HOME=str(fake_home),
        OMNIGENT_CONFIG_HOME=str(config_home),
        OMNIGENT_DATA_HOME=str(tmp_path / "data"),
        OMNIGENT_FEATURES="pi_claude_plugins",
        PI_OFFLINE="1",
    )
    env.pop("PI_CODING_AGENT_DIR", None)
    env.pop("CLAUDE_CONFIG_DIR", None)
    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"

    def run_turn(prompt: str) -> list[dict]:
        command = [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--model",
            model,
            "--harness",
            "pi",
            "-p",
            prompt,
            "--no-log",
            "--no-session",
        ]
        result = subprocess.run(
            command,
            env=env,
            cwd=fake_home,
            capture_output=True,
            text=True,
            timeout=_RUN_TIMEOUT_SEC,
        )
        assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        return get_mock_requests(mock_llm_server_url, key=model)

    turn = run_turn("/dp:review security please")

    for request in turn:
        system = "\n".join(
            json.dumps(message["content"])
            for message in request["messages"]
            if message["role"] in ("system", "developer")
        )
        assert _EXTENSION_MARKER in system
        assert _DISABLED_MARKER not in system
        # Two listings in one request would mean the plugin skill loaded twice.
        assert system.count(_SKILL_MARKER) == 1

    user_texts = [
        json.dumps(message["content"])
        for request in turn
        for message in request["messages"]
        if message["role"] == "user"
    ]
    assert any("Review focus: security please" in text for text in user_texts)
    assert any(f"Plugin root: {review_root}" in text for text in user_texts)
    assert not any("${CLAUDE_PLUGIN_ROOT}" in text for text in user_texts)
