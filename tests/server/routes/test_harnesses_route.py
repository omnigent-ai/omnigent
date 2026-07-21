from __future__ import annotations

from fastapi import FastAPI
from starlette.testclient import TestClient

from omnigent.server.routes.harnesses import create_harnesses_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(create_harnesses_router(), prefix="/v1")
    return TestClient(app)


def test_harnesses_route_preserves_catalog_without_dismissed_config(monkeypatch) -> None:
    monkeypatch.setattr("omnigent.harness_visibility.load_global_config", dict)

    response = _client().get("/v1/harnesses")

    assert response.status_code == 200
    ids = {row["id"] for row in response.json()["data"]}
    assert "claude-sdk" in ids
    assert "codex" in ids


def test_harnesses_route_filters_dismissed_harnesses(monkeypatch) -> None:
    monkeypatch.setattr(
        "omnigent.harness_visibility.load_global_config",
        lambda: {"dismissed_harnesses": ["claude-sdk", "not-a-harness"]},
    )

    response = _client().get("/v1/harnesses")

    assert response.status_code == 200
    ids = {row["id"] for row in response.json()["data"]}
    assert "claude-sdk" not in ids
    assert "codex" in ids
