"""Project-aware session creation defaults and consistency boundaries."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.db.utils import builtin_agent_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes._session_create_validation import (
    resolve_project_session_create,
)
from omnigent.server.schemas import (
    ProjectSessionCreateRequest,
    SessionCreateRequest,
    SessionResponse,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from tests.server.helpers import build_agent_bundle

pytestmark = pytest.mark.asyncio

ALICE = "alice@example.com"
BOB = "bob@example.com"
CUSTOM_AGENT_ID = "187b7cb7ac30abf4debfaa578d052ec6"
OTHER_AGENT_ID = "287b7cb7ac30abf4debfaa578d052ec6"
BUILTIN_AGENT_NAME = "generic-builtin"
BUILTIN_AGENT_ID = builtin_agent_id(BUILTIN_AGENT_NAME)


@pytest.fixture()
def project_create_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_store = SqlAlchemyAgentStore(db_uri)
    agent_store.create(CUSTOM_AGENT_ID, "project-custom", f"{CUSTOM_AGENT_ID}/bundle")
    agent_store.create(OTHER_AGENT_ID, "explicit-custom", f"{OTHER_AGENT_ID}/bundle")
    agent_store.create(
        BUILTIN_AGENT_ID,
        BUILTIN_AGENT_NAME,
        f"{BUILTIN_AGENT_ID}/bundle",
    )
    return create_app(
        agent_store=agent_store,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        project_store=SqlAlchemyProjectStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )


@pytest_asyncio.fixture()
async def project_create_client(project_create_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=project_create_app), base_url="http://test"
    ) as client:
        yield client


def _headers(user: str = ALICE) -> dict[str, str]:
    return {"X-Forwarded-Email": user}


async def _project(
    client: httpx.AsyncClient, config: dict[str, object], *, user: str = ALICE
) -> str:
    response = await client.post(
        "/v1/projects",
        json={"name": f"project-{user}", "config": config},
        headers=_headers(user),
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def test_omitted_values_are_filled_and_membership_is_immediate(
    project_create_client: httpx.AsyncClient,
) -> None:
    project_id = await _project(
        project_create_client,
        {"agent_id": CUSTOM_AGENT_ID, "workspace": "/work/project"},
    )
    response = await project_create_client.post(
        "/v1/sessions", json={"project_id": project_id}, headers=_headers()
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["agent_id"] == CUSTOM_AGENT_ID
    assert body["workspace"] == "/work/project"
    assert body["project_id"] == project_id


async def test_explicit_values_are_not_overridden(
    project_create_client: httpx.AsyncClient,
) -> None:
    project_id = await _project(
        project_create_client,
        {"agent_id": CUSTOM_AGENT_ID, "workspace": "/work/project"},
    )
    response = await project_create_client.post(
        "/v1/sessions",
        json={
            "project_id": project_id,
            "agent_id": OTHER_AGENT_ID,
            "workspace": "/work/project/subdir",
        },
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    assert response.json()["agent_id"] == OTHER_AGENT_ID
    assert response.json()["workspace"] == "/work/project/subdir"


async def test_explicit_null_is_not_defaulted(
    project_create_client: httpx.AsyncClient,
) -> None:
    project_id = await _project(project_create_client, {"agent_id": CUSTOM_AGENT_ID})
    response = await project_create_client.post(
        "/v1/sessions",
        json={"project_id": project_id, "agent_id": None},
        headers=_headers(),
    )
    assert response.status_code == 400
    assert "agent_id is required" in response.text


async def test_unknown_and_unowned_project_are_404(
    project_create_client: httpx.AsyncClient,
) -> None:
    bob_project = await _project(project_create_client, {"agent_id": CUSTOM_AGENT_ID}, user=BOB)
    for project_id in ("0" * 32, bob_project):
        response = await project_create_client.post(
            "/v1/sessions",
            json={"project_id": project_id},
            headers=_headers(),
        )
        assert response.status_code == 404
        assert "Project not found" in response.text


async def test_workspace_mismatch_warns_and_strict_mode_escalates(
    project_create_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = await _project(
        project_create_client,
        {"agent_id": CUSTOM_AGENT_ID, "workspace": "/work/project"},
    )
    payload = {
        "project_id": project_id,
        "agent_id": CUSTOM_AGENT_ID,
        "workspace": "/work/other",
    }
    response = await project_create_client.post("/v1/sessions", json=payload, headers=_headers())
    assert response.status_code == 201, response.text
    assert response.json()["warnings"][0]["code"] == "project_workspace_mismatch"

    monkeypatch.setenv("OMNIGENT_STRICT_PROJECT_SESSION_CREATE", "1")
    response = await project_create_client.post("/v1/sessions", json=payload, headers=_headers())
    assert response.status_code == 400


async def test_builtin_agent_mismatch_warning_surfaces(
    project_create_client: httpx.AsyncClient,
) -> None:
    project_id = await _project(project_create_client, {"agent_id": CUSTOM_AGENT_ID})
    response = await project_create_client.post(
        "/v1/sessions",
        json={"project_id": project_id, "agent_id": BUILTIN_AGENT_ID},
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    assert response.json()["warnings"][0]["code"] == "project_agent_mismatch"


async def test_custom_agent_mismatch_warning_surfaces(
    project_create_client: httpx.AsyncClient,
) -> None:
    """Any explicit agent differing from the pin warns, not just builtins."""
    project_id = await _project(project_create_client, {"agent_id": CUSTOM_AGENT_ID})
    response = await project_create_client.post(
        "/v1/sessions",
        json={"project_id": project_id, "agent_id": OTHER_AGENT_ID},
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    assert response.json()["warnings"][0]["code"] == "project_agent_mismatch"


@pytest.mark.parametrize("strict", [False, True])
async def test_fork_of_mismatched_session_emits_no_warnings(
    project_create_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    strict: bool,
) -> None:
    """Forking a session whose agent mismatches its project stays clean.

    The fork never requested a project — the mismatch belongs to the source
    session — so the fork response gains no ``warnings`` key and strict mode
    must not reject it.
    """
    project_id = await _project(project_create_client, {"agent_id": CUSTOM_AGENT_ID})
    created = await project_create_client.post(
        "/v1/sessions", json={"agent_id": BUILTIN_AGENT_ID}, headers=_headers()
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    moved = await project_create_client.patch(
        f"/v1/sessions/{session_id}",
        json={"project_id": project_id},
        headers=_headers(),
    )
    assert moved.status_code == 200, moved.text
    if strict:
        monkeypatch.setenv("OMNIGENT_STRICT_PROJECT_SESSION_CREATE", "1")
    fork = await project_create_client.post(
        f"/v1/sessions/{session_id}/fork", json={}, headers=_headers()
    )
    assert fork.status_code == 201, fork.text
    body = fork.json()
    assert "warnings" not in body
    assert body["project_id"] == project_id


async def test_without_project_id_response_is_unchanged(
    project_create_client: httpx.AsyncClient,
) -> None:
    response = await project_create_client.post(
        "/v1/sessions", json={"agent_id": CUSTOM_AGENT_ID}, headers=_headers()
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert "warnings" not in body
    # Full response round-trip pins the legacy response projection rather than
    # checking only the fields touched by this feature.
    assert body == SessionResponse.model_validate(body).model_dump(mode="json")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, {"type": "missing", "loc": ["agent_id"], "msg": "Field required"}),
        (
            {"agent_id": None},
            {
                "type": "string_type",
                "loc": ["agent_id"],
                "msg": "Input should be a valid string",
            },
        ),
    ],
)
async def test_without_project_id_retains_legacy_agent_validation_detail(
    project_create_client: httpx.AsyncClient,
    payload: dict[str, object],
    expected: dict[str, object],
) -> None:
    response = await project_create_client.post("/v1/sessions", json=payload, headers=_headers())
    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert {key: error[key] for key in ("type", "loc", "msg")} == expected
    assert "agent_id" in SessionCreateRequest.model_json_schema()["required"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"project_id": None}, {"type": "missing", "loc": ["agent_id"], "msg": "Field required"}),
        (
            {"project_id": None, "agent_id": None},
            {
                "type": "string_type",
                "loc": ["agent_id"],
                "msg": "Input should be a valid string",
            },
        ),
    ],
)
async def test_null_project_id_matches_key_absent_422(
    project_create_client: httpx.AsyncClient,
    payload: dict[str, object],
    expected: dict[str, object],
) -> None:
    """A null project_id body keeps the legacy 422 contract identical.

    Only the ``input`` echo may differ between the two responses: a
    missing-field error echoes the raw request body verbatim, exactly as the
    legacy shape did for the same body.
    """
    key_absent = {key: value for key, value in payload.items() if key != "project_id"}
    absent_response = await project_create_client.post(
        "/v1/sessions", json=key_absent, headers=_headers()
    )
    null_response = await project_create_client.post(
        "/v1/sessions", json=payload, headers=_headers()
    )
    assert absent_response.status_code == 422
    assert null_response.status_code == 422

    def _without_input(detail: list[dict[str, object]]) -> list[dict[str, object]]:
        return [{key: value for key, value in error.items() if key != "input"} for error in detail]

    assert _without_input(null_response.json()["detail"]) == _without_input(
        absent_response.json()["detail"]
    )
    error = null_response.json()["detail"][0]
    assert {key: error[key] for key in ("type", "loc", "msg")} == expected
    # The missing-field echo carries the raw body, matching the legacy shape.
    assert error["input"] == (payload if expected["type"] == "missing" else None)


async def test_null_project_id_with_valid_agent_matches_key_absent_create(
    project_create_client: httpx.AsyncClient,
) -> None:
    """A null project_id create behaves exactly like one without the key."""
    absent_response = await project_create_client.post(
        "/v1/sessions", json={"agent_id": CUSTOM_AGENT_ID}, headers=_headers()
    )
    null_response = await project_create_client.post(
        "/v1/sessions",
        json={"project_id": None, "agent_id": CUSTOM_AGENT_ID},
        headers=_headers(),
    )
    assert absent_response.status_code == 201, absent_response.text
    assert null_response.status_code == 201, null_response.text
    body = null_response.json()
    assert "warnings" not in body
    assert body["project_id"] is None
    volatile = {"id", "created_at", "updated_at", "root_conversation_id"}
    assert {key: value for key, value in body.items() if key not in volatile} == {
        key: value for key, value in absent_response.json().items() if key not in volatile
    }


@pytest.mark.parametrize(
    ("raw_body", "input_value"),
    [("null", None), ("5", 5), ('"project_id"', "project_id")],
)
async def test_non_object_json_retains_exact_legacy_422(
    project_create_client: httpx.AsyncClient,
    raw_body: str,
    input_value: object,
) -> None:
    response = await project_create_client.post(
        "/v1/sessions",
        content=raw_body,
        headers={**_headers(), "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == [
        {
            "type": "model_type",
            "loc": [],
            "msg": "Input should be a valid dictionary or instance of SessionCreateRequest",
            "input": input_value,
            "url": "https://errors.pydantic.dev/2.13/v/model_type",
        }
    ]


async def test_legacy_multi_error_order_starts_with_agent_id(
    project_create_client: httpx.AsyncClient,
) -> None:
    response = await project_create_client.post(
        "/v1/sessions",
        json={"agent_id": None, "labels": "not-a-dict"},
        headers=_headers(),
    )
    assert response.status_code == 422
    projected = [
        {key: error[key] for key in ("type", "loc", "msg")} for error in response.json()["detail"]
    ]
    assert projected == [
        {
            "type": "string_type",
            "loc": ["agent_id"],
            "msg": "Input should be a valid string",
        },
        {
            "type": "dict_type",
            "loc": ["labels"],
            "msg": "Input should be a valid dictionary",
        },
    ]


@pytest.mark.parametrize("config_field", [("workspace", 123), ("git", "yes")])
async def test_malformed_project_config_is_structured_400(
    project_create_client: httpx.AsyncClient,
    config_field: tuple[str, object],
) -> None:
    field, value = config_field
    project_id = await _project(
        project_create_client,
        {"agent_id": CUSTOM_AGENT_ID, field: value},
    )
    response = await project_create_client.post(
        "/v1/sessions", json={"project_id": project_id}, headers=_headers()
    )
    assert response.status_code == 400
    assert f"Invalid project config field '{field}'" in response.text


async def test_explicit_null_workspace_is_not_defaulted(
    project_create_client: httpx.AsyncClient,
) -> None:
    project_id = await _project(
        project_create_client,
        {"agent_id": CUSTOM_AGENT_ID, "workspace": "/work/project"},
    )
    response = await project_create_client.post(
        "/v1/sessions",
        json={"project_id": project_id, "workspace": None},
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    assert response.json()["workspace"] is None


async def test_git_default_fill_and_workspace_parent_traversal_warning(db_uri: str) -> None:
    project_store = SqlAlchemyProjectStore(db_uri)
    project = project_store.create(
        "487b7cb7ac30abf4debfaa578d052ec6",
        "git-defaults",
        ALICE,
        {
            "agent_id": CUSTOM_AGENT_ID,
            "workspace": "/work/project",
            "git": {"branch_name": "feature/project"},
        },
    )
    resolved = await resolve_project_session_create(
        body=ProjectSessionCreateRequest(
            project_id=project.id,
            host_id="host_abc",
            workspace="/work/project/../other",
        ),
        user_id=ALICE,
        project_store=project_store,
    )
    assert resolved.body.git is not None
    assert resolved.body.git.branch_name == "feature/project"
    assert resolved.warnings[0]["code"] == "project_workspace_mismatch"


async def test_multipart_create_defaults_workspace_and_files_atomically(
    project_create_client: httpx.AsyncClient,
) -> None:
    project_id = await _project(project_create_client, {"workspace": "/work/upload"})
    response = await project_create_client.post(
        "/v1/sessions",
        data={"metadata": f'{{"project_id":"{project_id}"}}'},
        files={
            "bundle": (
                "agent.tar.gz",
                build_agent_bundle(name="project-upload"),
                "application/gzip",
            )
        },
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    session = await project_create_client.get(
        f"/v1/sessions/{response.json()['session_id']}", headers=_headers()
    )
    assert session.status_code == 200, session.text
    assert session.json()["workspace"] == "/work/upload"
    assert session.json()["project_id"] == project_id


async def test_multipart_malformed_project_config_is_structured_400(
    project_create_client: httpx.AsyncClient,
) -> None:
    project_id = await _project(project_create_client, {"workspace": 123})
    response = await project_create_client.post(
        "/v1/sessions",
        data={"metadata": f'{{"project_id":"{project_id}"}}'},
        files={
            "bundle": (
                "agent.tar.gz",
                build_agent_bundle(name="malformed-project-upload"),
                "application/gzip",
            )
        },
        headers=_headers(),
    )
    assert response.status_code == 400
    assert "Invalid project config field 'workspace'" in response.text


async def test_shared_chokepoint_is_reusable_by_non_route_creators(
    db_uri: str,
) -> None:
    """Non-route creators can pass their create body through the same resolver."""
    project_store = SqlAlchemyProjectStore(db_uri)
    project = project_store.create(
        "387b7cb7ac30abf4debfaa578d052ec6",
        "scheduled",
        ALICE,
        {"agent_id": CUSTOM_AGENT_ID, "workspace": "/scheduled"},
    )
    resolved = await resolve_project_session_create(
        body=ProjectSessionCreateRequest(project_id=project.id),
        user_id=ALICE,
        project_store=project_store,
    )
    assert resolved.body.agent_id == CUSTOM_AGENT_ID
    assert resolved.body.workspace == "/scheduled"


def _import_payload(external_session_id: str, **extra: object) -> dict[str, object]:
    return {
        "source": "claude",
        "external_session_id": external_session_id,
        "items": [
            {
                "type": "message",
                "response_id": "claude:turn-1",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            }
        ],
        **extra,
    }


def _seed_claude_import_agent(db_uri: str) -> None:
    SqlAlchemyAgentStore(db_uri).create(
        builtin_agent_id("claude-native-ui"),
        name="claude-native-ui",
        bundle_location="builtin://claude-native-ui",
    )


async def test_import_with_project_defaults_workspace_and_files_session(
    project_create_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """An import naming a project consumes the resolver's decisions for real."""
    _seed_claude_import_agent(db_uri)
    project_id = await _project(project_create_client, {"workspace": "/work/import"})
    response = await project_create_client.post(
        "/v1/imports",
        json=_import_payload("project-import-1", project_id=project_id),
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    session = await project_create_client.get(
        f"/v1/sessions/{response.json()['session_id']}", headers=_headers()
    )
    assert session.status_code == 200, session.text
    assert session.json()["workspace"] == "/work/import"
    assert session.json()["project_id"] == project_id


async def test_import_with_unowned_or_unknown_project_is_404(
    project_create_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    _seed_claude_import_agent(db_uri)
    bob_project = await _project(project_create_client, {"agent_id": CUSTOM_AGENT_ID}, user=BOB)
    for project_id in ("0" * 32, bob_project):
        response = await project_create_client.post(
            "/v1/imports",
            json=_import_payload("project-import-denied", project_id=project_id),
            headers=_headers(),
        )
        assert response.status_code == 404, response.text
        assert "Project not found" in response.text
