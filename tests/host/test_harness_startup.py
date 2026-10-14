"""Launch precedence, env-wrapper settings, and host defaults."""

import functools
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


@functools.cache
def _real_env_supports_chdir() -> bool:
    """Return whether ``/usr/bin/env`` accepts ``-C`` and ``--chdir``."""
    for args in (["-C", "/", "/usr/bin/env"], ["--chdir=/", "/usr/bin/env"]):
        probe = subprocess.run(["/usr/bin/env", *args], env={}, capture_output=True, check=False)
        if probe.returncode != 0:
            return False
    return True


@functools.cache
def _real_env_supports_long_split_string() -> bool:
    """Return whether ``/usr/bin/env`` accepts GNU's ``--split-string`` spelling."""
    probe = subprocess.run(
        ["/usr/bin/env", "--split-string=/usr/bin/env"], env={}, capture_output=True, check=False
    )
    return probe.returncode == 0


# The rows print with printenv because a wrapper that runs another env is not modeled.
@pytest.mark.parametrize(
    "args",
    [
        ["A=1", "/usr/bin/printenv"],
        ["-i", "A=1", "/usr/bin/printenv"],
        ["-", "A=1", "/usr/bin/printenv"],
        ["-u", "KEEP", "A=1", "/usr/bin/printenv"],
        ["-u", "KEEP", "-", "A=1", "/usr/bin/printenv"],
        ["-iuKEEP", "A=1", "/usr/bin/printenv"],
        ["-S", "A=1 /usr/bin/printenv"],
        ["-S", "- A='x y' /usr/bin/printenv"],
        ["-S", "-u KEEP A=1 /usr/bin/printenv"],
        ["-iS", "A=1 /usr/bin/printenv"],
        ["--split-string=-u KEEP A=1 /usr/bin/printenv"],
        ["--split-string", "-u KEEP A=1 /usr/bin/printenv"],
        ["-C", "/", "A=1", "/usr/bin/printenv"],
        ["-S", "A=${KEEP}/c /usr/bin/printenv"],
        ["-S", 'A="${KEEP} x" /usr/bin/printenv'],
        ["-S", "A='${KEEP}' /usr/bin/printenv"],
        ["-S", "A=${UNSET_NAME}z /usr/bin/printenv"],
        ["-S", "${UNSET_NAME} A=1 /usr/bin/printenv"],
        ["-S", "A=1 ${UNSET_NAME}${UNSET_NAME} B=2 /usr/bin/printenv"],
        ["-S", "A=x\tB=y\nC=z /usr/bin/printenv"],
        ["-S", "A=/tmp/a\u00a0b /usr/bin/printenv"],
        ["-S", "A=x\u00a0#y /usr/bin/printenv"],
        ["-S", "A=x\x1cB=y /usr/bin/printenv"],
    ],
)
def test_env_wrapper_environment_matches_real_env(args):
    """The modeled wrapper environment equals what the real ``env`` passes on."""
    if args[0].startswith("--split-string") and not _real_env_supports_long_split_string():
        pytest.skip("/usr/bin/env lacks --split-string")
    if args[0] == "-C" and not _real_env_supports_chdir():
        pytest.skip("/usr/bin/env lacks -C")
    base = {"PATH": "/usr/bin:/bin", "KEEP": "kept", "A": "old"}
    wrapper = startup.env_wrapper_environment("/usr/bin/env", args, base)
    assert wrapper is not None
    expected = dict(base) if wrapper.inherit else {}
    for name in wrapper.unset:
        expected.pop(name, None)
    expected.update(wrapper.variables)
    process = subprocess.run(
        ["/usr/bin/env", *args], env=base, capture_output=True, text=True, check=True
    )
    # ``splitlines`` would also split on separators such as U+001C inside values.
    lines = process.stdout.removesuffix("\n").split("\n")
    assert dict(line.split("=", 1) for line in lines) == expected


def test_standalone_dash_ends_env_option_parsing():
    """
    Arguments after ``env -`` are not options, as in GNU ``env``.

    ``env - -u KEEP claude`` runs ``-u`` as the command, so it neither unsets
    ``KEEP`` nor changes directory.
    """
    environment = startup.env_wrapper_environment("/usr/bin/env", ["-", "-u", "KEEP", "claude"])
    assert environment is not None
    assert (environment.inherit, environment.unset, environment.variables) == (False, [], {})
    assert startup.env_wrapper_chdir("/usr/bin/env", ["-", "-C", "/", "claude"]) is None


def test_nested_env_wrapper_is_not_modeled():
    """A wrapper that runs another ``env`` reports no changes, so callers fall back."""
    args = ["-C", "/", "A=1", "env", "CLAUDE_CONFIG_DIR=/srv/claude", "claude"]
    assert startup.env_wrapper_environment("/usr/bin/env", args) is None
    assert startup.env_wrapper_chdir("/usr/bin/env", args) is None


def test_env_split_string_keeps_a_word_only_for_set_variables():
    """
    A word made only of unset ``${NAME}`` references is dropped, as in ``env``.

    A set but empty name still yields an empty word, which the real ``env``
    then fails to execute.
    """
    assert startup._split_env_string("${UNSET} A=1 claude", {}) == ["A=1", "claude"]
    assert startup._split_env_string("${EMPTY} A=1 claude", {"EMPTY": ""}) == [
        "",
        "A=1",
        "claude",
    ]
    assert startup._split_env_string('"${UNSET}" A=1 claude', {}) == ["", "A=1", "claude"]


@pytest.mark.parametrize(
    "args",
    [
        ["-C", "sub", "/bin/pwd"],
        ["-Csub", "/bin/pwd"],
        ["--chdir=link", "/bin/pwd"],
        ["-iC", "sub", "/bin/pwd"],
        ["-S", "-C sub /bin/pwd"],
        # env applies only the last -C, relative to the starting directory.
        ["-C", "link", "-C", "sub", "/bin/pwd"],
    ],
)
def test_env_wrapper_chdir_matches_real_env(tmp_path, args):
    """The modeled ``--chdir`` directory is where the real ``env`` runs the command."""
    if not _real_env_supports_chdir():
        pytest.skip("/usr/bin/env lacks -C/--chdir")
    (tmp_path / "sub").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "sub")
    chdir = startup.env_wrapper_chdir("/usr/bin/env", args)
    assert chdir is not None
    process = subprocess.run(
        ["/usr/bin/env", *args], cwd=tmp_path, env={}, capture_output=True, text=True, check=True
    )
    assert process.stdout.strip() == str((tmp_path / chdir).resolve())


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
