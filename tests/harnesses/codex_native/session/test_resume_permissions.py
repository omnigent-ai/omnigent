"""Resume permissions tests for Codex session."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.codex_native import app_server as codex_native_app_server
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    clear_bridge_state,
    read_bridge_state,
    write_bridge_state,
)
from tests.harnesses.codex_native.session._support import (
    _FakeCodexAppServerClient,
)


def test_clear_bridge_state_removes_stale_runtime_state(tmp_path: Path) -> None:
    """
    Clearing bridge state removes the stale runtime pointer.

    New app-server launches reuse the same bridge directory, so a leftover
    ``state.json`` must disappear before web-message forwarding can read
    it. A regression that leaves the old state in place would make
    ``read_bridge_state`` return the stale thread id here.

    :param tmp_path: Temporary bridge directory.
    :returns: None.
    """
    bridge_dir = tmp_path / "bridge"
    write_bridge_state(
        bridge_dir,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path="ws://127.0.0.1:1234",
            thread_id="019e96aa-0be2-7343-8d3b-6f914d60936b",
            codex_home=str(tmp_path / "codex-home"),
        ),
    )

    clear_bridge_state(bridge_dir)

    assert read_bridge_state(bridge_dir) is None


@pytest.mark.parametrize("retain_client", [False, True])
def test_preload_codex_thread_for_resume_manages_subscription(
    monkeypatch: pytest.MonkeyPatch,
    retain_client: bool,
) -> None:
    """
    Preloading uses Codex ``thread/resume`` before bridge state is exposed.

    This helper is the guard against a web turn racing ahead of the TUI
    and hitting ``turn/start`` on an app-server that has not loaded the
    persisted thread yet. If the request method or params regress, this
    test fails on the captured fake client request.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    fake_client = _FakeCodexAppServerClient()

    def fake_client_factory(*_args: Any, **_kwargs: Any) -> _FakeCodexAppServerClient:
        """
        Return the fake app-server client.

        :returns: Fake client.
        """
        return fake_client

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server.CodexAppServerClient",
        fake_client_factory,
    )

    retained = asyncio.run(
        codex_native_app_server.preload_codex_thread_for_resume(
            "ws://127.0.0.1:1234",
            "019e96aa-0be2-7343-8d3b-6f914d60936b",
            terminal_launch_args=[
                "-c",
                'default_permissions=":danger-full-access"',
                "-c",
                'approval_policy="never"',
                "-c",
                'approvals_reviewer="auto_review"',
            ],
            retain_client=retain_client,
        )
    )

    assert fake_client.connected is True
    assert fake_client.requests == [
        (
            "thread/resume",
            {
                "threadId": "019e96aa-0be2-7343-8d3b-6f914d60936b",
                "excludeTurns": True,
                "permissions": ":danger-full-access",
                "approvalPolicy": "never",
                "approvalsReviewer": "auto_review",
            },
        )
    ]
    assert fake_client.closed is not retain_client
    assert retained is (fake_client if retain_client else None)


@pytest.mark.parametrize("retain_client", [False, True])
@pytest.mark.parametrize("stage", ["connect", "request"])
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
def test_preload_codex_thread_closes_client_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    retain_client: bool,
    stage: str,
    error: type[BaseException],
) -> None:
    """Failed or cancelled startup must not leave a preload subscription open."""
    fake_client = _FakeCodexAppServerClient()

    async def fail(*_args: Any, **_kwargs: Any) -> None:
        raise error("preload failed")

    monkeypatch.setattr(fake_client, stage, fail)
    monkeypatch.setattr(
        codex_native_app_server, "client_for_transport", lambda *_args, **_kwargs: fake_client
    )
    with pytest.raises(error, match="preload failed"):
        asyncio.run(
            codex_native_app_server.preload_codex_thread_for_resume(
                "ws://127.0.0.1:9876", "thread_test", retain_client=retain_client
            )
        )
    assert fake_client.closed


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            codex_native_app_server.CodexAppServerResponseError(
                {
                    "code": -32603,
                    "message": (
                        "failed to read thread: thread-store internal error: failed to "
                        "load thread history /codex-home/sessions/rollout.jsonl: stream "
                        "did not contain valid UTF-8"
                    ),
                }
            ),
            True,
        ),
        (
            codex_native_app_server.CodexAppServerResponseError(
                {
                    "code": -32603,
                    "message": (
                        "error resuming thread: Fatal error: Failed to initialize "
                        "session: thread-store internal error: failed to resume local "
                        "thread recorder: final paginated rollout record at "
                        "/codex-home/sessions/rollout.jsonl is missing an ordinal"
                    ),
                }
            ),
            True,
        ),
        (
            codex_native_app_server.CodexAppServerResponseError(
                {
                    "code": -32600,
                    "message": (
                        "invalid paginated history lineage for "
                        "019e96aa-0be2-7343-8d3b-6f914d60936d: "
                        "source rollout is not paginated"
                    ),
                }
            ),
            True,
        ),
        (
            codex_native_app_server.CodexAppServerResponseError(
                {"code": -32603, "message": "internal error: something unrelated"}
            ),
            False,
        ),
        (
            codex_native_app_server.CodexAppServerResponseError(
                {"code": -32600, "message": "thread 019e already has an active writer"}
            ),
            False,
        ),
        (RuntimeError("thread-store internal error: not an app-server error"), False),
    ],
)
def test_is_unreadable_thread_error(exc: BaseException, expected: bool) -> None:
    """
    Only codex's thread-store read failure and its unpaginated-lineage
    rejection count as an unreadable thread.

    A refused resume (another writer holds the thread) and a plain runtime
    error must keep failing loud rather than silently starting a fresh thread.

    :param exc: Exception raised by the resume request.
    :param expected: Whether it classifies as an unreadable thread.
    """
    assert codex_native_app_server.is_unreadable_thread_error(exc) is expected


def test_codex_resume_permission_params_parse_legacy_flags() -> None:
    """Legacy approval and sandbox flags become preload overrides."""
    assert codex_native_app_server._codex_resume_permission_params(
        ["-a", "on-failure", "-s=read-only"]
    ) == {
        "approvalPolicy": "on-failure",
        "sandbox": "read-only",
    }


def test_codex_resume_permission_params_repairs_legacy_full_access_profile() -> None:
    """An incomplete stored Full Access profile resumes as the matching preset."""
    args = [
        "-c",
        'default_permissions=":danger-full-access"',
        "-c",
        'approvals_reviewer="user"',
    ]

    assert codex_native_app_server._codex_resume_permission_params(args) == {
        "permissions": ":danger-full-access",
        "approvalPolicy": "never",
        "approvalsReviewer": "user",
    }
    assert codex_native_app_server.build_codex_remote_args(
        codex_args=tuple(args),
        thread_id="thread_x",
        remote_url="ws://127.0.0.1:9876",
    ) == [
        "resume",
        "--remote",
        "ws://127.0.0.1:9876",
        "thread_x",
    ]


@pytest.mark.parametrize(
    ("permission_args", "expected_permissions"),
    [
        ((), {"approvalsReviewer": "auto_review"}),
        (
            ("-a", "on-failure", "-s=read-only"),
            {"approvalPolicy": "on-failure", "sandbox": "read-only"},
        ),
        (
            ("--ask-for-approval=on-request", "--sandbox", "workspace-write"),
            {"approvalPolicy": "on-request", "sandbox": "workspace-write"},
        ),
        (
            (
                "--config",
                'sandbox_mode="read-only"',
                '-c=approval_policy="on-request"',
                "-c",
                'approvals_reviewer="auto_review"',
            ),
            {
                "sandbox": "read-only",
                "approvalPolicy": "on-request",
                "approvalsReviewer": "auto_review",
            },
        ),
        (
            (
                '--config=default_permissions=":danger-full-access"',
                "-c",
                'approvals_reviewer="user"',
            ),
            {
                "permissions": ":danger-full-access",
                "approvalPolicy": "never",
                "approvalsReviewer": "user",
            },
        ),
        (
            ("--dangerously-bypass-approvals-and-sandbox",),
            {"approvalPolicy": "never", "sandbox": "danger-full-access"},
        ),
    ],
)
def test_remote_resume_applies_permissions_only_on_app_server(
    monkeypatch: pytest.MonkeyPatch,
    permission_args: tuple[str, ...],
    expected_permissions: dict[str, str],
) -> None:
    """Remote attachment must not repeat the policy already applied by preload."""
    fake_client = _FakeCodexAppServerClient()
    monkeypatch.setattr(
        codex_native_app_server,
        "client_for_transport",
        lambda *_args, **_kwargs: fake_client,
    )
    model_args = ("--model", "test-model", "-c", 'model_reasoning_effort="high"')
    launch_args = (*permission_args, *model_args)
    asyncio.run(
        codex_native_app_server.preload_codex_thread_for_resume(
            "ws://127.0.0.1:9876", "thread_test", terminal_launch_args=launch_args
        )
    )
    assert fake_client.requests == [
        (
            "thread/resume",
            {"threadId": "thread_test", "excludeTurns": True, **expected_permissions},
        )
    ]
    assert fake_client.closed
    assert codex_native_app_server.build_codex_remote_args(
        codex_args=launch_args,
        thread_id="thread_test",
        remote_url="ws://127.0.0.1:9876",
        config_overrides=('model_provider="test-provider"',),
    ) == [
        "-c",
        'model_provider="test-provider"',
        *model_args,
        "resume",
        "--remote",
        "ws://127.0.0.1:9876",
        "thread_test",
    ]


@pytest.mark.parametrize("codex_cli_version", [None, (0, 154, 0), (0, 155, 0)])
def test_remote_resume_omits_app_server_permission_config(
    codex_cli_version: tuple[int, int, int] | None,
) -> None:
    """Server config overrides also stay off the remote terminal's command line."""
    overrides = (
        'approval_policy="never"',
        'sandbox_mode="danger-full-access"',
        "sandbox_workspace_write.network_access=false",
        'model_provider="test-provider"',
    )
    assert codex_native_app_server.build_codex_remote_args(
        codex_args=(),
        thread_id="thread_test",
        remote_url="ws://127.0.0.1:9876",
        config_overrides=overrides,
        codex_cli_version=codex_cli_version,
        bypass_sandbox=True,
        bypass_hook_trust=True,
    ) == [
        "-c",
        'model_provider="test-provider"',
        "--dangerously-bypass-hook-trust",
        "resume",
        "--remote",
        "ws://127.0.0.1:9876",
        "thread_test",
    ]


@pytest.mark.parametrize("config_flag", ["-c", "--config", "-c=", "--config="])
@pytest.mark.parametrize(
    ("assignment", "expected_config"),
    [
        (
            "sandbox_workspace_write.network_access=false",
            {"sandbox_workspace_write.network_access": False},
        ),
        (
            'sandbox_workspace_write.writable_roots=["/tmp/test-workspace"]',
            {"sandbox_workspace_write.writable_roots": ["/tmp/test-workspace"]},
        ),
        (
            "sandbox_workspace_write={network_access=false, exclude_tmpdir_env_var=true}",
            {"sandbox_workspace_write": {"network_access": False, "exclude_tmpdir_env_var": True}},
        ),
        ("network.enabled=false", {"network.enabled": False}),
        (
            "permissions.restricted.network.enabled=false",
            {"permissions.restricted.network.enabled": False},
        ),
        (
            "network.proxy_url=http://127.0.0.1:8080",
            {"network.proxy_url": "http://127.0.0.1:8080"},
        ),
        (
            "permissions={restricted={network={enabled=false}}}",
            {"permissions": {"restricted": {"network": {"enabled": False}}}},
        ),
        ("network.enabled=not-a-boolean", {"network.enabled": "not-a-boolean"}),
        (
            "network.proxy_url= 'http://127.0.0.1:8080 ",
            {"network.proxy_url": "http://127.0.0.1:8080"},
        ),
        (
            "sandbox_workspace_write.writable_roots=[",
            {"sandbox_workspace_write.writable_roots": "["},
        ),
        (
            "sandbox_workspace_write={network_access=false,network_access=true}",
            {"sandbox_workspace_write": "{network_access=false,network_access=true}"},
        ),
        (
            "network.proxy_url= \"'http://127.0.0.1:8080'\"' ",
            {"network.proxy_url": "http://127.0.0.1:8080"},
        ),
    ],
)
def test_remote_resume_transfers_permission_config_to_preload(
    monkeypatch: pytest.MonkeyPatch,
    config_flag: str,
    assignment: str,
    expected_config: dict[str, object],
) -> None:
    fake_client = _FakeCodexAppServerClient()
    monkeypatch.setattr(
        codex_native_app_server, "client_for_transport", lambda *_args, **_kwargs: fake_client
    )
    config_args = (
        (config_flag + assignment,) if config_flag.endswith("=") else (config_flag, assignment)
    )
    launch_args = ("--sandbox", "workspace-write", "--ask-for-approval", "never", *config_args)

    asyncio.run(
        codex_native_app_server.preload_codex_thread_for_resume(
            "ws://127.0.0.1:9876", "thread_test", terminal_launch_args=launch_args
        )
    )

    assert fake_client.requests == [
        (
            "thread/resume",
            {
                "threadId": "thread_test",
                "excludeTurns": True,
                "sandbox": "workspace-write",
                "approvalPolicy": "never",
                "config": expected_config,
            },
        )
    ]
    assert codex_native_app_server.build_codex_remote_args(
        codex_args=launch_args,
        thread_id="thread_test",
        remote_url="ws://127.0.0.1:9876",
        codex_cli_version=(0, 155, 0),
    ) == ["resume", "--remote", "ws://127.0.0.1:9876", "thread_test"]
    assert codex_native_app_server.build_codex_remote_args(
        codex_args=launch_args,
        thread_id="thread_test",
        remote_url="ws://127.0.0.1:9876",
        codex_cli_version=(0, 153, 1),
    ) == [*launch_args, "resume", "--remote", "ws://127.0.0.1:9876", "thread_test"]
    assert codex_native_app_server.build_codex_remote_args(
        codex_args=launch_args,
        thread_id=None,
        remote_url="ws://127.0.0.1:9876",
        codex_cli_version=(0, 155, 0),
    ) == [*launch_args, "--remote", "ws://127.0.0.1:9876"]


def test_remote_resume_merges_permission_config_without_dropping_profile() -> None:
    launch_args = (
        "--sandbox",
        "workspace-write",
        "-c",
        "default_permissions=restricted",
        "-c",
        "permissions.restricted.network.enabled=false",
        "-c",
        "network.enabled=true",
        "--config=network.enabled=false",
        "-c",
        'model="test-model"',
    )

    assert codex_native_app_server._codex_resume_permission_params(launch_args) == {
        "permissions": "restricted",
        "config": {"permissions.restricted.network.enabled": False, "network.enabled": False},
    }
    assert codex_native_app_server.build_codex_remote_args(
        codex_args=launch_args,
        thread_id="thread_test",
        remote_url="ws://127.0.0.1:9876",
    ) == ["-c", 'model="test-model"', "resume", "--remote", "ws://127.0.0.1:9876", "thread_test"]


@pytest.mark.parametrize(
    ("assignments", "expected_config"),
    [
        (
            ("network.enabled=false", "network={enabled=true}"),
            {"network": {"enabled": True}},
        ),
        (
            ("network={enabled=true,proxy_port=8080}", "network.enabled=false"),
            {"network": {"enabled": False, "proxy_port": 8080}},
        ),
        (
            (
                "permissions={restricted={network={enabled=true}}}",
                "permissions.restricted.network.enabled=false",
            ),
            {"permissions": {"restricted": {"network": {"enabled": False}}}},
        ),
        (
            (
                "permissions.restricted.network.enabled=true",
                "permissions.restricted={network={enabled=false}}",
                "permissions.restricted.network.proxy_port=8080",
            ),
            {"permissions.restricted": {"network": {"enabled": False, "proxy_port": 8080}}},
        ),
        (
            ("permissions.restricted=false", "permissions.restricted.network.enabled=false"),
            {"permissions.restricted": {"network": {"enabled": False}}},
        ),
    ],
)
def test_remote_resume_preserves_overlapping_permission_config_order(
    assignments: tuple[str, ...], expected_config: dict[str, object]
) -> None:
    launch_args = (
        "--sandbox",
        "workspace-write",
        *(f"-c={assignment}" for assignment in assignments),
    )
    assert codex_native_app_server._codex_resume_permission_params(launch_args) == {
        "sandbox": "workspace-write",
        "config": expected_config,
    }
    assert codex_native_app_server.build_codex_remote_args(
        codex_args=launch_args,
        thread_id="thread_test",
        remote_url="ws://127.0.0.1:9876",
    ) == ["resume", "--remote", "ws://127.0.0.1:9876", "thread_test"]


@pytest.mark.parametrize("codex_cli_version", [(0, 136, 0), (0, 153, 0), (0, 153, 1)])
def test_remote_resume_preserves_legacy_bypass_args(
    codex_cli_version: tuple[int, int, int],
) -> None:
    """Older TUIs need the bypass settings even after app-server preload."""
    assert codex_native_app_server.build_codex_remote_args(
        codex_args=("-a", "on-request", "-s", "read-only", "--model", "test-model"),
        thread_id="thread_test",
        remote_url="ws://127.0.0.1:9876",
        config_overrides=('approval_policy="never"', 'sandbox_mode="danger-full-access"'),
        codex_cli_version=codex_cli_version,
        bypass_sandbox=True,
    ) == [
        "-c",
        'approval_policy="never"',
        "-c",
        'sandbox_mode="danger-full-access"',
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        "test-model",
        "resume",
        "--remote",
        "ws://127.0.0.1:9876",
        "thread_test",
    ]


@pytest.mark.parametrize(
    "args",
    [
        ("--config", "sandbox_workspace_write.network_access=1979-05-27"),
        ("--config", "network.proxy_port=nan"),
        ("--config", "network.proxy_port=inf"),
        ("--config", "permissions.restricted={network={enabled=1979-05-27}}"),
        ("--config", "network.enabled"),
        ("--config", "networking.enabled=false"),
        ("--config", "permissions_extra.enabled=false"),
        ("--full-auto",),
        ("-c", "approvals_reviewer=false"),
        ("--config", 'sandbox_mode=""'),
        ("-c",),
        ("--config",),
        ("-c", 'developer_instructions="Do not change approval_policy"'),
    ],
)
def test_remote_resume_preserves_settings_not_applied_by_preload(args: tuple[str, ...]) -> None:
    """Do not silently discard unsupported policy settings or unrelated config."""
    assert codex_native_app_server.build_codex_remote_args(
        codex_args=args,
        thread_id="thread_test",
        remote_url="ws://127.0.0.1:9876",
    ) == [*args, "resume", "--remote", "ws://127.0.0.1:9876", "thread_test"]
