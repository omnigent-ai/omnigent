"""Databricks SDK integration tests for the Managed Memory REST client."""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import parse_qs, urlsplit

import pytest
from databricks.sdk.errors import NotFound, PermissionDenied, Unauthenticated

from omnigent.stores.memory_store.databricks_client import (
    DatabricksManagedMemoryClient,
)

pytestmark = pytest.mark.databricks


class _ManagedMemoryServer(ThreadingHTTPServer):
    """Local OAuth issuer and Managed Memory API recorder."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _ManagedMemoryHandler)
        self.managed_requests: list[dict[str, object]] = []
        self.token_requests: list[dict[str, object]] = []
        self.token_expires_in = 3600
        self.token_status = 200
        self.managed_status = 200
        self.managed_error_code = "PERMISSION_DENIED"
        self.managed_error_message = "credential lacks access to the memory store"

    @property
    def base_url(self) -> str:
        return f"http://{self.server_address[0]}:{self.server_address[1]}"


class _ManagedMemoryHandler(BaseHTTPRequestHandler):
    @property
    def managed_server(self) -> _ManagedMemoryServer:
        return cast(_ManagedMemoryServer, self.server)

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _request_body(self) -> object | None:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return None
        return json.loads(self.rfile.read(length))

    def _record_managed_request(self) -> None:
        parsed = urlsplit(self.path)
        self.managed_server.managed_requests.append(
            {
                "method": self.command,
                "path": parsed.path,
                "query": parse_qs(parsed.query, keep_blank_values=True),
                "authorization": self.headers.get("Authorization"),
                "user_agent": self.headers.get("User-Agent"),
                "body": self._request_body(),
            }
        )
        if self.managed_server.managed_status == 200:
            self._send_json(200, {"ok": True})
        else:
            self._send_json(
                self.managed_server.managed_status,
                {
                    "error_code": self.managed_server.managed_error_code,
                    "message": self.managed_server.managed_error_message,
                },
            )

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/.well-known/databricks-config":
            self._send_json(
                200,
                {
                    "oidc_endpoint": f"{self.managed_server.base_url}/oidc",
                    "workspace_id": "1234567890",
                },
            )
            return
        if path == "/oidc/.well-known/oauth-authorization-server":
            self._send_json(
                200,
                {
                    "authorization_endpoint": f"{self.managed_server.base_url}/oidc/authorize",
                    "token_endpoint": f"{self.managed_server.base_url}/oidc/token",
                },
            )
            return
        self._record_managed_request()

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/oidc/token":
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode(), keep_blank_values=True)
            self.managed_server.token_requests.append(
                {
                    "form": form,
                    "authorization": self.headers.get("Authorization"),
                }
            )
            if self.managed_server.token_status != 200:
                self._send_json(
                    self.managed_server.token_status,
                    {
                        "error": "invalid_client",
                        "error_description": "bad client credentials",
                    },
                )
                return
            token_number = len(self.managed_server.token_requests)
            self._send_json(
                200,
                {
                    "access_token": f"token-{token_number}",
                    "token_type": "Bearer",
                    "expires_in": self.managed_server.token_expires_in,
                },
            )
            return
        self._record_managed_request()

    def do_PATCH(self) -> None:
        self._record_managed_request()

    def do_DELETE(self) -> None:
        self._record_managed_request()


@pytest.fixture()
def managed_memory_server() -> Iterator[_ManagedMemoryServer]:
    server = _ManagedMemoryServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _m2m_client(
    server: _ManagedMemoryServer,
    monkeypatch: pytest.MonkeyPatch,
    *,
    store_id: str = "support-memory",
) -> DatabricksManagedMemoryClient:
    for name in (
        "DATABRICKS_AUTH_TYPE",
        "DATABRICKS_CONFIG_PROFILE",
        "DATABRICKS_OIDC_TOKEN",
        "DATABRICKS_OIDC_TOKEN_ENV",
        "DATABRICKS_OIDC_TOKEN_FILEPATH",
        "DATABRICKS_TOKEN",
        "DATABRICKS_USERNAME",
        "DATABRICKS_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "memory-service")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("DATABRICKS_DISABLE_ASYNC_TOKEN_REFRESH", "true")
    return DatabricksManagedMemoryClient(
        store_id,
        workspace_host=server.base_url,
    )


def test_real_sdk_m2m_refreshes_expired_credentials_for_each_request(
    managed_memory_server: _ManagedMemoryServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_memory_server.token_expires_in = 0
    client = _m2m_client(managed_memory_server, monkeypatch)

    client.get_store()
    client.get_store()

    expected_basic_auth = "Basic " + base64.b64encode(b"memory-service:test-secret").decode()
    assert managed_memory_server.token_requests == [
        {
            "form": {"grant_type": ["client_credentials"], "scope": ["all-apis"]},
            "authorization": expected_basic_auth,
        },
        {
            "form": {"grant_type": ["client_credentials"], "scope": ["all-apis"]},
            "authorization": expected_basic_auth,
        },
    ]
    assert [request["authorization"] for request in managed_memory_server.managed_requests] == [
        "Bearer token-1",
        "Bearer token-2",
    ]


def test_real_sdk_exchanges_workload_identity_without_a_client_secret(
    managed_memory_server: _ManagedMemoryServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "DATABRICKS_CONFIG_PROFILE",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_TOKEN",
        "DATABRICKS_USERNAME",
        "DATABRICKS_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "memory-service")
    monkeypatch.setenv("DATABRICKS_OIDC_TOKEN", "workload-jwt")
    monkeypatch.setenv("DATABRICKS_DISABLE_ASYNC_TOKEN_REFRESH", "true")
    client = DatabricksManagedMemoryClient(
        "support-memory",
        workspace_host=managed_memory_server.base_url,
        auth_type="env-oidc",
    )

    client.get_store()

    assert managed_memory_server.token_requests == [
        {
            "form": {
                "client_id": ["memory-service"],
                "grant_type": ["urn:ietf:params:oauth:grant-type:token-exchange"],
                "scope": ["all-apis"],
                "subject_token": ["workload-jwt"],
                "subject_token_type": ["urn:ietf:params:oauth:token-type:jwt"],
            },
            "authorization": None,
        }
    ]
    assert managed_memory_server.managed_requests[0]["authorization"] == "Bearer token-1"


def test_real_sdk_preserves_canonical_http_shapes_and_encodes_resource_ids(
    managed_memory_server: _ManagedMemoryServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _m2m_client(
        managed_memory_server,
        monkeypatch,
        store_id="support/memory",
    )

    client.get_effective_permission()
    client.create_entry(
        {"actor_id": "alice@example.test", "path": "/preferences", "content": "Concise"},
        entry_id="entry one/part",
    )
    client.get_entry("entry one/part", view="MANAGED_MEMORY_ENTRY_VIEW_BASIC")
    client.list_entries(
        "alice@example.test",
        path="/preferences",
        page_size=25,
        page_token="next token",
        view="MANAGED_MEMORY_ENTRY_VIEW_BASIC",
    )
    client.search_entries(
        "alice@example.test",
        "response style",
        path_prefix="/preferences",
        page_size=10,
    )
    client.update_entry(
        "entry one/part",
        {"content": "Very concise", "description": "Response style"},
        update_mask="content,description",
    )
    client.delete_entry("entry one/part")

    base = "/api/2.0/agents/memory-stores/support%2Fmemory"
    entry_path = f"{base}/entries/entry%20one%2Fpart"
    requests = managed_memory_server.managed_requests
    assert [(request["method"], request["path"]) for request in requests] == [
        ("GET", f"{base}/permissions/self"),
        ("POST", f"{base}/entries"),
        ("GET", entry_path),
        ("GET", f"{base}/entries"),
        ("POST", f"{base}/entries:search"),
        ("PATCH", entry_path),
        ("DELETE", entry_path),
    ]
    assert requests[1]["query"] == {"managed_memory_entry_id": ["entry one/part"]}
    assert requests[1]["body"] == {
        "actor_id": "alice@example.test",
        "path": "/preferences",
        "content": "Concise",
    }
    assert requests[3]["query"] == {
        "actor_id": ["alice@example.test"],
        "path": ["/preferences"],
        "page_size": ["25"],
        "page_token": ["next token"],
        "view": ["MANAGED_MEMORY_ENTRY_VIEW_BASIC"],
    }
    assert requests[4]["body"] == {
        "actor_id": "alice@example.test",
        "query": "response style",
        "path_prefix": "/preferences",
        "page_size": 10,
    }
    assert requests[5]["query"] == {"update_mask": ["content,description"]}
    assert requests[5]["body"] == {
        "content": "Very concise",
        "description": "Response style",
    }
    assert all(request["authorization"] == "Bearer token-1" for request in requests)
    assert all("omnigent/" in str(request["user_agent"]) for request in requests)


@pytest.mark.parametrize(
    ("status", "error_code", "exception_type"),
    [
        (401, "UNAUTHENTICATED", Unauthenticated),
        (403, "PERMISSION_DENIED", PermissionDenied),
        (404, "NOT_FOUND", NotFound),
    ],
)
def test_real_sdk_api_errors_remain_typed_and_explicit(
    managed_memory_server: _ManagedMemoryServer,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    error_code: str,
    exception_type: type[Exception],
) -> None:
    managed_memory_server.managed_status = status
    managed_memory_server.managed_error_code = error_code
    managed_memory_server.managed_error_message = "forced memory-store failure"
    client = _m2m_client(managed_memory_server, monkeypatch)

    with pytest.raises(exception_type, match="forced memory-store failure"):
        client.get_store()

    assert len(managed_memory_server.managed_requests) == 1


def test_real_sdk_authentication_failure_stops_before_memory_request(
    managed_memory_server: _ManagedMemoryServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_memory_server.token_status = 401
    client = _m2m_client(managed_memory_server, monkeypatch)

    with pytest.raises(ValueError, match="invalid_client: bad client credentials"):
        client.get_store()

    assert len(managed_memory_server.token_requests) == 1
    assert managed_memory_server.managed_requests == []
