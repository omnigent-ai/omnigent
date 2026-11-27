"""Real Pi sessions load enabled Claude Code plugins and skip disabled ones."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
import yaml

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
    """Install enabled and disabled plugins through a shared cache; return the review root."""
    claude = home / ".claude"
    shared = home / "shared-plugin-cache"
    shared.mkdir(parents=True)
    (claude / "plugins").mkdir(parents=True)
    (claude / "plugins" / "cache").symlink_to(shared, target_is_directory=True)
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


@pytest.mark.parametrize(
    "mode",
    [
        "sdk",
        pytest.param(
            "native",
            marks=pytest.mark.skipif(os.name == "nt", reason="native PTY capture needs POSIX"),
        ),
    ],
)
def test_pi_gateway_loads_enabled_claude_plugins(
    omnigent_repo_root: Path,
    omnigent_python: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Shared-cache skills, commands and a Pi extension load; a disabled plugin does not."""
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
    if mode == "sdk":
        command = [
            str(omnigent_python), "-m", "omnigent", "run", str(yaml_path),
            "--model", model, "--harness", "pi", "-p", "/dp:review security please",
            "--no-log", "--no-session",
        ]  # fmt: skip
        result = subprocess.run(
            command, env=env, cwd=fake_home, capture_output=True, text=True, timeout=180
        )
        assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    else:
        import pexpect

        from omnigent.harnesses.pi_native.credentials import (
            pi_native_provider_launch,
            resolve_pi_native_provider,
        )
        from tests._helpers.terminal_screen import TerminalScreen

        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fake_home / ".claude"))
        monkeypatch.setenv("OMNIGENT_FEATURES", "pi_claude_plugins")
        provider = resolve_pi_native_provider(
            model=model,
            config_loader=lambda: yaml.safe_load((config_home / "config.yaml").read_text()),
        )
        assert provider is not None
        launch = pi_native_provider_launch(
            tmp_path / "native-agent", provider, global_agent_dir=fake_home / ".pi" / "agent"
        )
        executable = shutil.which("pi")
        assert executable is not None
        with pexpect.spawn(
            executable,
            ["--no-session", *launch.args],
            cwd=str(fake_home),
            env={**env, "TERM": "xterm-256color", **launch.env},
            encoding="utf-8",
            dimensions=(42, 120),
        ) as child:
            screen = TerminalScreen(child)
            screen.wait_for(
                lambda text: "ctrl+c/ctrl+d" in text, timeout=30, what="the native Pi prompt"
            )
            child.send("/skill:")
            skills = screen.wait_for(
                lambda text: _SKILL_MARKER in text,
                timeout=20,
                what="the shared-cache skill completion",
            )
            child.sendcontrol("c")
            screen.wait_for(
                lambda text: _SKILL_MARKER not in text,
                timeout=10,
                what="the completion menu to close",
            )
            child.send("/dp:review security please\r")
            expanded_command = screen.wait_for(
                lambda text: "Reviewed." in text and "Review focus: security please" in text,
                timeout=60,
                what="the expanded plugin command and mock reply",
            )
        print(
            "PI_SHARED_CACHE_NATIVE_DEMO "
            + json.dumps(
                {
                    "provider": "scripted mock OpenAI-compatible provider",
                    "screens": {"skill-completion": skills, "expanded-command": expanded_command},
                }
            )
        )

    requests = get_mock_requests(mock_llm_server_url, key=model)
    assert requests
    for request in requests:
        messages = request.get("messages", request.get("input", []))
        assert isinstance(messages, list)
        system = json.dumps(
            [
                request.get("instructions"),
                *[m["content"] for m in messages if m.get("role") in ("system", "developer")],
            ]
        )
        assert _EXTENSION_MARKER in system
        assert _DISABLED_MARKER not in system
        # Two listings in one request would mean the plugin skill loaded twice.
        assert system.count(_SKILL_MARKER) == 1
    messages = requests[0].get("messages", requests[0].get("input", []))
    user_texts = json.dumps([m["content"] for m in messages if m.get("role") == "user"])
    assert "Review focus: security please" in user_texts
    assert f"Plugin root: {review_root}" in user_texts
    assert "${CLAUDE_PLUGIN_ROOT}" not in user_texts
