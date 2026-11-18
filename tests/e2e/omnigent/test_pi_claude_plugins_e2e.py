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

_SKILL_MARKER = "CLAUDE_PLUGIN_SKILL_MARKER_4D1"
_EXTENSION_MARKER = "CLAUDE_PLUGIN_PI_EXTENSION_MARKER_8A2"
_DISABLED_MARKER = "CLAUDE_PLUGIN_DISABLED_MARKER_0C9"

_pytest_pi_unavailable = cli_unavailable_reason("pi")
pytestmark = pytest.mark.skipif(
    _pytest_pi_unavailable is not None,
    reason=f"pi Claude-plugin e2e requires a runnable 'pi' CLI: {_pytest_pi_unavailable}",
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _seed_claude_home(home: Path) -> Path:
    """Install two enabled marketplace plugins and one disabled; return the review plugin."""
    claude = home / ".claude"
    cache = claude / "plugins" / "cache" / "market"
    review, ext, off = (cache / name / "1.0.0" for name in ("dp", "ext", "off"))
    _write(
        review / "skills" / "probe" / "SKILL.md",
        f"---\nname: probe\ndescription: {_SKILL_MARKER} routing\n---\nProbe.\n",
    )
    _write(
        review / "commands" / "review.md",
        "---\ndescription: Review\nallowed-tools: Bash(git:*)\n---\n"
        "Review focus: $ARGUMENTS\nPlugin root: ${CLAUDE_PLUGIN_ROOT}\n",
    )
    _write(ext / "package.json", json.dumps({"name": "ext", "pi": {"extensions": ["./m.js"]}}))
    _write(
        ext / "m.js",
        "module.exports = pi => pi.on('before_agent_start', e => ({"
        f"systemPrompt: e.systemPrompt + '\\n{_EXTENSION_MARKER}'}}));\n",
    )
    _write(
        off / "skills" / "off" / "SKILL.md",
        f"---\nname: off\ndescription: {_DISABLED_MARKER}\n---\nOff.\n",
    )
    installed = {
        "plugins": {
            "dp@market": [{"installPath": str(review)}],
            "ext@market": [{"installPath": str(ext)}],
            "off@market": [{"installPath": str(off)}],
        }
    }
    _write(claude / "plugins" / "installed_plugins.json", json.dumps(installed))
    enabled = {"dp@market": True, "ext@market": True, "off@market": False}
    _write(claude / "settings.json", json.dumps({"enabledPlugins": enabled}))
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
    configure_mock_llm(mock_llm_server_url, [{"text": "Reviewed."}], key=model)
    fake_home = tmp_path / "home"
    review_root = _seed_claude_home(fake_home)
    config_home = tmp_path / "omnigent-config"
    write_pi_gateway_config(config_home, mock_url=mock_llm_server_url, model=model)
    env = {
        **mock_credentials_env,
        "HOME": str(fake_home),
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_DATA_HOME": str(tmp_path / "data"),
        "OMNIGENT_FEATURES": "pi_claude_plugins",
        "PI_OFFLINE": "1",
    }
    env.pop("PI_CODING_AGENT_DIR", None)
    env.pop("CLAUDE_CONFIG_DIR", None)
    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"
    command = [
        str(omnigent_python), "-m", "omnigent", "run", str(yaml_path),
        "--model", model, "--harness", "pi", "-p", "/dp:review security please",
        "--no-log", "--no-session",
    ]  # fmt: skip
    result = subprocess.run(
        command, env=env, cwd=fake_home, capture_output=True, text=True, timeout=180
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    requests = get_mock_requests(mock_llm_server_url, key=model)
    assert requests
    for request in requests:
        system = json.dumps(
            [m["content"] for m in request["messages"] if m["role"] in ("system", "developer")]
        )
        assert _EXTENSION_MARKER in system
        assert _DISABLED_MARKER not in system
        # Two listings in one request would mean the plugin skill loaded twice.
        assert system.count(_SKILL_MARKER) == 1
    user_texts = json.dumps([m["content"] for m in requests[0]["messages"] if m["role"] == "user"])
    assert "Review focus: security please" in user_texts
    assert f"Plugin root: {review_root}" in user_texts
    assert "${CLAUDE_PLUGIN_ROOT}" not in user_texts
