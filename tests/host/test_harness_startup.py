"""Launch precedence, env-wrapper settings, and host defaults."""

import os
import subprocess

import pytest

from omnigent.host import harness_startup as startup

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires /usr/bin/env and sh")


@pytest.fixture
def config(monkeypatch, tmp_path):
    cfg = {"harness": {}}
    monkeypatch.setattr(startup, "load_global_config", lambda: cfg)
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", dict)
    monkeypatch.setattr("omnigent._platform._cli_fallback_dirs", lambda: [tmp_path])
    for key in ("OMNIGENT_CLAUDE_PATH", "OMNIGENT_CODEX_PATH", "OMNIGENT_RUNNER_ENV_PASSTHROUGH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    tool = tmp_path / "tool"
    tool.write_text('#!/bin/sh\nprintf "%s" "$0"\n')
    tool.chmod(0o755)
    return cfg


@pytest.mark.parametrize(
    "harness,command,override,passthrough,expected,source",
    [
        ("claude-native", None, None, False, "claude", "default"),
        ("codex-native", None, None, False, "codex", "default"),
        ("claude-native", "tool", "missing", False, "tool", "config"),
        ("claude-native", "tool", "missing", True, "missing", "env"),
        ("codex-native", "tool", "other", False, "tool", "config"),
        ("codex-native", None, "tool", False, "tool", "env"),
        ("codex-native", None, "missing", False, "codex", "default"),
    ],
)
def test_launch_precedence(
    config, monkeypatch, tmp_path, harness, command, override, passthrough, expected, source
):
    entry = {"args": ["--system-prompt", "--SECRET", '{"apiKey":"SECRET"}', "-pSECRET"]}
    if command:
        entry["command"] = command
    config["harness"][harness] = entry
    var = f"OMNIGENT_{harness.removesuffix('-native').upper()}_PATH"
    if override:
        monkeypatch.setenv(var, override)
    if passthrough:
        monkeypatch.setenv("OMNIGENT_RUNNER_ENV_PASSTHROUGH", var)
    result = startup.describe_harness_startup(harness)
    assert result.model_dump() == {
        "command": expected,
        "command_source": source,
        "resolved_path": str(tmp_path / "tool") if expected == "tool" else None,
        "arg_count": 4,
        "args": ["--system-prompt", "--SECRET", '{"apiKey":"SECRET"}', "-pSECRET"],
        "configured_command": command,
        "configured_args": entry["args"],
        "environment": {"inherit": True, "variables": {}, "unset": []},
    }


@pytest.mark.parametrize(
    "prefix,found",
    [
        ([], True),
        (["-i"], False),
        (["--ignore-environment"], False),
        (["-u", "PATH"], False),
        (["--unset=PATH"], False),
        (["--unset", "PATH"], False),
        (["-iuPATH"], False),
        (["PATH="], False),
        (["-i", "PATH={path}"], True),
        (["--", "PATH={path}", "TOKEN=SECRET"], True),
        (["NOT-A-SHELL-NAME=SECRET"], True),
    ],
)
def test_env_resolution_matches_real_env(config, tmp_path, prefix, found):
    args = [arg.format(path=tmp_path) for arg in prefix] + ["tool", "--SECRET"]
    config["harness"]["claude-native"] = {"command": "/usr/bin/env", "args": args}
    result = startup.describe_harness_startup("claude-native")
    process = subprocess.run(
        ["/usr/bin/env", *args], env={"PATH": str(tmp_path)}, capture_output=True, text=True
    )
    assert (process.returncode == 0) == found
    assert result.resolved_path == (process.stdout if found else None)
    assert result.command == "tool" and result.arg_count == 1
    assert result.args == ["--SECRET"]
    assert result.configured_command == "/usr/bin/env"
    assert result.configured_args == args
    assert result.environment is not None
    assert result.environment.inherit == (
        not any(arg in ("-i", "--ignore-environment", "-iuPATH") for arg in prefix)
    )


@pytest.mark.parametrize(
    "args",
    [
        ["-S", "tool --api-key SECRET"],
        ["TOKEN=SECRET"],
        ["-u"],
        ["=SECRET", "tool"],
        ["-", "tool", "--SECRET"],
        ["-u", "PATH", "-", "tool", "--SECRET"],
    ],
)
def test_unsupported_env_syntax_retains_the_configured_invocation(config, args):
    config["harness"]["codex-native"] = {"command": "/usr/bin/env", "args": args}
    result = startup.describe_harness_startup("codex-native")
    assert result.command == "/usr/bin/env" and result.arg_count == len(args)
    assert result.args == args
    assert result.configured_args == args
    assert result.environment is None


@pytest.mark.parametrize("harness", ["claude-native", "codex-native"])
@pytest.mark.parametrize("command", ["env", "/usr/bin/env"])
def test_env_settings_preserve_values_and_separate_launch_arguments(config, harness, command):
    args = [
        "-i",
        "-u",
        "REMOVED",
        "TOKEN=old",
        "TOKEN=SECRET=with spaces",
        "EMPTY=",
        "tool",
        "codex",
        "--",
        "",
    ]
    config["harness"][harness] = {"command": command, "args": args}
    result = startup.describe_harness_startup(harness)
    assert result.command == "tool"
    assert result.args == ["codex", "--", ""]
    assert result.arg_count == 3
    assert result.configured_command == command
    assert result.configured_args == args
    assert result.environment is not None
    assert result.environment.model_dump() == {
        "inherit": False,
        "variables": {"TOKEN": "SECRET=with spaces", "EMPTY": ""},
        "unset": ["REMOVED"],
    }
    assert "SECRET=with spaces" in result.model_dump_json()


def test_no_config_reports_defaults_without_exporting_inherited_values(config, monkeypatch):
    monkeypatch.setenv("INHERITED_TOKEN", "unrelated-private-value")
    monkeypatch.setenv("OMNIGENT_RUNNER_ENV_PASSTHROUGH", "INHERITED_TOKEN")
    result = startup.describe_harness_startup("claude-native")
    assert result.command == "claude"
    assert result.configured_command is None
    assert result.configured_args == result.args == []
    assert result.environment is not None
    assert result.environment.model_dump() == {"inherit": True, "variables": {}, "unset": []}
    assert "unrelated-private-value" not in result.model_dump_json()


@pytest.mark.parametrize(
    "harness", ["pi-native", "antigravity-native", "opencode-native", "unknown"]
)
def test_unsupported_harness(config, harness):
    with pytest.raises(ValueError):
        startup.describe_harness_startup(harness)


def test_older_host_payload_without_argument_values_is_accepted():
    result = startup.HarnessStartup.model_validate(
        {
            "command": "claude",
            "resolved_path": None,
            "command_source": "default",
            "arg_count": 2,
        }
    )
    assert result.args is None
    assert result.configured_args is None
    assert result.environment is None
