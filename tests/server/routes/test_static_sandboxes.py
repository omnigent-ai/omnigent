"""``/v1/static-sandboxes``: pass-through to the provider, plus host stop and wake."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.server import vm_hosts
from omnigent.server.routes.static_sandboxes import create_static_sandboxes_router
from omnigent.stores.host_store import HostStore
from tests.server.static_sandbox_helpers import OWNER, WS, VmShim, make_deployment, make_shim

_HEADERS = {"x-user": OWNER}


class _HeaderAuth:
    def get_user_id(self, request: Request) -> str | None:
        return request.headers.get("x-user")


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    vm_hosts._lookup_cache.clear()
    vm_hosts._providers_cache.clear()


@pytest.fixture()
def host_store(db_uri: str) -> HostStore:
    return HostStore(db_uri)


@pytest.fixture()
def shim(host_store: HostStore) -> VmShim:
    return make_shim(host_store)


@pytest.fixture()
def client(shim: VmShim, host_store: HostStore) -> TestClient:
    app = FastAPI()
    app.state.sandbox_config = make_deployment(shim)
    app.state.host_store = host_store

    @app.exception_handler(OmnigentError)
    async def _omnigent_error(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=exc.http_status)

    app.include_router(
        create_static_sandboxes_router(auth_provider=_HeaderAuth()),  # type: ignore[arg-type]
        prefix="/v1",
    )
    return TestClient(app)


def test_lists_saved_entries_with_status(client: TestClient) -> None:
    resp = client.get("/v1/static-sandboxes", headers=_HEADERS)
    assert resp.status_code == 200
    [entry] = resp.json()["static_sandboxes"]
    assert set(resp.json()) == {"static_sandboxes"}
    assert entry["provider"] == "remote"
    assert entry["sandbox_id"] == WS
    assert entry["label"] == "Checkout web app"
    assert entry["status"] == "running"
    # The host link is provider's bookkeeping, not part of the public shape.
    assert "omnigent_host_id" not in entry


def test_available_lists_every_vm_for_the_picker(client: TestClient, shim: VmShim) -> None:
    shim.vms["android-build"] = "paused"
    resp = client.get("/v1/static-sandboxes/available", headers=_HEADERS)
    available = resp.json()["available_sandboxes"]
    assert [vm["sandbox_id"] for vm in available] == [WS, "android-build"]
    assert {vm["provider"] for vm in available} == {"remote"}


def test_put_adds_and_renames_with_a_trimmed_label(client: TestClient, shim: VmShim) -> None:
    shim.vms["bar"] = "paused"
    resp = client.put(
        "/v1/static-sandboxes/remote/bar", json={"label": "  My   VM "}, headers=_HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["label"] == "My VM"
    resp = client.put("/v1/static-sandboxes/remote/bar", json={"label": ""}, headers=_HEADERS)
    assert resp.json()["label"] == "bar"
    # Saving an entry starts nothing on the VM.
    assert shim.started_tokens == []


def test_a_provider_without_static_sandboxes_is_refused(client: TestClient, shim: VmShim) -> None:
    shim.vms["bar"] = "paused"
    resp = client.put("/v1/static-sandboxes/modal/bar", json={"label": "x"}, headers=_HEADERS)
    assert resp.status_code == 400
    assert "does not offer static sandboxes" in resp.json()["detail"]
    assert client.post("/v1/static-sandboxes/modal/bar/wake", headers=_HEADERS).status_code == 400
    assert client.delete("/v1/static-sandboxes/modal/bar", headers=_HEADERS).status_code == 400
    assert "bar" not in shim.static_labels


def test_lists_nothing_when_the_service_drops_the_capability(
    client: TestClient, shim: VmShim
) -> None:
    del shim.declared_capabilities["static_sandboxes"]
    resp = client.get("/v1/static-sandboxes", headers=_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"static_sandboxes": []}


def test_put_on_a_vm_the_caller_does_not_own_is_404(client: TestClient) -> None:
    resp = client.put(
        "/v1/static-sandboxes/remote/someone-else", json={"label": "x"}, headers=_HEADERS
    )
    assert resp.status_code == 404


def test_an_unprovisioned_shim_is_a_clear_503(client: TestClient, shim: VmShim) -> None:
    shim.static_status = 503
    resp = client.get("/v1/static-sandboxes", headers=_HEADERS)
    assert resp.status_code == 503
    assert "not available" in resp.json()["detail"]


def test_wake_starts_the_host(client: TestClient, shim: VmShim, host_store: HostStore) -> None:
    shim.vms[WS] = "paused"
    resp = client.post(f"/v1/static-sandboxes/remote/{WS}/wake", headers=_HEADERS)
    assert resp.status_code == 200
    host_id = resp.json()["host_id"]
    assert resp.json() == {"provider": "remote", "sandbox_id": WS, "host_id": host_id}
    assert host_store.is_online(host_id)
    assert f"/v1/static-sandboxes/{WS}/resume" in shim.paths("POST")


def test_delete_stops_the_host_and_never_the_vm(
    client: TestClient, shim: VmShim, host_store: HostStore
) -> None:
    host_id = client.post(f"/v1/static-sandboxes/remote/{WS}/wake", headers=_HEADERS).json()[
        "host_id"
    ]
    resp = client.delete(f"/v1/static-sandboxes/remote/{WS}", headers=_HEADERS)
    assert resp.status_code == 204
    assert WS not in shim.static_labels
    assert host_store.resolve_launch_token(host_id, shim.started_tokens[-1]) is None
    assert shim.deletes_of_vms() == []
    assert client.delete(f"/v1/static-sandboxes/remote/{WS}", headers=_HEADERS).status_code == 404


def test_requires_a_user(client: TestClient) -> None:
    assert client.get("/v1/static-sandboxes").status_code == 401
