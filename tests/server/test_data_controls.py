"""Unit tests for the deployment data-handling controls."""

from __future__ import annotations

import base64
import copy
import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.entities.conversation import ITEM_TYPE_TO_DATA_CLS
from omnigent.server import app as app_module
from omnigent.server.data_controls import (
    OUTPUT_FREE_ITEM_TYPES,
    OUTPUT_ITEM_TYPES,
    OUTPUT_OMITTED,
    ContentSecurityPolicyMode,
    DataControls,
    IncludeOutputs,
    content_security_policy,
    inline_script_hashes,
    load_data_controls,
    omit_item_outputs,
)


def test_defaults_change_nothing() -> None:
    controls = load_data_controls({}, environ={})
    assert controls == DataControls()
    assert controls.export_includes_outputs(True)
    assert not controls.export_includes_outputs(False)
    assert controls.info() == {
        "include_outputs": "request",
        "allow_downloads": True,
        "content_security_policy": "off",
    }


@pytest.mark.parametrize(
    ("section", "environ", "expected"),
    [
        (
            {
                "include_outputs": "never",
                "allow_downloads": False,
                "content_security_policy": "enforce",
            },
            {},
            DataControls(IncludeOutputs.NEVER, False, ContentSecurityPolicyMode.ENFORCE),
        ),
        # YAML 1.1 reads a bare ``off`` as false and ``on`` as true.
        ({"content_security_policy": False}, {}, DataControls()),
        (
            {"content_security_policy": True},
            {},
            DataControls(content_security_policy=ContentSecurityPolicyMode.ENFORCE),
        ),
        (
            {"content_security_policy": "report_only", "allow_downloads": "no"},
            {},
            DataControls(
                allow_downloads=False,
                content_security_policy=ContentSecurityPolicyMode.REPORT_ONLY,
            ),
        ),
        # An environment variable overrides the file.
        (
            {"include_outputs": "request", "allow_downloads": True},
            {
                "OMNIGENT_EXPORT_INCLUDE_OUTPUTS": "NEVER",
                "OMNIGENT_ALLOW_DOWNLOADS": "0",
                "OMNIGENT_CONTENT_SECURITY_POLICY": "report-only",
            },
            DataControls(IncludeOutputs.NEVER, False, ContentSecurityPolicyMode.REPORT_ONLY),
        ),
        (
            {},
            {"OMNIGENT_CONTENT_SECURITY_POLICY": "true"},
            DataControls(content_security_policy=ContentSecurityPolicyMode.ENFORCE),
        ),
        # A blank variable does not override the file.
        (
            {"include_outputs": "never"},
            {"OMNIGENT_EXPORT_INCLUDE_OUTPUTS": " "},
            DataControls(include_outputs=IncludeOutputs.NEVER),
        ),
    ],
    ids=["config", "yaml-off", "yaml-on", "aliases", "env-wins", "env-true", "blank-env"],
)
def test_reads_the_config_and_environment(
    section: dict[str, Any], environ: dict[str, str], expected: DataControls
) -> None:
    assert load_data_controls({"data_controls": section}, environ=environ) == expected


@pytest.mark.parametrize(
    ("config", "environ", "message"),
    [
        ({"data_controls": ["never"]}, {}, "data_controls must be a mapping"),
        ({"data_controls": {"include_outputs": "always"}}, {}, "include_outputs"),
        ({"data_controls": {"allow_downloads": "maybe"}}, {}, "allow_downloads"),
        ({"data_controls": {"content_security_policy": "strict"}}, {}, "content_security_policy"),
        ({}, {"OMNIGENT_ALLOW_DOWNLOADS": "nope"}, "OMNIGENT_ALLOW_DOWNLOADS"),
    ],
)
def test_an_invalid_value_fails_loudly(
    config: dict[str, Any], environ: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        load_data_controls(config, environ=environ)


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (
            {"id": "fco_1", "type": "function_call_output", "call_id": "c1", "output": "secret"},
            {
                "id": "fco_1",
                "type": "function_call_output",
                "call_id": "c1",
                "output": OUTPUT_OMITTED,
            },
        ),
        (
            {"type": "terminal_command", "kind": "output", "stdout": "secret", "stderr": "err"},
            {
                "type": "terminal_command",
                "kind": "output",
                "stdout": OUTPUT_OMITTED,
                "stderr": None,
            },
        ),
        (
            {"type": "terminal_command", "kind": "input", "input": "cat secrets"},
            {"type": "terminal_command", "kind": "input", "input": "cat secrets"},
        ),
        (
            {
                "type": "native_tool",
                "item": {"type": "code_interpreter_call", "id": "ci_1", "outputs": ["secret"]},
            },
            {"type": "native_tool", "item": {"type": "code_interpreter_call", "id": "ci_1"}},
        ),
        (
            {"type": "function_call", "name": "shell", "arguments": '{"cmd": "ls"}'},
            {"type": "function_call", "name": "shell", "arguments": '{"cmd": "ls"}'},
        ),
        (
            {
                "type": "compaction",
                "summary": "Loaded the data.",
                "compacted_messages": [{"role": "tool", "content": "secret"}],
            },
            {"type": "compaction", "summary": "Loaded the data.", "compacted_messages": None},
        ),
        (
            {"type": "slash_command", "name": "status", "arguments": "", "output": "secret"},
            {"type": "slash_command", "name": "status", "arguments": "", "output": OUTPUT_OMITTED},
        ),
        (
            {"id": "x_1", "type": "future_tool_result", "status": "completed", "data": "secret"},
            {"id": "x_1", "type": "future_tool_result", "status": "completed"},
        ),
    ],
    ids=[
        "function-output",
        "terminal-output",
        "terminal-input",
        "native-tool",
        "call",
        "compaction",
        "slash-command",
        "unknown-type",
    ],
)
def test_omit_item_outputs(item: dict[str, Any], expected: dict[str, Any]) -> None:
    original = copy.deepcopy(item)
    assert omit_item_outputs(item) == expected
    assert item == original


def test_every_item_type_has_an_export_decision() -> None:
    """A new item type must be classified before it can carry outputs into exports."""
    assert set(ITEM_TYPE_TO_DATA_CLS) == OUTPUT_FREE_ITEM_TYPES | OUTPUT_ITEM_TYPES
    assert not OUTPUT_FREE_ITEM_TYPES & OUTPUT_ITEM_TYPES


def test_inline_script_hashes_cover_only_inline_scripts() -> None:
    body = 'window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";'
    html = (
        f"<head><script>{body}</script>"
        '<script type="module" crossorigin src="/assets/index.js"></script>'
        '<script data-src="x">late()</script></head>'
    )
    digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
    late = base64.b64encode(hashlib.sha256(b"late()").digest()).decode()
    assert inline_script_hashes(html) == [f"'sha256-{digest}'", f"'sha256-{late}'"]


def _web_ui(tmp_path: Path) -> Path:
    dist = tmp_path / "web-ui"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><html><head><script type="module" src="./assets/index.js"></script>'
        "</head><body></body></html>"
    )
    (dist / "assets" / "index.js").write_text("console.log('hi');")
    return dist


async def _get(
    app: Any, *paths: str, headers: dict[str, str] | None = None
) -> list[httpx.Response]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return [await client.get(path, headers=headers) for path in paths]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "header"),
    [
        (ContentSecurityPolicyMode.ENFORCE, "content-security-policy"),
        (ContentSecurityPolicyMode.REPORT_ONLY, "content-security-policy-report-only"),
    ],
)
async def test_spa_html_carries_the_policy(
    tmp_path: Path, mode: ContentSecurityPolicyMode, header: str
) -> None:
    """Every HTML response carries the policy, with the base-path script hashed; assets don't."""
    spa = app_module._SPAStaticFiles(
        directory=_web_ui(tmp_path),
        html=True,
        base_path="/proxy/6767",
        content_security_policy=mode,
    )
    root, deep, asset = await _get(spa, "/", "/c/conv_abc", "/assets/index.js")

    script_hashes = inline_script_hashes(root.text)
    assert len(script_hashes) == 1  # the injected base-path global
    for response in (root, deep):
        assert response.status_code == 200
        assert response.headers[header] == content_security_policy(script_hashes)
    assert header not in asset.headers
    other = {"content-security-policy", "content-security-policy-report-only"} - {header}
    assert not other & set(root.headers)

    (revalidated,) = await _get(spa, "/", headers={"if-none-match": root.headers["etag"]})
    assert revalidated.status_code == 304
    assert revalidated.headers[header] == root.headers[header]


@pytest.mark.asyncio
async def test_changing_the_mode_invalidates_cached_html(tmp_path: Path) -> None:
    """A browser revalidating a page cached under one mode gets a fresh 200 under another."""
    dist = _web_ui(tmp_path)
    etags = {}
    for mode in ContentSecurityPolicyMode:
        spa = app_module._SPAStaticFiles(directory=dist, html=True, content_security_policy=mode)
        (root,) = await _get(spa, "/")
        etags[mode] = root.headers["etag"]
    assert len(set(etags.values())) == len(etags)

    off = app_module._SPAStaticFiles(directory=dist, html=True)
    (revalidated,) = await _get(
        off, "/", headers={"if-none-match": etags[ContentSecurityPolicyMode.ENFORCE]}
    )
    assert revalidated.status_code == 200
    assert "content-security-policy" not in revalidated.headers


@pytest.mark.asyncio
async def test_spa_html_has_no_policy_by_default(tmp_path: Path) -> None:
    spa = app_module._SPAStaticFiles(directory=_web_ui(tmp_path), html=True)
    (root,) = await _get(spa, "/")
    assert root.status_code == 200
    assert "content-security-policy" not in root.headers
    assert "content-security-policy-report-only" not in root.headers


def test_policy_allows_wasm_but_not_script_eval() -> None:
    policy = dict(
        directive.split(" ", 1)
        for directive in content_security_policy(["'sha256-abc'"]).split("; ")
    )
    sources = policy["script-src"].split()
    assert sources[:3] == ["'self'", "'wasm-unsafe-eval'", "'sha256-abc'"]
    assert all(source.startswith("'sha256-") for source in sources[3:])
    assert "'unsafe-eval'" not in policy["script-src"]
    assert "'unsafe-inline'" not in policy["script-src"]
    assert policy["object-src"] == "'none'"
    assert policy["img-src"] == "'self' data: blob:"


@pytest.mark.asyncio
async def test_create_app_wires_the_configured_policy(
    runtime_init: None, db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``data_controls`` config block reaches the SPA mount and ``/v1/info``."""
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

    del runtime_init
    monkeypatch.setattr(app_module, "_WEB_UI_DIST", _web_ui(tmp_path))
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = app_module.create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        server_config={"data_controls": {"content_security_policy": "enforce"}},
    )
    root, info = await _get(app, "/", "/v1/info")

    assert root.headers["content-security-policy"] == content_security_policy()
    assert "content-security-policy" not in info.headers
    assert info.json()["data_controls"]["content_security_policy"] == "enforce"
