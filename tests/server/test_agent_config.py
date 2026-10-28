"""Bundle-level coverage of partial configuration edits and safe projections."""

from __future__ import annotations

import hashlib
import io
import tarfile
import traceback

import pytest
import yaml
from fastapi import HTTPException

from omnigent.entities import Agent
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.agent_config import agent_config_object, edit_agent_bundle
from omnigent.server.schemas import AgentConfigPatch
from omnigent.spec.parser import _ConfigYamlLoader
from tests._helpers.session import bundle_files

_CONFIG = b"spec_version: 1\nname: example\nexecutor:\n  config:\n    harness: claude-sdk\n"


def _files(bundle: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(bundle)) as archive:
        return {
            member.name.removeprefix("./"): content.read()
            for member in archive
            if member.isfile() and (content := archive.extractfile(member)) is not None
        }


def test_partial_edit_preserves_assets_scalars_and_executor_options(monkeypatch):
    monkeypatch.setenv("EDIT_SECRET", "server-secret")
    config = _CONFIG + (
        b"description: old\ninstructions: shared.md\n"
        b"llm:\n  model: old-model\n  connection:\n    api_key: ${EDIT_SECRET}\n"
        b"future: {mode: on, date: 2026-01-01, leading: 001}\n"
    )
    assets = {
        "shared.md": b"Shared instruction asset",
        "agents/child/config.yaml": _CONFIG.replace(b"example", b"child")
        + b"instructions: child.md\n",
        "agents/child/child.md": b"Child instructions",
        "files/data.bin": b"\x00\xffdata",
        "scripts/run.sh": b"#!/bin/sh\nexit 0\n",
    }
    initial = bundle_files({"config.yaml": config, **assets})
    # bundle_files supplies contents; this scenario additionally needs executable metadata.
    buf = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(initial)) as source,
        tarfile.open(fileobj=buf, mode="w:gz") as archive,
    ):
        for member in source:
            if member.name == "scripts/run.sh":
                member.mode = 0o751
            archive.addfile(member, source.extractfile(member))
    bundle, spec = edit_agent_bundle(
        buf.getvalue(), AgentConfigPatch(description="new", model="new-model", harness="codex")
    )
    files = _files(bundle)
    assert all(files[name] == data for name, data in assets.items())
    raw = yaml.load(files["config.yaml"], Loader=_ConfigYamlLoader)
    assert raw["future"] == yaml.load(config, Loader=_ConfigYamlLoader)["future"]
    assert raw["llm"] == {"model": "old-model", "connection": {"api_key": "${EDIT_SECRET}"}}
    assert spec.description == "new"
    assert spec.llm is not None
    assert spec.executor.model == spec.llm.model == "new-model"
    assert spec.executor.connection == {"api_key": "${EDIT_SECRET}"}
    assert spec.executor.harness_kind == "codex"
    assert spec.instructions == "Shared instruction asset"
    with tarfile.open(fileobj=io.BytesIO(bundle)) as archive:
        assert archive.getmember("./scripts/run.sh").mode & 0o111 == 0o111


@pytest.mark.parametrize("instructions", [None, "", "shared.md", "literal\ntext\n"])
def test_instruction_override_is_literal_and_keeps_original_assets(instructions):
    assets = {"shared.md": b"Original", "AGENTS.md": b"Fallback"}
    bundle = bundle_files({"config.yaml": _CONFIG + b"prompt: shared.md\n", **assets})
    result, spec = edit_agent_bundle(bundle, AgentConfigPatch(instructions=instructions))
    assert spec.instructions == (instructions or "")
    files = _files(result)
    assert all(files[name] == data for name, data in assets.items())
    raw = yaml.safe_load(files["config.yaml"])
    assert raw["prompt"] == "shared.md"
    if instructions:
        assert files[raw["instructions"]].decode() == instructions
    else:
        assert raw["instructions"] == ""
    assert edit_agent_bundle(result, AgentConfigPatch(instructions=instructions))[0] == result


def test_instruction_hash_collision_is_not_overwritten():
    text = "New instructions"
    filename = f"instructions-{hashlib.sha256(text.encode()).hexdigest()}.md"
    bundle = bundle_files({"config.yaml": _CONFIG, filename: b"Existing asset"})
    with pytest.raises(HTTPException) as caught:
        edit_agent_bundle(bundle, AgentConfigPatch(instructions=text))
    assert caught.value.status_code == 409
    bundle = bundle_files({"config.yaml": _CONFIG, filename: text.encode()})
    edited, spec = edit_agent_bundle(bundle, AgentConfigPatch(instructions=text))
    assert spec.instructions == text
    assert set(_files(edited)) == {"config.yaml", filename}


def test_credential_changes_match_the_names_exposed_by_config_reads():
    bundle = bundle_files(
        {
            "config.yaml": _CONFIG,
            "tools/mcp/docs.yaml": (
                b"name: docs\ntransport: http\nurl: https://example.com/mcp\n"
                b"headers:\n  123: token\n  true: old\n"
            ),
        }
    )
    _, spec = edit_agent_bundle(bundle, None)
    projection = agent_config_object(Agent("id", 1, "example", "blob"), spec)
    assert projection.mcp_servers["docs"].configured_header_names == ["123", "True"]
    patched, spec = edit_agent_bundle(
        bundle,
        AgentConfigPatch.model_validate(
            {"mcp_servers": {"docs": {"headers": {"123": None, "True": "updated"}}}}
        ),
    )
    assert spec.mcp_servers[0].headers == {"True": "updated"}
    assert yaml.safe_load(_files(patched)["tools/mcp/docs.yaml"])["headers"] == {True: "updated"}


@pytest.mark.parametrize("layout", ["inline", "sidecar"])
@pytest.mark.parametrize("transport", ["http", "stdio"])
def test_mcp_typed_merge_preserves_advanced_fields_and_credentials(layout, transport):
    credentials = "headers" if transport == "http" else "env"
    connection = (
        {"url": "https://example.com/mcp"}
        if transport == "http"
        else {"command": "echo", "args": ["old"]}
    )
    declaration = {
        **connection,
        credentials: {"KEEP": "${KEEP_SECRET}", "REMOVE": "old-secret"},
        "description": "original",
        "timeout": 25,
        "tools": ["lookup"],
        "future": {"nested": ["preserve", 42]},
    }
    if layout == "inline":
        bundle = bundle_files(
            {
                "config.yaml": _CONFIG
                + yaml.safe_dump({"tools": {"docs": {"type": "mcp", **declaration}}}).encode()
            }
        )
    else:
        bundle = bundle_files(
            {
                "config.yaml": _CONFIG,
                "tools/mcp/unrelated-filename.yaml": yaml.safe_dump(
                    {"name": "docs", "transport": transport, **declaration}
                ).encode(),
            }
        )
    patch = AgentConfigPatch.model_validate(
        {
            "mcp_servers": {
                "docs": {
                    "description": None,
                    credentials: {"REMOVE": None, "NEW": "", "ABSENT": None},
                    **({"args": []} if transport == "stdio" else {}),
                }
            }
        }
    )
    result, spec = edit_agent_bundle(bundle, patch)
    files = _files(result)
    raw = (
        yaml.safe_load(files["config.yaml"])["tools"]["docs"]
        if layout == "inline"
        else (yaml.safe_load(files["tools/mcp/unrelated-filename.yaml"]))
    )
    assert raw[credentials] == {"KEEP": "${KEEP_SECRET}", "NEW": ""}
    for key in ("timeout", "tools", "future"):
        assert raw[key] == declaration[key]
    assert spec.mcp_servers[0].description is None
    assert spec.mcp_servers[0].args == []
    projected = agent_config_object(Agent("agent-id", 1, "example", "blob", version=4), spec)
    server = projected.mcp_servers["docs"]
    assert getattr(server, f"configured_{'header' if transport == 'http' else 'env'}_names") == [
        "KEEP",
        "NEW",
    ]
    assert "SECRET" not in projected.model_dump_json()
    assert projected.id == "agent-id" and projected.version == 4
    assert server.editable
    assert edit_agent_bundle(result, patch)[0] == result
    no_op = AgentConfigPatch.model_validate(
        {
            "mcp_servers": {
                "docs": {"transport": transport, "description": None, "args": [], credentials: {}}
            }
        }
    )
    assert edit_agent_bundle(result, no_op)[0] == result
    cleared, spec = edit_agent_bundle(
        result, AgentConfigPatch.model_validate({"mcp_servers": {"docs": {credentials: None}}})
    )
    assert not getattr(spec.mcp_servers[0], credentials)
    removed, spec = edit_agent_bundle(cleared, AgentConfigPatch(mcp_servers={"docs": None}))
    assert not spec.mcp_servers
    assert edit_agent_bundle(removed, AgentConfigPatch(mcp_servers={"docs": None}))[0] == removed


@pytest.mark.parametrize(
    "patch",
    [
        None,
        {},
        {"description": None},
        {"mcp_servers": {}},
        {"model": "legacy"},
        {"harness": "claude-sdk"},
    ],
)
def test_semantic_noop_returns_original_bytes(patch):
    bundle = bundle_files({"config.yaml": _CONFIG + b"llm: {model: legacy}\n"})
    typed = AgentConfigPatch.model_validate(patch) if patch is not None else None
    result, spec = edit_agent_bundle(bundle, typed)
    assert result is bundle
    assert spec.name == "example"


def test_mcp_creation_and_registry_read_only():
    bundle = bundle_files(
        {"config.yaml": _CONFIG + b"tools:\n  managed:\n    type: mcp\n    transport: registry\n"}
    )
    result, spec = edit_agent_bundle(
        bundle,
        AgentConfigPatch.model_validate(
            {"mcp_servers": {"docs": {"transport": "http", "url": "https://example.com/mcp"}}}
        ),
    )
    assert {server.name for server in spec.mcp_servers} == {"docs", "managed"}
    assert "tools/mcp/docs.yaml" in _files(result)
    projected = agent_config_object(Agent("id", 1, "example", "blob"), spec)
    assert not projected.mcp_servers["managed"].editable
    for patch in (None, {}, {"description": "update"}):
        with pytest.raises(HTTPException) as caught:
            edit_agent_bundle(
                result, AgentConfigPatch.model_validate({"mcp_servers": {"managed": patch}})
            )
        assert caught.value.status_code == 409


@pytest.mark.parametrize("yaml_name,name", [("on", "on"), ("true", "True"), ("123", "123")])
def test_inline_mcp_yaml_scalar_names_and_values_use_root_parser(yaml_name, name):
    bundle = bundle_files(
        {
            "config.yaml": _CONFIG
            + (
                f"tools:\n  {yaml_name}:\n    type: mcp\n    url: https://example.com/mcp\n".encode()
                + b"    headers: {on: off}\n    future: {yes: no}\n"
            )
        }
    )
    result, spec = edit_agent_bundle(
        bundle,
        AgentConfigPatch.model_validate({"mcp_servers": {name: {"description": "updated"}}}),
    )
    assert spec.mcp_servers[0].headers == {"on": "off"}
    raw = yaml.load(_files(result)["config.yaml"], Loader=_ConfigYamlLoader)
    assert next(iter(raw["tools"].values()))["future"] == {"yes": "no"}
    assert f"tools/mcp/{name}.yaml" not in _files(result)
    assert not edit_agent_bundle(result, AgentConfigPatch(mcp_servers={name: None}))[1].mcp_servers


@pytest.mark.parametrize(
    "files,name",
    [
        (
            {
                "config.yaml": _CONFIG + b"tools:\n  docs: {type: mcp, future: managed}\n",
                "tools/mcp/docs.yaml": b"name: docs\ntransport: http\nurl: https://example.com/mcp\n",
            },
            "docs",
        ),
        (
            {
                "config.yaml": _CONFIG
                + (
                    b"tools:\n  true: {type: mcp, future: managed}\n"
                    b'  "True": {type: mcp, url: https://example.com/mcp}\n'
                ),
            },
            "True",
        ),
    ],
)
@pytest.mark.parametrize("patch", [None, {"description": "updated"}])
def test_shadowed_mcp_declarations_are_not_edited(files, name, patch):
    bundle = bundle_files(files)
    assert edit_agent_bundle(bundle, None)[1].mcp_servers[0].name == name
    with pytest.raises(HTTPException) as caught:
        edit_agent_bundle(bundle, AgentConfigPatch.model_validate({"mcp_servers": {name: patch}}))
    assert caught.value.status_code == 409
    assert caught.value.detail["message"] == "Ambiguous MCP declarations cannot be edited"


def test_inline_ignored_fields_survive_noop_and_unrelated_edit():
    bundle = bundle_files(
        {
            "config.yaml": _CONFIG
            + (
                b"tools:\n  docs:\n    type: mcp\n    url: https://example.com/old\n"
                b"    env: {}\n    args: []\n    future: retained\n"
            )
        }
    )
    assert (
        edit_agent_bundle(bundle, AgentConfigPatch.model_validate({"mcp_servers": {"docs": {}}}))[
            0
        ]
        is bundle
    )
    result, spec = edit_agent_bundle(
        bundle,
        AgentConfigPatch.model_validate(
            {"mcp_servers": {"docs": {"url": "https://example.com/new"}}}
        ),
    )
    raw = yaml.safe_load(_files(result)["config.yaml"])["tools"]["docs"]
    assert raw["env"] == {} and raw["args"] == [] and raw["future"] == "retained"
    assert spec.mcp_servers[0].url == "https://example.com/new"


@pytest.mark.parametrize(
    "transport,connection,patch",
    [
        ("http", "url: https://example.com/mcp", {"env": {"PRIVATE": "hidden-secret"}}),
        ("stdio", "command: echo", {"headers": {"Authorization": "hidden-secret"}}),
    ],
)
def test_inline_merged_raw_transport_fields_are_validated(transport, connection, patch):
    bundle = bundle_files(
        {
            "config.yaml": _CONFIG
            + (
                f"tools:\n  docs:\n    type: mcp\n    transport: {transport}\n    {connection}\n"
            ).encode()
        }
    )
    with pytest.raises(OmnigentError) as caught:
        edit_agent_bundle(
            bundle, AgentConfigPatch.model_validate({"mcp_servers": {"docs": patch}})
        )
    assert str(caught.value) == "Invalid agent configuration"


def test_loaded_effective_root_fields_are_a_noop():
    bundle = bundle_files(
        {
            "config.yaml": _CONFIG
            + b"description: Original\nllm: {model: legacy}\nprompt: shared.md\n",
            "shared.md": b"Original instructions",
        }
    )
    _, spec = edit_agent_bundle(bundle, None)
    patch = AgentConfigPatch(
        description=spec.description,
        model=spec.executor.model,
        harness=spec.executor.harness_kind,
        instructions=spec.instructions,
    )
    assert edit_agent_bundle(bundle, patch)[0] is bundle


@pytest.mark.parametrize(
    "patch",
    [
        {"harness": "unknown"},
        {"mcp_servers": {"docs": {"transport": "stdio", "command": "echo"}}},
        {"mcp_servers": {"new": {"transport": "http"}}},
        {"mcp_servers": {"docs": {"url": "invalid-secret-url"}}},
    ],
)
def test_invalid_final_configuration_is_rejected_without_inputs(patch, caplog):
    bundle = bundle_files(
        {
            "config.yaml": _CONFIG
            + b"tools:\n  docs:\n    type: mcp\n    url: https://example.com/mcp\n"
        }
    )
    with pytest.raises(OmnigentError) as caught:
        edit_agent_bundle(bundle, AgentConfigPatch.model_validate(patch))
    assert caught.value.code == ErrorCode.INVALID_INPUT
    assert str(caught.value) == "Invalid agent configuration"
    assert "invalid-secret-url" not in "".join(traceback.format_exception(caught.value))
    assert "invalid-secret-url" not in caplog.text


def test_touched_mcp_runs_existing_policy_but_omitted_mcp_does_not(monkeypatch):
    bundle = bundle_files(
        {"config.yaml": _CONFIG + b"tools:\n  local:\n    type: mcp\n    command: echo\n"}
    )
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "0")
    assert (
        edit_agent_bundle(bundle, AgentConfigPatch(description="allowed"))[1].description
        == "allowed"
    )
    with pytest.raises(OmnigentError) as caught:
        edit_agent_bundle(
            bundle,
            AgentConfigPatch.model_validate(
                {"mcp_servers": {"local": {"description": "changed"}}}
            ),
        )
    assert caught.value.code == ErrorCode.FORBIDDEN
    assert "stdio MCP servers are not permitted" in str(caught.value)


@pytest.mark.parametrize(
    "config", [b"name: [private-secret", _CONFIG + b"executor: private-secret\n"]
)
def test_invalid_stored_bundle_errors_are_sanitized(config, caplog):
    with pytest.raises(OmnigentError) as caught:
        edit_agent_bundle(bundle_files({"config.yaml": config}), None)
    assert str(caught.value) == "Invalid agent configuration"
    assert "private-secret" not in "".join(traceback.format_exception(caught.value))
    assert "private-secret" not in caplog.text


@pytest.mark.parametrize(
    "failure_site,message",
    [
        ("omnigent.server.bundles.load", "Agent bundle validation failed"),
        ("omnigent.server.agent_config.extract_safe", "Agent configuration edit failed"),
    ],
)
def test_filesystem_failures_are_sanitized_server_errors(
    monkeypatch, caplog, failure_site, message
):
    def fail(*args, **kwargs):
        raise OSError("No space left on device: private-path")

    monkeypatch.setattr(failure_site, fail)
    with pytest.raises(OmnigentError) as caught:
        edit_agent_bundle(
            bundle_files({"config.yaml": _CONFIG}), AgentConfigPatch(description="updated")
        )
    assert caught.value.code == ErrorCode.INTERNAL_ERROR
    assert caught.value.http_status == 500
    assert str(caught.value) == message
    assert "private-path" not in "".join(traceback.format_exception(caught.value))
    assert "private-path" not in caplog.text


def test_unsupported_format_and_incompatible_executor_options():
    legacy = bundle_files(
        {"agent.yaml": b"name: old\nprompt: hi\nexecutor: {harness: claude-sdk}\n"}
    )
    with pytest.raises(HTTPException) as caught:
        edit_agent_bundle(legacy, None)
    assert caught.value.status_code == 409
    assert isinstance(caught.value.detail, dict)
    assert caught.value.detail["code"] == "agent_config_not_editable"
    bundle = bundle_files(
        {
            "config.yaml": _CONFIG.replace(
                b"harness: claude-sdk", b"harness: pi\n    context_files: true"
            )
        }
    )
    with pytest.raises(OmnigentError):
        edit_agent_bundle(bundle, AgentConfigPatch(harness="codex"))


@pytest.mark.parametrize("patch", [{"model": "new-model"}, {"harness": "codex"}])
def test_model_and_harness_edits_require_compatible_executor(patch):
    bundle = bundle_files(
        {"config.yaml": b"spec_version: 1\nname: example\nexecutor: {type: claude_sdk}\n"}
    )
    assert (
        edit_agent_bundle(bundle, AgentConfigPatch(description="allowed"))[1].description
        == "allowed"
    )
    with pytest.raises(HTTPException) as caught:
        edit_agent_bundle(bundle, AgentConfigPatch.model_validate(patch))
    assert caught.value.status_code == 409
