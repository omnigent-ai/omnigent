"""HTTP bundle uploads apply the same content validation in every auth posture.

Install, create and replace share ``validate_agent_bundle``; a local single-user
server must reject the same bundles as a multi-user server.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from tests._helpers.session import post_session_bundle
from tests.server.conftest import _app_client
from tests.server.test_bundle_function_policies import _CUSTOM
from tests.server.test_bundle_function_policies import _bundle as _policy_bundle
from tests.server.test_bundles import (
    _CALLABLE_TOOL_YAML,
    _MIN_CONFIG,
    _bundle_with_cwd,
    _make_bundle_bytes,
    _single_file_yaml_bundle,
)

pytestmark = pytest.mark.asyncio

_MULTI_USER = {"X-Forwarded-Email": "alice@example.com"}

# case -> (agent name, invalid bundle, validator message)
_CASES: dict[str, tuple[str, bytes, str]] = {
    "unregistered_handler": (
        "review_agent",
        _policy_bundle({"function": {"path": _CUSTOM}}, "directory"),
        r"not a registered policy handler",
    ),
    "callable_tool": (
        "callable_agent",
        _single_file_yaml_bundle(_CALLABLE_TOOL_YAML.format(name="callable_agent")),
        r"server-side Python callable tool",
    ),
    "source_file_tool": (
        "filetool_agent",
        _make_bundle_bytes(
            {
                "config.yaml": _MIN_CONFIG.format(name="filetool_agent"),
                "tools/python/echo.py": "def echo(text: str) -> str:\n    return text\n",
            }
        ),
        r"server-side Python/TS tool file",
    ),
    "absolute_cwd": (
        "uploaded-agent",
        _bundle_with_cwd("/operator/workspace"),
        r"os_env\.cwd must be a relative path within the workspace",
    ),
}


def _upload(bundle: bytes) -> dict[str, tuple[str, bytes, str]]:
    return {"bundle": ("bundle.tar.gz", bundle, "application/gzip")}


def _assert_rejected(resp: httpx.Response, message: str) -> None:
    assert resp.status_code == 400, resp.text
    assert re.search(message, resp.text), resp.text


@pytest_asyncio.fixture(params=["multi_user", "single_user"])
async def posture(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, mock_llm, tmp_path: Path
) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str]]]:
    """A client for the strict multi-user posture or the single-user local server."""
    if request.param == "single_user":
        monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")
        app, headers = request.getfixturevalue("app"), {}
    else:
        # The suite pins the single-user marker; the provider reads it at construction.
        monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER", raising=False)
        app, headers = request.getfixturevalue("auth_app"), _MULTI_USER
    async with _app_client(app, mock_llm, tmp_path) as client:
        yield client, headers


@pytest.mark.parametrize("case", sorted(_CASES))
async def test_agent_install_validates_bundle_contents(
    posture: tuple[httpx.AsyncClient, dict[str, str]], case: str
) -> None:
    client, headers = posture
    _, bundle, message = _CASES[case]
    resp = await client.post("/v1/agents", headers=headers, files=_upload(bundle))
    _assert_rejected(resp, message)
    installed = await client.get("/v1/agents", params={"scope": "user"}, headers=headers)
    assert installed.status_code == 200, installed.text
    assert installed.json()["data"] == []


@pytest.mark.parametrize("case", sorted(_CASES))
async def test_session_create_validates_bundle_contents(
    posture: tuple[httpx.AsyncClient, dict[str, str]], case: str
) -> None:
    client, headers = posture
    _, bundle, message = _CASES[case]
    resp = await post_session_bundle(client.post, "/v1/sessions", bundle, headers=headers)
    _assert_rejected(resp, message)
    sessions = await client.get("/v1/sessions", headers=headers)
    assert sessions.status_code == 200, sessions.text
    assert sessions.json()["data"] == []


@pytest.mark.parametrize("case", sorted(_CASES))
async def test_session_agent_replace_validates_bundle_contents(
    posture: tuple[httpx.AsyncClient, dict[str, str]], case: str
) -> None:
    client, headers = posture
    name, bundle, message = _CASES[case]
    clean = _make_bundle_bytes({"config.yaml": _MIN_CONFIG.format(name=name)})
    created = await post_session_bundle(client.post, "/v1/sessions", clean, headers=headers)
    assert created.status_code == 201, created.text
    session_id, agent_id = created.json()["session_id"], created.json()["agent_id"]
    resp = await client.put(
        f"/v1/sessions/{session_id}/agent", headers=headers, files=_upload(bundle)
    )
    _assert_rejected(resp, message)
    current = await client.get(f"/v1/sessions/{session_id}/agent", headers=headers)
    assert current.status_code == 200, current.text
    assert current.json()["id"] == agent_id
