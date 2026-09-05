"""Data-only AgentSpec validation, including hostile and unsupported inputs."""

from __future__ import annotations

import importlib
import os
import socket
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.spec.offline import MAX_FILE_BYTES, MAX_FILES, _Reader, validate_path
from omnigent.spec.offline_scope import ContentError
from omnigent.spec.parser import parse, parse_config
from omnigent.spec.validator import validate

BASE = "spec_version: 1\nname: example\nexecutor:\n  config:\n    harness: claude-sdk\n"
SECRET = "offline-test-secret-not-a-real-credential"


def write_config(root: Path, text: str = BASE) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_bundle_and_explicit_config_have_the_same_report(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    directory = validate_path(str(tmp_path)).to_dict()
    explicit = validate_path(str(path)).to_dict()
    assert directory == explicit
    assert directory == {
        "schema_version": 1,
        "mode": "offline",
        "status": "valid",
        "exit_code": 0,
        "diagnostics": [],
        "skipped_checks": [
            {
                "code": "HOST_AUTH",
                "reason": "Host readiness, credentials, environment expansion "
                "and provider auth are not checked.",
            },
            {
                "code": "LIVE_SERVICES",
                "reason": "MCP, model and network availability are not checked.",
            },
            {
                "code": "CODE",
                "reason": "Tool implementations and policy handlers/arguments are not imported, "
                "resolved or executed.",
            },
            {
                "code": "PLUGINS",
                "reason": "Community harness plugins and optional-package availability "
                "are not checked.",
            },
        ],
    }


@pytest.mark.parametrize("tools", ["", "tools: {}\n", "tools: {agents: [worker]}\n"])
def test_offline_output_does_not_depend_on_container_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools: str
) -> None:
    path = write_config(tmp_path, BASE + tools)
    write_config(
        tmp_path / "agents" / "worker",
        "spec_version: 1\nname: worker\nexecutor: {type: agents_sdk}\n",
    )
    monkeypatch.delenv("OMNIGENT_CONTAINER_RUNTIME", raising=False)
    expected = validate_path(str(path)).to_dict()
    assert expected["status"] == "valid"
    for value in ("docker", "podman", "", "invalid-host-runtime"):
        monkeypatch.setenv("OMNIGENT_CONTAINER_RUNTIME", value)
        assert validate_path(str(path)).to_dict() == expected
        assert os.environ["OMNIGENT_CONTAINER_RUNTIME"] == value


def test_offline_validation_does_not_read_container_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_config(tmp_path)
    original_get = os.environ.get

    def guarded_get(key: str, default: object = None) -> object:
        assert key != "OMNIGENT_CONTAINER_RUNTIME", "Offline parsing read the runtime environment"
        return original_get(key, default)

    monkeypatch.setattr(os.environ, "get", guarded_get)
    assert validate_path(str(path)).exit_code == 0


@pytest.mark.parametrize("extension", [".yaml", ".yml"])
def test_explicit_v1_yaml_filename(tmp_path: Path, extension: str) -> None:
    path = tmp_path / f"agent{extension}"
    path.write_text(BASE, encoding="utf-8")
    assert validate_path(str(path)).exit_code == 0


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("", "INVALID_SHAPE"),
        ("[]", "INVALID_SHAPE"),
        ("name: old-format\nprompt: hello\npolicies: {}", "UNSUPPORTED_FORMAT"),
        ("spec_version: 2", "INVALID_SPEC"),
        ("spec_version: false", "INVALID_SHAPE"),
        ("spec_version: 1\nname: [one]", "INVALID_SHAPE"),
        (BASE + "unknown: ignored-by-runtime", "UNSUPPORTED_FIELD"),
        (BASE + "os_env: {cwd: /tmp}", "UNSUPPORTED_FIELD"),
        (BASE + "terminals: {}", "UNSUPPORTED_FIELD"),
        (BASE + "llm: []", "INVALID_SHAPE"),
        (BASE + "llm: {model: a, misspelled: true}", "UNSUPPORTED_FIELD"),
        (BASE + "interaction: {modalities: false}", "INVALID_SHAPE"),
        (BASE + "tools: {missing: {type: mcp}}", "INVALID_SHAPE"),
        (BASE + "tools: {missing: {type: function, callable: local.run}}", "UNSUPPORTED_FIELD"),
        (BASE + "tools: {builtins: [not_a_builtin]}", "INVALID_SHAPE"),
        (BASE + "tools: {builtins: [browser_navigate]}", "INVALID_SHAPE"),
        (BASE + "tools: {sandbox: {type: mcp, command: silently-discarded}}", "UNSUPPORTED_FIELD"),
        (BASE + "tools: {builtins: [{name: web_search, api_key: secret}]}", "INVALID_SHAPE"),
        (BASE + "tools: {agents: [missing]}", "INVALID_SPEC"),
        (BASE + "skills: [missing]", "INVALID_SHAPE"),
        (BASE + "params: &p {again: *p}", "UNSUPPORTED_YAML"),
        (BASE + "params: {<<: {value: 1}}", "UNSUPPORTED_YAML"),
        (BASE + "params: {key: first, key: second}", "DUPLICATE_KEY"),
        (BASE + "params: !!python/object/apply:os.system ['echo forbidden']", "INVALID_YAML"),
        (BASE + "params: [\n", "INVALID_YAML"),
        (BASE + "---\nsecond: document", "INVALID_YAML"),
    ],
)
def test_rejects_invalid_or_unvalidated_content(tmp_path: Path, text: str, code: str) -> None:
    path = write_config(tmp_path, text)
    result = validate_path(str(path))
    assert result.exit_code == 1
    assert result.diagnostics[0].code == code
    assert result.to_dict()["status"] == "invalid"


@pytest.mark.parametrize(
    "policy",
    [
        "type: function\n      function: local.handler\n      typo: deny",
        "type: function\n      function: local.handler\n      on: [response]",
        "type: function\n      function: local.handler\n      on: [not_a_phase]",
        "type: function\n      function: local.handler\n      handler: other.handler",
        "type: function\n      function: invalid-path",
        "type: function\n      function: {path: local.handler, arguments: []}",
        "type: function\n      function: {path: local.handler, typo: true}",
        "type: function\n      function: local.handler\n      set_labels: [missing]",
        "type: function\n      function: local.handler\n      condition: {missing: yes}",
        "type: function\n      function: local.handler\n      condition: {access: bad}",
        "type: prompt\n      prompt: Never disclose data",
    ],
)
def test_strict_policy_declarations(tmp_path: Path, policy: str) -> None:
    path = write_config(
        tmp_path,
        BASE + "guardrails:\n  labels:\n    access: {values: [yes, no]}\n"
        f"  policies:\n    gate:\n      {policy}\n",
    )
    assert validate_path(str(path)).exit_code == 1


def test_policy_factories_tools_and_credentials_are_only_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Offline validation attempted a runtime operation")

    path = write_config(
        tmp_path,
        BASE
        + """tools:
  local_mcp:
    type: mcp
    command: never-run-me
    args: [--start-agent]
    env: {TOKEN: "${OFFLINE_SECRET}"}
guardrails:
  labels:
    access: {initial: yes, values: [yes, no]}
  policies:
    gate:
      type: function
      handler:
        path: offline_bundle_trap.factory
        arguments: {api_key: "${OFFLINE_SECRET}"}
      config: {token: "${OFFLINE_SECRET}"}
      condition: {access: yes}
      set_labels: [access]
""",
    )
    trap = "raise AssertionError('Bundle-local code was imported')\n"
    (tmp_path / "offline_bundle_trap.py").write_text(trap, encoding="utf-8")
    tools = tmp_path / "tools" / "python"
    tools.mkdir(parents=True)
    (tools / "tool.py").write_text(trap, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("OFFLINE_SECRET", SECRET)
    from omnigent.spec import parser

    monkeypatch.setattr(parser, "expand_env_vars", forbidden)
    monkeypatch.setattr(importlib, "import_module", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = validate_path(str(path))
    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert result.exit_code == 0, result.to_dict()
    assert before == after
    assert SECRET not in str(result.to_dict())


@pytest.mark.parametrize(
    "text",
    [
        f"spec_version: '{SECRET}'",
        BASE + f"guardrails: {{labels: {{private: {{initial: '{SECRET}', values: [public]}}}}}}",
        BASE + f"params: {{'{SECRET}': [}}",
        BASE + f"tools: {{agents: ['{SECRET}']}}",
        BASE + f"params: {{'{SECRET}': 1, '{SECRET}': 2}}",
        BASE + f"guardrails: {{policies: {{'{SECRET}': {{type: function, on: [response]}}}}}}",
    ],
)
def test_diagnostics_never_disclose_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, text: str
) -> None:
    path = write_config(tmp_path, text)
    result = validate_path(str(path))
    assert result.exit_code == 1
    assert SECRET not in str(result.to_dict())
    assert SECRET not in caplog.text


def test_missing_subagent_is_not_pruned(tmp_path: Path) -> None:
    write_config(tmp_path)
    (tmp_path / "agents" / "incomplete").mkdir(parents=True)
    result = validate_path(str(tmp_path))
    assert result.exit_code == 1
    assert result.diagnostics[0].file == "agents/incomplete/config.yaml"


def test_invalid_child_and_duplicate_names_fail(tmp_path: Path) -> None:
    write_config(tmp_path)
    write_config(tmp_path / "agents" / "first")
    write_config(tmp_path / "agents" / "second")
    assert validate_path(str(tmp_path)).exit_code == 1


@pytest.mark.parametrize(
    "instruction", ["missing.md", "../secret.txt", "/secret.txt", "C:\\secret.txt"]
)
def test_strict_instruction_references(tmp_path: Path, instruction: str) -> None:
    path = write_config(tmp_path, BASE + f"instructions: '{instruction}'\n")
    assert validate_path(str(path)).exit_code == 1


def test_file_and_yaml_complexity_limits(tmp_path: Path) -> None:
    path = write_config(tmp_path, BASE + "#" * MAX_FILE_BYTES)
    assert validate_path(str(path)).diagnostics[0].code == "INPUT_LIMIT"
    path.write_text(BASE + "params: " + "[" * 40 + "0" + "]" * 40, encoding="utf-8")
    assert validate_path(str(path)).diagnostics[0].code == "INPUT_LIMIT"


@pytest.mark.parametrize(
    "value", ["https://example.invalid/agent.yaml", "\\\\server\\bundle", "//server/bundle", ""]
)
def test_remote_inputs_do_not_touch_the_filesystem(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Remote validation input touched the filesystem")

    monkeypatch.setattr(Path, "lstat", forbidden)
    assert validate_path(value).exit_code == 2


def test_missing_and_unsupported_input(tmp_path: Path) -> None:
    assert validate_path(str(tmp_path / "missing.yaml")).exit_code == 2
    path = tmp_path / "bundle.tar.gz"
    path.write_bytes(b"not an archive")
    assert validate_path(str(path)).exit_code == 2
    assert validate_path(str(tmp_path)).exit_code == 1


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX symlink/FIFO safety; native Windows unsupported"
)
def test_symlinks_and_special_files_are_not_read(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    write_config(bundle, BASE + "instructions: prompt.md\n")
    target = tmp_path / "outside.md"
    target.write_text(SECRET, encoding="utf-8")
    (bundle / "prompt.md").symlink_to(target)
    result = validate_path(str(bundle))
    assert result.exit_code == 1
    assert SECRET not in str(result.to_dict())
    (bundle / "prompt.md").unlink()
    os.mkfifo(bundle / "prompt.md")
    assert validate_path(str(bundle)).exit_code == 1
    alias = tmp_path / "alias"
    alias.symlink_to(bundle, target_is_directory=True)
    assert validate_path(str(alias)).exit_code == 2


def test_shared_config_parser_preserves_normal_bundle_parsing(tmp_path: Path) -> None:
    write_config(tmp_path, BASE + "instructions: prompt.md\n")
    (tmp_path / "prompt.md").write_text("Hello from instructions.", encoding="utf-8")
    spec = parse(tmp_path, expand_env=False)
    assert spec.instructions == "Hello from instructions."
    assert spec.executor.config["harness"] == "claude-sdk"
    assert validate(spec, offline=True).valid
    assert (
        parse_config({"spec_version": 1, "executor": {"type": "agents_sdk"}}).instructions is None
    )


def test_long_single_line_prose_remains_inline(tmp_path: Path) -> None:
    path = write_config(tmp_path, BASE + "instructions: " + "You are a helpful assistant. " * 40)
    assert validate_path(str(path)).exit_code == 0


def test_empty_inline_instructions_are_not_a_directory_reference(tmp_path: Path) -> None:
    path = write_config(tmp_path, BASE + "instructions: ''\n")
    assert validate_path(str(path)).exit_code == 0


def test_diagnostics_ignore_directory_creation_order(tmp_path: Path) -> None:
    results = []
    for name, children in (("one", ("b", "A")), ("two", ("A", "b"))):
        root = tmp_path / name
        write_config(root)
        for child in children:
            write_config(root / "agents" / child, BASE + "guardrails: {labels: []}")
        result = validate_path(str(root))
        assert result.exit_code == 1
        assert result.diagnostics[0].file == "agents/A/config.yaml"
        results.append(result.to_dict())
    assert results[0] == results[1]


@pytest.mark.parametrize("name", ["on", "yes", "true"])
def test_mcp_scalar_coercion_cannot_hide_collisions(tmp_path: Path, name: str) -> None:
    write_config(tmp_path)
    mcp = tmp_path / "tools" / "mcp"
    mcp.mkdir(parents=True)
    (mcp / "server.yaml").write_text(
        f"name: {name}\ntransport: http\nurl: https://example.invalid/mcp\n", encoding="utf-8"
    )
    assert validate_path(str(tmp_path)).exit_code == 1
    (mcp / "server.yaml").write_text(
        f"name: '{name}'\ntransport: http\nurl: https://example.invalid/mcp\n", encoding="utf-8"
    )
    assert validate_path(str(tmp_path)).exit_code == 0
    assert parse(tmp_path, expand_env=False).mcp_servers[0].name == name


def test_directory_limit_bounds_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    class Entries:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def __iter__(self):
            for index in range(MAX_FILES * 10):
                calls.append(index)
                yield types.SimpleNamespace(path=str(tmp_path / str(index)))

    monkeypatch.setattr(os, "scandir", lambda path: Entries())
    with pytest.raises(ContentError, match="entry limit"):
        _Reader(tmp_path).entries(tmp_path)
    assert len(calls) == MAX_FILES + 1


def test_legacy_spec_exports_are_lazy_and_forward_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnigent.spec as spec_module

    compat = types.ModuleType("omnigent.spec._omnigent_compat")
    compat.is_omnigent_yaml = Mock(return_value=True)
    compat.diagnose_yaml_rejection = Mock(return_value="reason")
    sentinel = parse_config({"spec_version": 1})
    compat.load_omnigent_yaml = Mock(return_value=sentinel)
    monkeypatch.setitem(sys.modules, compat.__name__, compat)
    path = tmp_path / "legacy.yaml"
    assert spec_module.is_omnigent_yaml(path)
    assert spec_module.diagnose_yaml_rejection(path) == "reason"
    assert (
        spec_module.load_omnigent_yaml(
            path, enforce_handler_allowlist=True, prune_invalid_sub_agents=True
        )
        is sentinel
    )
    compat.load_omnigent_yaml.assert_called_once_with(
        path, enforce_handler_allowlist=True, prune_invalid_sub_agents=True
    )
