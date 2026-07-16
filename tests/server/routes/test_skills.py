from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.server.routes.skills import create_skills_router


class _Response:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _RunnerClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str] | None]] = []

    async def get(
        self,
        path: str,
        *,
        params: dict[str, str] | None,
        timeout: float,
    ) -> _Response:
        del timeout
        self.requests.append((path, params))
        include_other_tools = params == {"include_other_tools": "true"}
        if path.endswith("/skills/catalog"):
            return _Response(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "codex-only:abc",
                            "name": "codex-only",
                            "description": "codex-only description",
                            "origin": "personal",
                            "enabled": True,
                            "available": True,
                            "has_conflict": False,
                            "updated_at": None,
                        }
                    ]
                    if include_other_tools
                    else [],
                    "include_other_tools": include_other_tools,
                    "hidden_count": 0 if include_other_tools else 1,
                },
            )
        if path.endswith("/skills/catalog/codex-only%3Aabc") and include_other_tools:
            return _Response(
                200,
                {
                    "id": "codex-only:abc",
                    "name": "codex-only",
                    "content": "runner-owned content",
                    "provenance": {"original_path": "/runner/home/.codex/skills/codex-only"},
                },
            )
        return _Response(404, {"detail": "Skill not found"})


class _RunnerRouter:
    def __init__(self, client: _RunnerClient) -> None:
        self.client = client
        self.session_ids: list[str] = []

    def client_for_session_resources(self, session_id: str):  # type: ignore[no-untyped-def]
        self.session_ids.append(session_id)
        return type("Routed", (), {"client": self.client})()


def test_catalog_requires_explicit_session_context() -> None:
    runner_client = _RunnerClient()
    app = FastAPI()
    app.include_router(
        create_skills_router(object(), runner_router=_RunnerRouter(runner_client)),  # type: ignore[arg-type]
        prefix="/v1",
    )
    client = TestClient(app)

    response = client.get("/v1/skills")

    assert response.status_code == 422
    assert runner_client.requests == []


def test_catalog_proxies_runner_context_and_preserves_visibility_override() -> None:
    runner_client = _RunnerClient()
    runner_router = _RunnerRouter(runner_client)
    app = FastAPI()
    app.include_router(
        create_skills_router(object(), runner_router=runner_router),  # type: ignore[arg-type]
        prefix="/v1",
    )
    client = TestClient(app)

    listing = client.get(
        "/v1/skills",
        params={"session_id": "conv/remote", "include_other_tools": "true"},
    )
    assert listing.status_code == 200
    skill_id = listing.json()["data"][0]["id"]
    detail = client.get(
        f"/v1/skills/{skill_id}",
        params={"session_id": "conv/remote", "include_other_tools": "true"},
    )

    assert detail.status_code == 200
    assert detail.json()["content"] == "runner-owned content"
    assert runner_router.session_ids == ["conv/remote", "conv/remote"]
    assert runner_client.requests == [
        (
            "/v1/sessions/conv%2Fremote/skills/catalog",
            {"include_other_tools": "true"},
        ),
        (
            "/v1/sessions/conv%2Fremote/skills/catalog/codex-only%3Aabc",
            {"include_other_tools": "true"},
        ),
    ]


def test_catalog_detail_uses_persisted_visibility_when_override_is_omitted() -> None:
    runner_client = _RunnerClient()
    app = FastAPI()
    app.include_router(
        create_skills_router(object(), runner_router=_RunnerRouter(runner_client)),  # type: ignore[arg-type]
        prefix="/v1",
    )
    client = TestClient(app)

    response = client.get(
        "/v1/skills/codex-only:abc",
        params={"session_id": "conv_remote"},
    )

    assert response.status_code == 404
    assert runner_client.requests == [
        ("/v1/sessions/conv_remote/skills/catalog/codex-only%3Aabc", None)
    ]


def test_trust_routes_remain_server_persisted(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(
        create_skills_router(object(), runner_router=_RunnerRouter(_RunnerClient())),  # type: ignore[arg-type]
        prefix="/v1",
    )
    client = TestClient(app)

    assert client.get("/v1/skills/trust").json() == {
        "value": "current",
        "include_other_tools": False,
    }
    assert client.put("/v1/skills/trust", json={"value": "all-host"}).json() == {
        "value": "all-host",
        "include_other_tools": True,
    }
