"""Fenced admission and real inert-process ownership without an app or vendor CLI."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import io
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from click import ClickException

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harnesses.claude_native import bridge, main
from omnigent.harnesses.claude_native.settings_generation import (
    ClaudeSettingsRequest,
    acquire_claude_settings_request,
    has_live_claude_settings_owners,
    prune_unused_claude_settings,
    request_claude_bridge_deletion,
    settings_generation_path,
)
from omnigent.inner import process_lifetime
from omnigent.inner.terminal import TerminalInstance
from tests._helpers.live_server import terminate_process
from tests._helpers.stdio_jsonrpc import read_json_line
from tests.harnesses.claude_native._launch_failure_helpers import (
    POST_ERRORS,
    POST_RESPONSE_FAILURES,
    UNCERTAIN_POST_ERRORS,
    UNDELIVERED_POST_ERRORS,
)

pytestmark = pytest.mark.timeout(20)


@pytest.fixture
def bridge_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    return bridge.prepare_bridge_dir("conv_fenced", workspace=tmp_path)


def _populate(request: ClaudeSettingsRequest) -> Path:
    bridge.augment_claude_args((), bridge_dir=request.bridge_dir, settings_request=request)
    return request.settings_path(request.bridge_dir)


@pytest.mark.parametrize(
    "protocol,owner,metadata,lifecycle_fault",
    [
        (protocol, owner, None, None)
        for protocol in (1, 2)
        for owner in ("sender", "reader", "unknown")
    ]
    + [(2, "reader", raw, None) for raw in (b"{", b"[]", b"\xff")]
    + [
        (protocol, "reader", None, fault)
        for protocol in (1, 2)
        for fault in ("symlink", "read", "write", "format", "depth")
    ],
)
def test_deleted_bridge_removes_credentials_and_keeps_owned_inputs(
    bridge_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    protocol: int,
    owner: str,
    metadata: bytes | None,
    lifecycle_fault: str | None,
) -> None:
    request = ClaudeSettingsRequest.create(bridge_dir, protocol=protocol)
    baseline = _populate(request)
    contents = baseline.read_bytes()
    reader = (
        acquire_claude_settings_request(bridge_dir, request.generation, protocol=protocol)
        if owner == "reader"
        else None
    )
    unknown = baseline.with_suffix(f".terminal-{'1' * 32}.unconfirmed.owner")
    if owner == "unknown":
        unknown.touch(mode=0o600)
        request.close()
    bridge.build_hook_settings(
        bridge_dir,
        ap_server_url="https://example.com",
        ap_auth_headers={"Authorization": "Bearer synthetic-launch"},
    )
    binding = bridge.read_bridge_credential_binding(bridge_dir)
    assert binding is not None
    credential_files = ["permission_hook.json", "server.json", "tool_relay.json", "tool_relay.env"]
    for name in credential_files[1:]:
        (bridge_dir / name).write_text("synthetic-local-credential")
    if metadata is not None:
        (bridge_dir / "bridge.json").write_bytes(metadata)
    lifecycle_path = bridge_dir / ".claude-settings-lifecycle.json"
    original_lifecycle = lifecycle_path.read_bytes()
    target = bridge_dir.parent / "lifecycle-target.json"
    try:
        with monkeypatch.context() as faults:
            if lifecycle_fault == "symlink":
                target.write_bytes(original_lifecycle)
                lifecycle_path.unlink()
                lifecycle_path.symlink_to(target)
            elif lifecycle_fault == "read":
                read_settings = bridge._read_caller_settings

                def fail_read(path: Path, **kwargs) -> str:
                    if path == lifecycle_path:
                        raise OSError("synthetic lifecycle read failure")
                    return read_settings(path, **kwargs)

                faults.setattr(bridge, "_read_caller_settings", fail_read)
            elif lifecycle_fault == "write":
                write_json = bridge._write_json_file

                def fail_write(path: Path, *args, **kwargs) -> None:
                    if path == lifecycle_path:
                        raise OSError("synthetic lifecycle write failure")
                    write_json(path, *args, **kwargs)

                faults.setattr(bridge, "_write_json_file", fail_write)
            elif lifecycle_fault in {"format", "depth"}:
                lifecycle_path.write_text("{" if lifecycle_fault == "format" else "[" * 2000)
            request_claude_bridge_deletion(bridge_dir, "conv_fenced")
        if lifecycle_fault == "symlink":
            assert lifecycle_path.is_symlink()
            assert target.read_bytes() == original_lifecycle
        elif lifecycle_fault in {"read", "write"}:
            assert lifecycle_path.read_bytes() == original_lifecycle
        tombstone = json.loads((bridge_dir / "bridge.json").read_text())
        assert tombstone == {
            **({"bridge_id": "conv_fenced"} if metadata is None else {}),
            "active_session_id": "conv_fenced",
            "conversation_id": "conv_fenced",
            "deleted": True,
        }
        assert not any((bridge_dir / name).exists() for name in credential_files)
        assert bridge.read_active_session_id(bridge_dir) is None
        assert bridge.read_bridge_credential_binding(bridge_dir) is None
        assert baseline.read_bytes() == contents
        assert has_live_claude_settings_owners(bridge_dir)
        assert not bridge.update_permission_hook_auth_headers(
            bridge_dir,
            {"Authorization": "Bearer synthetic-late"},
            credential_binding=binding,
        )
        with pytest.raises(ValueError, match="expired"):
            bridge.build_hook_settings(bridge_dir, ap_server_url="https://example.com")
        assert not (bridge_dir / "permission_hook.json").exists()
        assert baseline.read_bytes() == contents
        if owner == "unknown":
            assert unknown.exists()
    finally:
        if lifecycle_fault is not None:
            lifecycle_path.unlink(missing_ok=True)
            lifecycle_path.write_bytes(original_lifecycle)
        request.close()
        if reader is not None:
            reader.close()
        unknown.unlink(missing_ok=True)
        prune_unused_claude_settings(bridge_dir)
    if lifecycle_fault is not None:
        assert bridge_dir.exists()
        assert not any((bridge_dir / name).exists() for name in credential_files)
        request_claude_bridge_deletion(bridge_dir, "conv_fenced")
    assert not bridge_dir.exists()


@pytest.mark.parametrize("identity_fault", ["session", "directory", "unreadable"])
def test_malformed_bridge_deletion_preserves_unverified_activation(
    bridge_dir: Path, identity_fault: str
) -> None:
    lifecycle_path = bridge_dir / ".claude-settings-lifecycle.json"
    lifecycle = json.loads(lifecycle_path.read_text())
    if identity_fault == "session":
        lifecycle["session_id"] = "another-session"
    elif identity_fault == "directory":
        lifecycle["bridge_identity"][1] += 1
    lifecycle_path.write_text("{" if identity_fault == "unreadable" else json.dumps(lifecycle))
    config = bridge_dir / "bridge.json"
    config.write_text("{")
    credentials = bridge_dir / "permission_hook.json"
    credentials.write_text("synthetic-other-activation")
    before = {path.name: path.read_bytes() for path in bridge_dir.iterdir() if path.is_file()}

    request_claude_bridge_deletion(bridge_dir, "conv_fenced")

    assert {
        path.name: path.read_bytes() for path in bridge_dir.iterdir() if path.is_file()
    } == before


def test_old_owners_cannot_remove_or_refresh_reactivated_credentials(bridge_dir: Path) -> None:
    request = ClaudeSettingsRequest.create(bridge_dir, protocol=2)
    baseline = _populate(request)
    reader = acquire_claude_settings_request(bridge_dir, request.generation, protocol=2)
    binding = bridge.read_bridge_credential_binding(bridge_dir)
    assert binding is not None
    try:
        request_claude_bridge_deletion(bridge_dir, "conv_fenced")
        bridge.prepare_bridge_dir("conv_fenced", workspace=bridge_dir.parent)
        current = bridge.read_bridge_credential_binding(bridge_dir)
        assert current is not None and current != binding
        assert current.directory_identity == binding.directory_identity
        bridge.build_hook_settings(
            bridge_dir,
            ap_server_url="https://example.com",
            ap_auth_headers={"Authorization": "Bearer synthetic-new-activation"},
        )
        config = (bridge_dir / "bridge.json").read_bytes()
        snapshot = (bridge_dir / "permission_hook.json").read_bytes()
        assert not bridge.update_permission_hook_auth_headers(
            bridge_dir,
            {"Authorization": "Bearer synthetic-old-activation"},
            credential_binding=binding,
        )
        with pytest.raises(ValueError, match="expired"):
            acquire_claude_settings_request(bridge_dir, request.generation, protocol=2)
        request.close()
        reader.close()
        assert not baseline.exists()
        request_claude_bridge_deletion(bridge_dir, "another-session")
        assert (bridge_dir / "bridge.json").read_bytes() == config
        assert (bridge_dir / "permission_hook.json").read_bytes() == snapshot
        assert bridge.read_active_session_id(bridge_dir) == "conv_fenced"
    finally:
        reader.close()
        request.close()


@pytest.mark.parametrize("kind", ["control", "relay"])
@pytest.mark.parametrize("reactivate", [False, True])
def test_cached_local_handlers_refuse_a_deleted_bridge_activation(
    bridge_dir: Path, kind: str, reactivate: bool
) -> None:
    request = ClaudeSettingsRequest.create(bridge_dir)
    _populate(request)
    notifications: queue.Queue = queue.Queue()
    handler_class = (
        bridge._handler_factory("synthetic-local-token", notifications, bridge_dir=bridge_dir)
        if kind == "control"
        else bridge._tool_relay_handler_factory(
            "synthetic-local-token", AsyncMock(), Mock(), bridge_dir=bridge_dir
        )
    )
    handler = handler_class.__new__(handler_class)
    handler.path = "/tools-changed" if kind == "control" else "/hook/observe-tool"
    handler.headers = {"Authorization": "Bearer synthetic-local-token", "Content-Length": "2"}
    handler.rfile = io.BytesIO(b"{}")
    handler.send_error = Mock()
    handler._send_json = Mock()
    try:
        handler.do_POST()
        handler.send_error.assert_not_called()
        handler._send_json.assert_called_once()
        request_claude_bridge_deletion(bridge_dir, "conv_fenced")
        if reactivate:
            bridge.prepare_bridge_dir("conv_fenced", workspace=bridge_dir.parent)
        handler.do_POST()
        handler.send_error.assert_called_once_with(401)
        handler._send_json.assert_called_once()
    finally:
        request.close()


@pytest.mark.parametrize("kind", ["control", "relay"])
@pytest.mark.parametrize("reactivate", [False, True])
def test_deleted_bridge_cannot_be_republished_by_an_inflight_endpoint_start(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch, kind: str, reactivate: bool
) -> None:
    request = ClaudeSettingsRequest.create(bridge_dir)
    _populate(request)
    server = Mock()

    def retire_during_bind(_handler: object) -> tuple[Mock, object]:
        request_claude_bridge_deletion(bridge_dir, "conv_fenced")
        if reactivate:
            bridge.prepare_bridge_dir("conv_fenced", workspace=bridge_dir.parent)
        return server, "http://unused.invalid" if kind == "relay" else bridge_dir / "control.sock"

    monkeypatch.setattr(bridge, "IS_WINDOWS", False)
    monkeypatch.delenv(bridge.BRIDGE_BIND_HOST_ENV_VAR, raising=False)
    monkeypatch.setattr(bridge, "_start_bridge_http_server", retire_during_bind)
    monkeypatch.setattr(bridge, "_start_unix_control_server", retire_during_bind)
    try:
        with pytest.raises(ValueError, match="expired"):
            if kind == "relay":
                bridge.start_tool_relay(
                    bridge_dir=bridge_dir, tools=[], tool_executor=AsyncMock(), loop=Mock()
                )
            else:
                bridge._start_http_ingress(bridge_dir, "synthetic-local-token", queue.Queue())
        server.server_close.assert_called_once()
        server.serve_forever.assert_not_called()
        for name in ("server.json", "tool_relay.json", "tool_relay.env"):
            assert not (bridge_dir / name).exists()
        assert bridge.read_active_session_id(bridge_dir) == ("conv_fenced" if reactivate else None)
    finally:
        request.close()


@pytest.mark.parametrize("reactivate", [False, True])
def test_old_relay_refresh_and_close_preserve_reactivated_credentials(
    bridge_dir: Path, reactivate: bool
) -> None:
    request = ClaudeSettingsRequest.create(bridge_dir)
    _populate(request)
    info = {"url": "http://unused.invalid", "token": "synthetic-old-relay", "tools": []}
    relay = bridge.ClaudeNativeToolRelay(
        bridge_dir=bridge_dir,
        httpd=Mock(),
        advertised_url=info["url"],
        relay_info=info,
        tool_executor=AsyncMock(),
        closed=threading.Event(),
        credential_binding=bridge.read_bridge_credential_binding(bridge_dir),
    )
    relay_file = bridge_dir / "tool_relay.json"
    try:
        relay.update_tools([], AsyncMock())
        assert json.loads(relay_file.read_text())["token"] == info["token"]
        request_claude_bridge_deletion(bridge_dir, "conv_fenced")
        if reactivate:
            bridge.prepare_bridge_dir("conv_fenced", workspace=bridge_dir.parent)
            bridge._write_json_file(relay_file, {**info, "token": "synthetic-new-relay"})
            current = relay_file.read_bytes()
        with pytest.raises(RuntimeError, match="activation has ended"):
            relay.update_tools([], AsyncMock())
        relay.close()
        if reactivate:
            assert relay_file.read_bytes() == current
        else:
            assert not relay_file.exists()
    finally:
        request.close()


@pytest.mark.parametrize("admitted", [False, True])
def test_sender_close_fences_new_admission_and_preserves_admitted_readers(
    bridge_dir: Path, admitted: bool
) -> None:
    request = ClaudeSettingsRequest.create(bridge_dir, protocol=2)
    baseline = _populate(request)
    contents = baseline.read_bytes()
    request.mark_dispatched()
    receiver = (
        acquire_claude_settings_request(bridge_dir, request.generation, protocol=2)
        if admitted
        else None
    )
    try:
        request.close()
        assert not baseline.with_suffix(".dispatch.owner").exists()
        with pytest.raises(ValueError, match="expired"):
            acquire_claude_settings_request(bridge_dir, request.generation, protocol=2)
        if receiver is not None:
            assert baseline.read_bytes() == contents
            assert prune_unused_claude_settings(bridge_dir) == 0
            receiver.close()
        assert not list(bridge_dir.glob(f"claude-settings-{request.generation}*"))
    finally:
        request.close()
        if receiver is not None:
            receiver.close()


@pytest.mark.parametrize(
    ("admitted", "failure"),
    [
        (admitted, failure)
        for failure in [*UNCERTAIN_POST_ERRORS, "400", *POST_RESPONSE_FAILURES]
        for admitted in [False, True]
    ]
    + [(False, failure) for failure in UNDELIVERED_POST_ERRORS]
    + [(True, "reactivated-ack")],
)
async def test_uncertain_http_completion_releases_only_sender_ownership(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch, admitted: bool, failure: str
) -> None:
    monkeypatch.setattr(main, "resolve_claude_launch", lambda cmd, args: (cmd, args))
    received = []
    receiver = None
    generation = None
    if failure == "cleanup-failure":
        monkeypatch.setattr(
            ClaudeSettingsRequest,
            "cancel_undelivered_dispatch",
            Mock(side_effect=OSError("inert cleanup failure")),
        )

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal receiver, generation
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "claude_settings_request_protocol": 1,
                    "claude_settings_fenced_request_protocol": 2,
                },
            )
        body = json.loads(request.content)
        assert request.url.path.endswith("/resources/claude-settings-v2/terminals")
        assert body["claude_settings_request_protocol"] == 2
        generation = body["claude_settings_request"]
        if admitted:
            receiver = acquire_claude_settings_request(bridge_dir, generation, protocol=2)
            received.append(settings_generation_path(bridge_dir, generation).read_bytes())
        if failure in POST_ERRORS:
            raise POST_ERRORS[failure]("inert uncertain delivery")
        if failure == "reactivated-ack":
            request_claude_bridge_deletion(bridge_dir, "conv_fenced")
            bridge.prepare_bridge_dir("conv_fenced", workspace=bridge_dir.parent)
            return httpx.Response(
                409,
                json={
                    "detail": "inert rejection after reactivation",
                    "claude_settings_request_ack": generation,
                },
            )
        return httpx.Response(
            200 if failure in {"no-ack", "mismatched"} else int(failure),
            json={
                "id": "terminal",
                **({"claude_settings_request_ack": "0" * 32} if failure == "mismatched" else {}),
            },
        )

    try:
        async with httpx.AsyncClient(
            base_url="http://unused.invalid", transport=httpx.MockTransport(respond)
        ) as client:
            expected = POST_ERRORS.get(failure, ClickException)
            with pytest.raises(expected):
                await main._launch_claude_terminal(
                    client, "conv_fenced", (), command="claude", bridge_dir=bridge_dir
                )
        assert generation is not None
        baseline = settings_generation_path(bridge_dir, generation)
        assert not baseline.with_suffix(".dispatch.owner").exists()
        with pytest.raises(ValueError, match="expired"):
            acquire_claude_settings_request(bridge_dir, generation, protocol=2)
        if receiver is not None:
            assert baseline.read_bytes() == received[0]
            assert prune_unused_claude_settings(bridge_dir) == 0
            receiver.close()
        if failure == "reactivated-ack":
            prune_unused_claude_settings(bridge_dir)
        assert not list(bridge_dir.glob(f"claude-settings-{generation}*"))
    finally:
        if receiver is not None:
            receiver.close()


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        pytest.param(
            httpx.Response(200, json={"claude_settings_request_protocol": 1}), 1, id="legacy-peer"
        ),
        pytest.param(
            httpx.Response(200, json={"claude_settings_fenced_request_protocol": 2}),
            2,
            id="fenced-peer",
        ),
        pytest.param(
            httpx.Response(
                200,
                json={
                    "claude_settings_request_protocol": 1,
                    "claude_settings_fenced_request_protocol": 3,
                },
            ),
            1,
            id="legacy-with-unknown-fenced-version",
        ),
        pytest.param(
            httpx.Response(404, json={"claude_settings_request_protocol": 1}), None, id="http-404"
        ),
        pytest.param(
            httpx.Response(503, json={"claude_settings_request_protocol": 1}), None, id="http-503"
        ),
        pytest.param(httpx.ConnectError("inert probe failure"), None, id="connect-error"),
        pytest.param(httpx.ReadTimeout("inert probe timeout"), None, id="timeout"),
        pytest.param(httpx.Response(200, content=b"{"), None, id="malformed-json"),
        pytest.param(httpx.Response(200, json=[]), None, id="non-object"),
        pytest.param(httpx.Response(200, json={}), None, id="missing-version"),
        *[
            pytest.param(
                httpx.Response(200, json={"claude_settings_request_protocol": version}),
                None,
                id=name,
            )
            for name, version in [
                ("version-zero", 0),
                ("version-future", 2),
                ("version-string", "1"),
                ("version-float", 1.0),
                ("version-bool", True),
            ]
        ],
        pytest.param(
            httpx.Response(200, json={"claude_settings_fenced_request_protocol": True}),
            None,
            id="fenced-bool",
        ),
        pytest.param(
            httpx.Response(200, json={"claude_settings_fenced_request_protocol": 2.0}),
            None,
            id="fenced-float",
        ),
    ],
)
async def test_additive_capability_negotiation_keeps_legacy_fallback(
    probe: httpx.Response | httpx.HTTPError, expected: int | None
) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        if isinstance(probe, httpx.HTTPError):
            raise probe
        return probe

    async with httpx.AsyncClient(
        base_url="http://unused.invalid", transport=httpx.MockTransport(respond)
    ) as client:
        assert await main._supports_claude_settings_requests(client, "conv_fenced") == expected


@pytest.mark.parametrize("status", [404, 503])
async def test_old_server_or_rebound_runner_falls_back_before_dispatch(status: int) -> None:
    paths = []

    def respond(request):
        paths.append(request.url.path)
        return httpx.Response(
            status
            if request.url.path.endswith("/resources/claude-settings-v2/terminals")
            else 200,
            json={
                "claude_settings_request_protocol": 1,
                "claude_settings_fenced_request_protocol": 2,
            },
        )

    async with httpx.AsyncClient(
        base_url="http://unused.invalid", transport=httpx.MockTransport(respond)
    ) as client:
        assert await main._supports_claude_settings_requests(client, "conv_fenced") == 1
    assert len(paths) == 2 and paths[-1].endswith("/resources/claude-settings-v2/terminals")


async def test_dispatched_v2_is_not_downgraded_if_runner_changes(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "resolve_claude_launch", lambda cmd, args: (cmd, args))
    posts = []

    def respond(request):
        if request.method == "POST":
            posts.append(request.url.path)
            return httpx.Response(404, json={"detail": "inert older receiver"})
        return httpx.Response(
            200,
            json={
                "claude_settings_request_protocol": 1,
                "claude_settings_fenced_request_protocol": 2,
            },
        )

    async with httpx.AsyncClient(
        base_url="http://unused.invalid", transport=httpx.MockTransport(respond)
    ) as client:
        with pytest.raises(ClickException, match="404"):
            await main._launch_claude_terminal(
                client, "conv_fenced", (), command="claude", bridge_dir=bridge_dir
            )
    assert len(posts) == 1 and posts[0].endswith("/resources/claude-settings-v2/terminals")
    assert not list(bridge_dir.glob("claude-settings-*"))


@pytest.mark.parametrize("peer", ["legacy", "v1", "old-server"])
@pytest.mark.parametrize("status", [200, 503])
async def test_legacy_launches_reuse_historical_file_without_untracked_generations(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch, peer: str, status: int
) -> None:
    monkeypatch.setattr(main, "resolve_claude_launch", lambda cmd, args: (cmd, args))
    posted = []

    def respond(request):
        if request.method == "GET":
            capabilities = {} if peer == "legacy" else {"claude_settings_request_protocol": 1}
            if peer == "old-server":
                capabilities["claude_settings_fenced_request_protocol"] = 2
            return httpx.Response(
                404
                if request.url.path.endswith("/resources/claude-settings-v2/terminals")
                else 200,
                json=capabilities,
            )
        assert not request.url.path.endswith("/resources/claude-settings-v2/terminals")
        body = json.loads(request.content)
        assert "claude_settings_request" not in body
        assert "claude_settings_generation" not in body
        args = body["spec"]["args"]
        assert args[:2] == ["--settings", '{"env":{"CALLER":"kept-in-argv"}}']
        assert Path(args[max(i for i, arg in enumerate(args) if arg == "--settings") + 1]) == (
            bridge_dir / "claude-settings.json"
        )
        posted.append(body)
        return httpx.Response(status, json={"id": "legacy-terminal"})

    async with httpx.AsyncClient(
        base_url="http://unused.invalid", transport=httpx.MockTransport(respond)
    ) as client:
        for _ in range(6):
            launch = main._launch_claude_terminal(
                client,
                "conv_fenced",
                ("--settings", '{"env":{"CALLER":"kept-in-argv"}}'),
                command="claude",
                bridge_dir=bridge_dir,
            )
            if status == 200:
                assert await launch == "legacy-terminal"
            else:
                with pytest.raises(ClickException, match="503"):
                    await launch
            assert not list(bridge_dir.glob("claude-settings-*"))
    assert len(posted) == 6
    assert (bridge_dir / "claude-settings.json").stat().st_mode & 0o777 == 0o600


def test_legacy_receiver_cannot_consume_a_fenced_record(bridge_dir: Path) -> None:
    with ClaudeSettingsRequest.create(bridge_dir, protocol=2) as request:
        baseline = _populate(request)
        request.mark_dispatched()
        before = baseline.read_bytes()
        with pytest.raises(ValueError, match="expired"):
            acquire_claude_settings_request(bridge_dir, request.generation)
        assert baseline.read_bytes() == before
        with acquire_claude_settings_request(bridge_dir, request.generation, protocol=2):
            pass


def test_fenced_request_cannot_be_admitted_for_another_session(bridge_dir: Path) -> None:
    with ClaudeSettingsRequest.create(bridge_dir, protocol=2) as request:
        baseline = _populate(request)
        request.mark_dispatched()
        contents = baseline.read_bytes()
        with pytest.raises(ValueError, match="expired"):
            acquire_claude_settings_request(
                bridge_dir, request.generation, protocol=2, expected_session_id="another-session"
            )
        assert baseline.read_bytes() == contents
        with acquire_claude_settings_request(
            bridge_dir, request.generation, protocol=2, expected_session_id="conv_fenced"
        ):
            pass


@pytest.mark.parametrize("failure", ["early-return", "bootstrap-error", "cancel"])
async def test_route_dependency_admits_before_bootstrap_and_releases_on_every_exit(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from omnigent.runner import resource_routes

    routes = {}

    class InertRoutes:
        def __getattr__(self, method):
            def register(path, **_kwargs):
                def capture(function):
                    routes[method, path] = function
                    return function

                return capture

            return register

    arguments = {
        name: AsyncMock()
        for name in inspect.signature(resource_routes.register_resource_routes).parameters
        if name != "app"
    }
    for name in arguments:
        if name.endswith("_locks") or name in {"_resp_to_conv", "_session_comment_relays"}:
            arguments[name] = {}
    monkeypatch.setattr(
        resource_routes,
        "_claude_native_bridge_id_for_session",
        AsyncMock(return_value="conv_fenced"),
    )
    resource_routes.register_resource_routes(InertRoutes(), **arguments)
    endpoint = routes["post", "/v1/sessions/{session_id}/resources/terminals"]
    provider = inspect.signature(endpoint).parameters["fenced_admission"].default.dependency
    with ClaudeSettingsRequest.create(bridge_dir, protocol=2) as request:
        baseline = _populate(request)
        contents = baseline.read_bytes()
        request.mark_dispatched()
        body = {
            "bridge_inject_dir": True,
            "claude_settings_request_protocol": 2,
            "claude_settings_request": request.generation,
        }
        if failure != "early-return":
            body.update(terminal="claude", session_key="main")
            arguments["_resolve_session_agent_spec"].side_effect = (
                asyncio.CancelledError()
                if failure == "cancel"
                else RuntimeError("inert bootstrap")
            )
        incoming = SimpleNamespace(json=AsyncMock(return_value=body))
        ownership = provider("conv_fenced", incoming)
        admission = await anext(ownership)
        try:
            request.close()
            assert baseline.read_bytes() == contents
            if failure == "early-return":
                response = await endpoint("conv_fenced", incoming, fenced_admission=admission)
                assert response.status_code == 400
            else:
                with pytest.raises((RuntimeError, asyncio.CancelledError)):
                    await endpoint("conv_fenced", incoming, fenced_admission=admission)
            assert baseline.read_bytes() == contents
        finally:
            await ownership.aclose()
        assert not baseline.exists()
        expired = provider("conv_fenced", incoming)
        with pytest.raises(OmnigentError) as error:
            await anext(expired)
        assert error.value.http_status == 409
        assert error.value.code == ErrorCode.CONFLICT
        assert "rebuild the request" in error.value.message


_CONTROLLER = """
import json,sys
from pathlib import Path
from omnigent.harnesses.claude_native import bridge
from omnigent.harnesses.claude_native.settings_generation import (
    ClaudeSettingsRequest, acquire_claude_settings_request,
)
from omnigent.inner.terminal import TerminalInstance
from omnigent.inner.process_lifetime import encode_process_leases
root=Path(sys.argv[1])
protocol=int(sys.argv[2])
bridge._TRUSTED_PARENT=root.parent
bridge._BRIDGE_ROOT=root
directory=bridge.prepare_bridge_dir('conv_fenced',workspace=root.parent)
request=ClaudeSettingsRequest.create(directory,protocol=protocol)
bridge.augment_claude_args((),bridge_dir=directory,settings_request=request)
private=root.parent/'terminal'
private.mkdir()
instance=TerminalInstance(name='claude',session_key='main',private_dir=private,socket_path=private/'tmux.sock')
request.mark_dispatched()
with acquire_claude_settings_request(directory,request.generation,protocol=protocol) as receiver:
    receiver.attach(instance)
output=private/'settings.json'
output.write_bytes(request.settings_path(directory).read_bytes())
print(json.dumps({'generation':request.generation,'leases':encode_process_leases(instance.process_lifetime_leases),'directory':str(directory),'output':str(output)}),flush=True)
sys.stdin.read()
"""

_CONSUMER = """
import json,os,sys
from pathlib import Path
os.closerange(3,256)
paths=[Path(value) for value in sys.argv[1:]]
def snapshot():
    data={'contents':paths[0].read_text()}
    if len(paths)>1:
        data['inputs']=[path.read_text() for path in paths[1:]]
    return data
print(json.dumps({'pid':os.getpid(),**snapshot()}),flush=True)
sys.stdin.readline()
print(json.dumps(snapshot()),flush=True)
sys.stdin.read()
"""


@pytest.mark.parametrize("existing_uncertainty", [False, True])
@pytest.mark.parametrize("protocol", [1, 2])
async def test_unconfirmed_consumer_completion_retains_generation_and_private_files(
    bridge_dir: Path, tmp_path: Path, existing_uncertainty: bool, protocol: int
) -> None:
    private = tmp_path / "terminal"
    private.mkdir()
    instance = TerminalInstance(
        name="claude", session_key="main", private_dir=private, socket_path=private / "tmux.sock"
    )
    request = ClaudeSettingsRequest.create(bridge_dir, protocol=protocol)
    baseline = _populate(request)
    contents = baseline.read_bytes()
    with acquire_claude_settings_request(
        bridge_dir, request.generation, protocol=protocol
    ) as receiver:
        receiver.attach(instance)
    encoded = process_lifetime.encode_process_leases(instance.process_lifetime_leases)
    assert encoded is not None
    process_lifetime.write_process_lease_manifest(private, encoded)
    output = private / "settings.json"
    output.write_bytes(contents)
    uncertain = instance.process_lifetime_leases[0].claim.with_suffix(".unconfirmed.owner")
    if existing_uncertainty:
        uncertain.write_text("unrecognized completion record")
    try:
        with process_lifetime.hold_process_leases(encoded):
            instance._release_process_lifetime_handoffs(ready_only=True)
            request.close()
        assert prune_unused_claude_settings(bridge_dir) == 0
        assert baseline.read_bytes() == contents
        assert has_live_claude_settings_owners(bridge_dir)
        with process_lifetime.finish_process_leases(private) as unused:
            assert unused is None
        await instance.close()
        assert output.read_bytes() == contents
        assert instance._private_cleanup_thread is None
        if existing_uncertainty:
            assert uncertain.read_text() == "unrecognized completion record"
    finally:
        request.close()
        await instance.close()


@pytest.mark.parametrize("kill_guard", [False, True], ids=["guard-alive", "guard-killed"])
@pytest.mark.parametrize("protocol", [1, 2])
def test_consumer_drops_inherited_leases_but_keeps_inputs_after_guard_and_controller_death(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kill_guard: bool, protocol: int
) -> None:
    controller = subprocess.Popen(
        [sys.executable, "-c", _CONTROLLER, str(tmp_path / "bridges"), str(protocol)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    guard = None
    consumer_pid = None
    try:
        assert controller.stdout is not None
        record = read_json_line(controller.stdout, timeout_s=5)
        directory = Path(record["directory"])
        monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
        monkeypatch.setattr(bridge, "_BRIDGE_ROOT", directory.parent)
        baseline = settings_generation_path(directory, record["generation"])
        contents = baseline.read_text()
        assert record["leases"] is not None
        guard = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(Path(process_lifetime.__file__)),
                record["leases"],
                "--",
                sys.executable,
                "-c",
                _CONSUMER,
                str(baseline),
                record["output"],
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert guard.stdout is not None and guard.stdin is not None
        ready = read_json_line(guard.stdout, timeout_s=5)
        consumer_pid = ready["pid"]
        assert ready["contents"] == contents
        assert ready["inputs"] == [contents]
        controller.kill()
        controller.wait(timeout=5)
        if kill_guard:
            guard.kill()
            guard.wait(timeout=5)
        assert prune_unused_claude_settings(directory) == 0
        assert not baseline.with_suffix(".dispatch.owner").exists()
        assert has_live_claude_settings_owners(directory)
        request_claude_bridge_deletion(directory, "conv_fenced")
        monkeypatch.setattr(bridge, "_BRIDGE_ROOT", directory.parent)
        assert bridge.prune_orphaned_bridge_dirs() == 0
        assert baseline.read_text() == contents
        guard.stdin.write("read again\n")
        guard.stdin.flush()
        assert read_json_line(guard.stdout, timeout_s=5) == {
            "contents": contents,
            "inputs": [contents],
        }
        guard.stdin.close()
        assert guard.stdout.read() == ""
        if not kill_guard:
            assert guard.wait(timeout=5) == 0
        consumer_pid = None
        assert prune_unused_claude_settings(directory) == 0
        assert baseline.read_text() == contents
        assert has_live_claude_settings_owners(directory)
    finally:
        if consumer_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(consumer_pid, 9)
        terminate_process(guard)
        terminate_process(controller)


@pytest.mark.parametrize("protocol", [1, 2])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_preparation_never_publishes_terminal_ownership(
    bridge_dir: Path, tmp_path: Path, protocol: int, cancelled: bool
) -> None:
    private = tmp_path / "terminal"
    private.mkdir()
    instance = TerminalInstance(
        name="claude", session_key="main", private_dir=private, socket_path=private / "tmux.sock"
    )
    request = ClaudeSettingsRequest.create(bridge_dir, protocol=protocol)
    baseline = _populate(request)
    contents = baseline.read_bytes()
    failure = asyncio.CancelledError() if cancelled else ValueError("inert preparation failure")
    prepare = AsyncMock(side_effect=failure)
    try:
        with acquire_claude_settings_request(
            bridge_dir, request.generation, protocol=protocol
        ) as receiver:
            with pytest.raises(type(failure)) as raised:
                await receiver.attach_async(instance, prepare=prepare)
            assert raised.value is failure
            prepare.assert_awaited_once_with()
            assert instance.process_lifetime_leases == []
            assert not list(bridge_dir.glob("*.terminal-*.owner"))
            assert baseline.read_bytes() == contents
        request.close()
        assert not list(bridge_dir.glob(f"claude-settings-{request.generation}*"))
    finally:
        request.close()
        await instance.close()
    assert not private.exists()


async def test_published_terminal_cannot_be_discarded_as_failed_preparation(
    bridge_dir: Path, tmp_path: Path
) -> None:
    private = tmp_path / "terminal"
    private.mkdir()
    instance = TerminalInstance(
        name="claude", session_key="main", private_dir=private, socket_path=private / "tmux.sock"
    )
    request = ClaudeSettingsRequest.create(bridge_dir, protocol=2)
    baseline = _populate(request)
    contents = baseline.read_bytes()
    try:
        with acquire_claude_settings_request(bridge_dir, request.generation, protocol=2) as reader:
            await reader.attach_async(instance)
        encoded = process_lifetime.encode_process_leases(instance.process_lifetime_leases)
        assert encoded is not None
        process_lifetime.write_process_lease_manifest(private, encoded)
        _spec, release = instance._process_lifetime_handoffs[0]
        owner = release.__self__
        owner._discard_unpublished()
        request.close()
        assert baseline.read_bytes() == contents
        assert prune_unused_claude_settings(bridge_dir) == 0
        await instance.close()
        assert private.exists()
        assert instance._private_cleanup_thread is None
    finally:
        request.close()
        await instance.close()


@pytest.mark.parametrize("fault", ["malformed", "oversized", "identity"])
async def test_invalid_guard_encoding_never_starts_an_otherwise_valid_target(
    bridge_dir: Path, tmp_path: Path, fault: str
) -> None:
    private = tmp_path / "terminal"
    private.mkdir()
    instance = TerminalInstance(
        name="claude", session_key="main", private_dir=private, socket_path=private / "tmux.sock"
    )
    request = ClaudeSettingsRequest.create(bridge_dir, protocol=2)
    baseline = _populate(request)
    contents = baseline.read_bytes()
    marker = tmp_path / "target-started"
    try:
        with acquire_claude_settings_request(bridge_dir, request.generation, protocol=2) as reader:
            await reader.attach_async(instance)
        encoded = process_lifetime.encode_process_leases(
            [replace(instance.process_lifetime_leases[0], ready=None)]
        )
        assert encoded is not None

        def run(payload: str) -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(Path(process_lifetime.__file__)),
                    payload,
                    "--",
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                    str(marker),
                ],
                capture_output=True,
                timeout=5,
            )

        control = run(encoded)
        assert control.returncode == 0, control.stderr
        assert marker.is_file()
        marker.unlink()
        if fault == "identity":
            payload = json.loads(base64.urlsafe_b64decode(encoded))
            payload[0][1][1] += 1
            invalid = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode("ascii")
        else:
            invalid = "!" if fault == "malformed" else "x" * 65537
        assert run(invalid).returncode != 0
        assert not marker.exists()
        assert baseline.read_bytes() == contents
    finally:
        request.close()
        await instance.close()


@pytest.mark.parametrize("confirmed_close", [False, True])
async def test_late_terminal_guard_refuses_retired_operands_before_target_starts(
    bridge_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, confirmed_close: bool
) -> None:
    private = tmp_path / "terminal"
    private.mkdir()
    instance = TerminalInstance(
        name="claude", session_key="main", private_dir=private, socket_path=private / "tmux.sock"
    )
    with ClaudeSettingsRequest.create(bridge_dir, protocol=2) as request:
        baseline = _populate(request)
        with acquire_claude_settings_request(bridge_dir, request.generation, protocol=2) as lease:
            lease.attach(instance)
        encoded = process_lifetime.encode_process_leases(instance.process_lifetime_leases)
        if not confirmed_close:
            instance.running = True
            instance.socket_path.touch()
            monkeypatch.setattr(
                instance, "_tmux", AsyncMock(side_effect=RuntimeError("inert kill failure"))
            )
        await instance.close()
        assert private.exists() is not confirmed_close
    assert baseline.exists() is not confirmed_close
    marker = tmp_path / "target-started"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(process_lifetime.__file__)),
            encoded,
            "--",
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
            str(marker),
        ],
        capture_output=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert not marker.exists()
    if not confirmed_close:
        monkeypatch.setattr(instance, "_tmux", AsyncMock(return_value=""))
        await instance.close()


async def test_live_controller_handoff_retains_unconfirmed_consumer_ownership(
    bridge_dir: Path, tmp_path: Path
) -> None:
    private = tmp_path / "terminal"
    private.mkdir()
    instance = TerminalInstance(
        name="claude", session_key="main", private_dir=private, socket_path=private / "tmux.sock"
    )
    request = ClaudeSettingsRequest.create(bridge_dir, protocol=2)
    baseline = _populate(request)
    with acquire_claude_settings_request(bridge_dir, request.generation, protocol=2) as receiver:
        receiver.attach(instance)
    spec, release = instance._process_lifetime_handoffs[0]
    transferred = threading.Event()

    def complete_handoff():
        release()
        transferred.set()

    instance._process_lifetime_handoffs[0] = spec, complete_handoff
    instance._start_process_lifetime_handoff()
    encoded = process_lifetime.encode_process_leases(instance.process_lifetime_leases)
    guard = subprocess.Popen(
        [
            sys.executable,
            "-I",
            str(Path(process_lifetime.__file__)),
            encoded,
            "--",
            sys.executable,
            "-c",
            _CONSUMER,
            str(baseline),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert guard.stdout is not None and guard.stdin is not None
        read_json_line(guard.stdout, timeout_s=5)
        assert await asyncio.to_thread(transferred.wait, 5)
        request.close()
        assert baseline.exists()
        claims = list(bridge_dir.glob("*.terminal-*.owner"))
        assert len(claims) == 1 and claims[0].name.endswith(".unconfirmed.owner")
        assert prune_unused_claude_settings(bridge_dir) == 0
        guard.stdin.write("finish\n")
        guard.stdin.flush()
        read_json_line(guard.stdout, timeout_s=5)
        guard.stdin.close()
        assert guard.wait(timeout=5) == 0
        assert prune_unused_claude_settings(bridge_dir) == 0
        assert baseline.exists()
    finally:
        terminate_process(guard)
        request.close()
        await instance.close()


@pytest.mark.parametrize("cleanup", ["close", "orphan"])
@pytest.mark.parametrize("protocol", [1, 2])
async def test_finalized_private_settings_survive_unconfirmed_consumer_completion(
    bridge_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup: str,
    protocol: int,
) -> None:
    from omnigent.inner import terminal

    private = tmp_path / "omnigent-terminal-fenced"
    private.mkdir()
    instance = TerminalInstance(
        name="claude", session_key="main", private_dir=private, socket_path=private / "tmux.sock"
    )
    request = ClaudeSettingsRequest.create(bridge_dir, protocol=protocol)
    baseline = _populate(request)
    baseline_contents = baseline.read_text()
    with acquire_claude_settings_request(
        bridge_dir, request.generation, protocol=protocol
    ) as receiver:
        receiver.attach(instance)
    encoded = process_lifetime.encode_process_leases(instance.process_lifetime_leases)
    if encoded is not None:
        process_lifetime.write_process_lease_manifest(private, encoded)
    output = private / f"claude-finalized-settings-{request.generation}.json"
    output.write_text('{"env":{"INERT_PRIVATE":"kept"}}')
    command = [sys.executable, "-c", _CONSUMER, str(output), str(baseline)]
    if encoded is not None:
        command = [
            sys.executable,
            "-I",
            str(Path(process_lifetime.__file__)),
            encoded,
            "--",
            *command,
        ]
    guard = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert guard.stdout is not None and guard.stdin is not None
        ready = read_json_line(guard.stdout, timeout_s=5)
        assert ready["inputs"] == [baseline_contents]
        assert json.loads(ready["contents"])["env"] == {"INERT_PRIVATE": "kept"}
        request.close()
        if cleanup == "close":
            instance.running = True
            monkeypatch.setattr(instance, "_tmux", AsyncMock(return_value=""))
            await instance.close()
        else:
            instance._release_process_lifetime_handoffs(ready_only=False)
            monkeypatch.setattr(terminal, "_tmux_available", lambda: True)
            monkeypatch.setattr(terminal, "_terminals_tmp_root", lambda: tmp_path)
            monkeypatch.setattr(
                terminal.owner_claim, "read_owner_claim", lambda _path: SimpleNamespace(pid=123)
            )
            monkeypatch.setattr(terminal.owner_claim, "owner_is_gone", lambda *_a, **_kw: True)
            assert terminal.reap_orphaned_terminals() == 0
        assert output.exists()
        assert baseline.read_text() == baseline_contents
        guard.stdin.write("last read\n")
        guard.stdin.flush()
        last = read_json_line(guard.stdout, timeout_s=5)
        assert last["inputs"] == [baseline_contents]
        assert json.loads(last["contents"])["env"] == {"INERT_PRIVATE": "kept"}
        guard.stdin.close()
        assert guard.wait(timeout=5) == 0
        if cleanup == "close":
            assert instance._private_cleanup_thread is None
        else:
            assert terminal.reap_orphaned_terminals() == 0
        assert output.exists()
        assert prune_unused_claude_settings(bridge_dir) == 0
        assert list(bridge_dir.glob("claude-settings-*"))
    finally:
        terminate_process(guard)
        request.close()
        await instance.close()


@pytest.mark.parametrize("sandboxed", [False, True])
async def test_terminal_launch_wires_process_guard_after_private_manifest_is_written(
    bridge_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandboxed: bool
) -> None:
    from omnigent.inner import terminal
    from omnigent.inner.sandbox import SandboxPolicy

    private = tmp_path / "terminal"
    private.mkdir()
    instance = TerminalInstance(
        name="claude",
        session_key="main",
        private_dir=private,
        socket_path=private / "tmux.sock",
        command="inert-selected-command",
        args=["--literal", "argument with spaces"],
    )
    if sandboxed:
        instance.sandbox_policy = SandboxPolicy(
            "linux_bwrap", True, [tmp_path], [private], [], False
        )
    request = ClaudeSettingsRequest.create(bridge_dir, protocol=2)
    _populate(request)
    with acquire_claude_settings_request(bridge_dir, request.generation, protocol=2) as receiver:
        receiver.attach(instance)
    encoded = process_lifetime.encode_process_leases(instance.process_lifetime_leases)
    launches = []
    launcher_options = []

    async def spawn(*args, **_kwargs):
        manifest = json.loads((private / "process-lifetime.json").read_text())
        assert manifest["leases"] == encoded
        assert manifest["directory_identity"] == [private.stat().st_dev, private.stat().st_ino]
        assert (private / "process-lifetime.json").stat().st_mode & 0o777 == 0o600
        launches.append(args)
        return SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))

    def make_launcher(_command, _policy, **kwargs):
        launcher_options.append(kwargs)
        path = private / "inert-launcher"
        path.touch(mode=0o700)
        return str(path)

    monkeypatch.setattr(
        terminal, "asyncio", SimpleNamespace(**{**vars(asyncio), "create_subprocess_exec": spawn})
    )
    monkeypatch.setattr(terminal, "create_exec_launcher", make_launcher)
    monkeypatch.setattr(instance, "_tmux", AsyncMock(return_value=""))
    try:
        await instance.launch(cwd=tmp_path)
        command = launches[0]
        new_session = command.index("new-session")
        cwd_option = command.index("-c", new_session)
        inner = shlex.split(command[cwd_option + 2])
        if sandboxed:
            assert launcher_options == [{"cwd": str(tmp_path), "process_lifetime": encoded}]
            assert inner == [str(private / "inert-launcher"), *instance.args]
        else:
            assert launcher_options == []
            assert inner == [
                sys.executable,
                "-I",
                str(Path(process_lifetime.__file__)),
                encoded,
                "--",
                instance.command,
                *instance.args,
            ]
        assert instance.command == "inert-selected-command"
        assert instance.args == ["--literal", "argument with spaces"]
    finally:
        await instance.close()
        request.close()
        if instance._private_cleanup_thread is not None:
            await asyncio.to_thread(instance._private_cleanup_thread.join, 5)


@pytest.mark.parametrize("fault", ["future-schema", "copied-directory"])
async def test_unknown_private_manifest_cannot_authorize_file_deletion(
    bridge_dir: Path, tmp_path: Path, fault: str
) -> None:
    private = tmp_path / "terminal"
    private.mkdir()
    instance = TerminalInstance(
        name="claude", session_key="main", private_dir=private, socket_path=private / "tmux.sock"
    )
    with ClaudeSettingsRequest.create(bridge_dir, protocol=2) as request:
        _populate(request)
        with acquire_claude_settings_request(
            bridge_dir, request.generation, protocol=2
        ) as receiver:
            receiver.attach(instance)
        encoded = process_lifetime.encode_process_leases(instance.process_lifetime_leases)
        original = tmp_path / "original" if fault == "copied-directory" else private
        original.mkdir(exist_ok=True)
        process_lifetime.write_process_lease_manifest(original, encoded)
        manifest = private / "process-lifetime.json"
        if fault == "copied-directory":
            shutil.copyfile(original / "process-lifetime.json", manifest)
        else:
            contents = json.loads(manifest.read_text())
            contents["schema"] = 99
            manifest.write_text(json.dumps(contents))
        output = private / "finalized.json"
        output.write_text('{"synthetic":"retained"}')
        assert instance._finish_private_resources() is None
        assert output.read_text() == '{"synthetic":"retained"}'
        await instance.close()


@pytest.mark.parametrize("protocol", [1, 2])
@pytest.mark.parametrize(
    "fault",
    [
        "claim-metadata",
        "request-metadata",
        "lease-replaced",
        "claim-replaced",
        "claim-symlink",
        "bridge-replaced",
    ],
)
def test_unknown_or_replaced_claims_remain_protected(
    bridge_dir: Path, tmp_path: Path, protocol: int, fault: str
) -> None:
    request = ClaudeSettingsRequest.create(bridge_dir, protocol=protocol)
    baseline = _populate(request)
    request.mark_dispatched()
    claim = baseline.with_suffix(".dispatch.owner")
    if fault == "claim-metadata":
        claim.write_text('{"schema":"unknown"}')
    elif fault == "request-metadata":
        baseline.with_suffix(".request.lock").write_text('{"schema":3}')
    elif fault == "lease-replaced":
        baseline.with_suffix(".lease.lock").rename(tmp_path / "original-lease")
        baseline.with_suffix(".lease.lock").touch(mode=0o600)
    elif fault in {"claim-replaced", "claim-symlink"}:
        original = tmp_path / "original-claim"
        claim.rename(original)
        if fault == "claim-replaced":
            claim.write_bytes(original.read_bytes())
        else:
            claim.symlink_to(original)
    else:
        original = tmp_path / "original-bridge"
        bridge_dir.rename(original)
        shutil.copytree(original, bridge_dir)
    request.close()
    snapshot = {path.name: path.read_bytes() for path in bridge_dir.iterdir()}
    assert prune_unused_claude_settings(bridge_dir) == 0
    assert has_live_claude_settings_owners(bridge_dir)
    assert {path.name: path.read_bytes() for path in bridge_dir.iterdir()} == snapshot
