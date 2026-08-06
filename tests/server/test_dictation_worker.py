"""Tests for standalone dictation worker health, auth, and warmup."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from omnigent.server.dictation import WORKER_TOKEN_ENV, FakeDictationEngine
from omnigent.server.dictation_worker import create_worker_app

_AUTH = {"Authorization": "Bearer worker-secret"}


def test_health_public_ready_token_protected() -> None:
    with TestClient(
        create_worker_app(engine_provider=FakeDictationEngine, shared_token="worker-secret")
    ) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/ready").status_code == 401
        assert client.get("/ready", headers=_AUTH).json() == {"status": "ready"}


def test_worker_token_from_environment_is_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WORKER_TOKEN_ENV, "  worker-secret\n")
    with TestClient(create_worker_app(engine_provider=FakeDictationEngine)) as client:
        for _ in range(100):
            response = client.get("/ready", headers=_AUTH)
            if response.status_code == 200:
                break
        assert response.json() == {"status": "ready"}


def test_ready_reports_warming_then_ready() -> None:
    release = threading.Event()

    def slow_engine() -> FakeDictationEngine:
        release.wait(timeout=5)
        return FakeDictationEngine()

    with TestClient(
        create_worker_app(engine_provider=slow_engine, shared_token="worker-secret")
    ) as client:
        response = client.get("/ready", headers=_AUTH)
        assert response.status_code == 503
        assert response.json()["detail"] == {"status": "warming"}
        release.set()
        for _ in range(100):
            response = client.get("/ready", headers=_AUTH)
            if response.status_code == 200:
                break
        assert response.json() == {"status": "ready"}


def test_ready_sanitizes_warmup_failure() -> None:
    def broken_engine() -> FakeDictationEngine:
        raise RuntimeError("secret model path /private/models/token")

    with TestClient(
        create_worker_app(engine_provider=broken_engine, shared_token="worker-secret")
    ) as client:
        for _ in range(100):
            response = client.get("/ready", headers=_AUTH)
            if response.json().get("detail", {}).get("status") == "failed":
                break
        assert response.status_code == 503
        assert response.json() == {"detail": {"status": "failed"}}
        assert "/private/models" not in response.text
