"""Tests for the work-item intake webhook (/v1/work-items/intake/{source})."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.server.routes.work_items import create_work_items_router
from omnigent.stores.work_item_store.sqlalchemy_store import SqlAlchemyWorkItemStore


@pytest.fixture()
def client(db_uri: str) -> TestClient:
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.code})

    app.include_router(create_work_items_router(SqlAlchemyWorkItemStore(db_uri)), prefix="/v1")
    return TestClient(app)


def _gh_headers(secret: str, body: bytes) -> dict[str, str]:
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


def test_github_intake_creates_and_is_idempotent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_GITHUB_WEBHOOK_SECRET", "ghs")
    payload = {
        "repository": {"full_name": "acme/app"},
        "issue": {"number": 42, "title": "Bug: deploy fails", "body": "details"},
    }
    raw = json.dumps(payload).encode()

    res = client.post("/v1/work-items/intake/github", content=raw, headers=_gh_headers("ghs", raw))
    assert res.status_code == 200
    out = res.json()
    assert out["created"] is True
    assert out["source"] == "github"
    assert out["external_id"] == "acme/app#42"
    assert out["title"] == "Bug: deploy fails"

    # Same event again → idempotent (no duplicate).
    again = client.post(
        "/v1/work-items/intake/github", content=raw, headers=_gh_headers("ghs", raw)
    )
    assert again.json()["created"] is False
    assert again.json()["id"] == out["id"]


def test_github_intake_rejects_bad_signature(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_GITHUB_WEBHOOK_SECRET", "ghs")
    raw = b"{}"
    res = client.post(
        "/v1/work-items/intake/github", content=raw, headers=_gh_headers("WRONG", raw)
    )
    assert res.status_code == 401


def test_intake_not_configured_is_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_GITHUB_WEBHOOK_SECRET", raising=False)
    res = client.post("/v1/work-items/intake/github", content=b"{}")
    assert res.status_code == 404


def test_generic_bearer_intake(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_INTAKE_SECRET", "tok")
    raw = json.dumps({"title": "Manual task", "external_id": "x1"}).encode()
    ok = client.post(
        "/v1/work-items/intake/generic",
        content=raw,
        headers={"Authorization": "Bearer tok", "Content-Type": "application/json"},
    )
    assert ok.status_code == 200
    assert ok.json()["created"] is True
    assert ok.json()["source"] == "manual"  # "generic" maps to manual

    bad = client.post(
        "/v1/work-items/intake/generic",
        content=raw,
        headers={"Authorization": "Bearer nope"},
    )
    assert bad.status_code == 401


def test_unknown_source_is_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_INTAKE_SECRET", "tok")
    res = client.post(
        "/v1/work-items/intake/telepathy",
        content=b"{}",
        headers={"Authorization": "Bearer tok"},
    )
    assert res.status_code == 400
