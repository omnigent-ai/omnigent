"""Methods the schema declares but nothing serves yet stay unreachable everywhere a call arrives.

A client built against the schema may send them; the host's tunnel, its direct socket and the
server's route each refuse them without work, and no GetCapabilities answer lists them.
"""

from __future__ import annotations

import socket
from pathlib import Path

import httpx
import pytest
from connectrpc.code import Code

from omnigent.api.workspace.v1alpha1 import workspace_pb2 as pb
from omnigent.host import workspace_service
from omnigent.host.frames import HostWorkspaceCallFrame
from omnigent.host.workspace_methods import DECLARED_NOT_SERVED, WORKSPACE_METHODS
from omnigent.host.workspace_pull_requests import PULL_REQUEST_OPS
from omnigent.host.workspace_service import start_workspace_socket
from omnigent.host.workspace_tokens import mint_token
from omnigent.server.workspace_access import METHOD_GATES, gate_for
from omnigent.workspace_fs import WorkspaceReader
from tests._helpers.connect_stream import connect_frame, end_of_stream_code
from tests.host import test_workspace_api_pull_requests as server_tests
from tests.host import test_workspace_pull_requests as host_tests
from tests.host import test_workspace_service as service_tests
from tests.host.test_workspace_api_pull_requests import host_calls as host_calls
from tests.host.test_workspace_pull_requests import provider_reads as provider_reads

_NOT_SERVED = sorted(DECLARED_NOT_SERVED)


def _request(name: str) -> bytes:
    """A request for ``name`` naming a pull request where its message has the field."""
    method = pb.DESCRIPTOR.services_by_name["WorkspaceService"].methods_by_name[name]
    request = pb.__dict__[method.input_type.name]()
    if "pr_url" in method.input_type.fields_by_name:
        request.pr_url = host_tests._PR
    return request.SerializeToString()


def _streams(name: str) -> bool:
    method = pb.DESCRIPTOR.services_by_name["WorkspaceService"].methods_by_name[name]
    return method.server_streaming


@pytest.fixture(autouse=True)
def _own_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))


def test_the_schema_is_the_served_table_and_the_declared_not_served_ones() -> None:
    declared = {
        method.name for method in pb.DESCRIPTOR.services_by_name["WorkspaceService"].methods
    }
    assert declared >= DECLARED_NOT_SERVED
    assert not DECLARED_NOT_SERVED & set(WORKSPACE_METHODS)
    assert declared == set(WORKSPACE_METHODS) | DECLARED_NOT_SERVED


def test_no_host_op_or_server_gate_serves_them() -> None:
    assert not DECLARED_NOT_SERVED & set(PULL_REQUEST_OPS)
    assert not DECLARED_NOT_SERVED & set(workspace_service._SERVED)
    assert not DECLARED_NOT_SERVED & set(METHOD_GATES)
    assert all(gate_for(name) is None for name in DECLARED_NOT_SERVED)


async def test_the_tunnel_lists_none_and_refuses_each_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_reads: list[str | None]
) -> None:
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    host_tests._record(host_tests._SESSION)

    listed, _, _ = await host_tests._answer(host_tests._frame(tmp_path, "GetCapabilities"))
    assert not DECLARED_NOT_SERVED & host_tests._capabilities(listed)
    for name in _NOT_SERVED:
        _messages, code, _ = await host_tests._answer(
            host_tests._frame(tmp_path, name, _request(name))
        )
        assert code == Code.UNIMPLEMENTED.value, name

    assert provider_reads == []


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="needs Unix sockets")
async def test_the_direct_socket_lists_none_and_refuses_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider_reads: list[str | None]
) -> None:
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    opened: list[str] = []

    def reader_for(workspace: str) -> WorkspaceReader:
        opened.append(workspace)
        return WorkspaceReader(Path(workspace))

    listener = await start_workspace_socket(reader_for, host_id="host_a")
    assert listener is not None
    token, _ = mint_token(
        listener.token_key,
        host_id="host_a",
        session_id=host_tests._SESSION,
        workspace=str(tmp_path),
    )
    headers = {"Authorization": f"Bearer {token}"}
    service = "/omnigent.api.workspace.v1alpha1.WorkspaceService"
    try:
        transport = httpx.AsyncHTTPTransport(uds=str(listener.path))
        async with httpx.AsyncClient(transport=transport, base_url="http://workspace") as http:
            listed = await http.post(f"{service}/GetCapabilities", json={}, headers=headers)
            assert listed.status_code == 200, listed.text
            assert not DECLARED_NOT_SERVED & set(listed.json()["capabilities"])
            for name in _NOT_SERVED:
                if _streams(name):
                    response = await http.post(
                        f"{service}/{name}",
                        content=connect_frame(_request(name)),
                        headers={**headers, "Content-Type": "application/connect+proto"},
                    )
                    assert response.status_code == 200, (name, response.text)
                    assert end_of_stream_code(response.content) == "unimplemented", name
                    continue
                response = await http.post(
                    f"{service}/{name}",
                    content=_request(name),
                    headers={**headers, "Content-Type": "application/proto"},
                )
                assert response.status_code == 501, (name, response.text)
    finally:
        await listener.stop()

    assert opened == []
    assert provider_reads == []


@pytest.mark.parametrize("session_owner", [True, False], ids=["owner", "non-owner"])
async def test_the_server_never_lists_them_even_when_a_host_does(
    tmp_path: Path, session_owner: bool
) -> None:
    response = await server_tests._listing_through_server(
        tmp_path, ["ListDirectory", *_NOT_SERVED], session_owner=session_owner
    )

    assert list(response.capabilities) == ["ListDirectory"]


async def test_the_servers_route_refuses_each_before_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_calls: list[HostWorkspaceCallFrame]
) -> None:
    app = service_tests._server_app(tmp_path, monkeypatch)
    for name in _NOT_SERVED:
        response = await server_tests._post(
            app, name, _request(name), {"Content-Type": "application/proto"}
        )
        assert response.status_code == 404, (name, response.text)

    assert host_calls == []
