"""Host dispatcher boundaries with all configuration and process effects mocked."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.host.frames import HostSetupRequestFrame, HostSetupTerminalFrame, SetupMethod
from omnigent.host.setup_transport import HostSetupDispatcher

pytestmark = pytest.mark.asyncio


async def test_active_operation_refuses_config_write(monkeypatch):
    apply = Mock()
    monkeypatch.setattr("omnigent.onboarding.setup_service.apply_setup_action", apply)
    dispatcher = HostSetupDispatcher()
    dispatcher._manager = Mock(has_active_operation=Mock(return_value=True))
    result = await dispatcher.request(
        HostSetupRequestFrame(
            "request",
            SetupMethod.ACTION,
            {"action": "set_opencode_model", "model": "fixture/model"},
        ),
        AsyncMock(),
    )
    assert result.error_status == 409
    apply.assert_not_called()


async def test_detached_and_wrong_operation_input_never_reaches_process():
    dispatcher = HostSetupDispatcher()
    manager = Mock(handle_terminal=AsyncMock(), detach=AsyncMock())
    dispatcher._manager = manager
    dispatcher._attachments["attachment"] = "operation"
    await dispatcher.terminal(HostSetupTerminalFrame("wrong", "attachment", {"data": "input"}))
    manager.handle_terminal.assert_not_called()
    await dispatcher.disconnect()
    manager.detach.assert_awaited_once_with("operation", "attachment")
    await dispatcher.terminal(HostSetupTerminalFrame("operation", "attachment", {"data": "input"}))
    manager.handle_terminal.assert_not_called()


async def test_tunnel_disconnect_during_attach_detaches_late_channel():
    dispatcher = HostSetupDispatcher()
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def attach(*args):
        entered.set()
        await resume.wait()
        return Mock(as_dict=Mock(return_value={"state": "running"}))

    manager = Mock(attach=AsyncMock(side_effect=attach), detach=AsyncMock())
    dispatcher._manager = manager
    task = asyncio.create_task(
        dispatcher.request(
            HostSetupRequestFrame(
                "request",
                SetupMethod.ATTACH,
                operation_id="operation",
                attachment_id="attachment",
            ),
            AsyncMock(),
        )
    )
    await entered.wait()
    await dispatcher.disconnect()
    resume.set()
    result = await task
    assert result.error_status == 409
    manager.detach.assert_awaited_once_with("operation", "attachment")
    assert not dispatcher._attachments


async def test_setup_failures_do_not_echo_raw_exception(monkeypatch, caplog):
    monkeypatch.setattr(
        "omnigent.onboarding.setup_service.get_setup_inventory",
        Mock(side_effect=RuntimeError("fixture-sensitive-value")),
    )
    result = await HostSetupDispatcher().request(
        HostSetupRequestFrame("request", SetupMethod.INVENTORY),
        AsyncMock(),
    )
    assert result.error_status == 502
    assert "fixture-sensitive-value" not in str(result)
    assert "fixture-sensitive-value" not in caplog.text


async def test_failed_save_and_secret_cleanup_returns_safe_host_error(monkeypatch, caplog):
    from omnigent.onboarding.setup_service import SetupPersistenceError

    monkeypatch.setattr(
        "omnigent.onboarding.setup_service.apply_setup_action",
        Mock(side_effect=SetupPersistenceError()),
    )
    result = await HostSetupDispatcher().request(
        HostSetupRequestFrame(
            "request",
            SetupMethod.ACTION,
            {"action": "set_harness_key", "harness": "cursor", "secret": "fixture-secret"},
        ),
        AsyncMock(),
    )
    assert result.error_status == 502
    assert result.error == "Setup was not saved; stored secret cleanup did not complete"
    assert "fixture-secret" not in repr(result)
    assert "fixture-secret" not in caplog.text


async def test_inventory_includes_only_available_guided_actions(monkeypatch):
    from omnigent.host.setup_operations import SetupOperationAction
    from omnigent.onboarding.setup_schema import SetupInventory

    monkeypatch.setattr(
        "omnigent.onboarding.setup_service.get_setup_inventory", lambda: SetupInventory()
    )
    dispatcher = HostSetupDispatcher()
    dispatcher._manager = Mock(
        supported_actions=Mock(return_value=(SetupOperationAction.CODEX_LOGIN,))
    )
    result = await dispatcher.request(
        HostSetupRequestFrame("request", SetupMethod.INVENTORY), AsyncMock()
    )
    assert result.error_status is None
    assert result.payload["supported_operations"] == ["codex-login"]


async def test_custom_import_detection_is_revalidated_on_host(monkeypatch):
    from omnigent.onboarding.setup_schema import SetupDetection, SetupDetectRequest

    detect = Mock(return_value=SetupDetection())
    monkeypatch.setattr("omnigent.onboarding.setup_service.detect_setup_connections", detect)
    dispatcher = HostSetupDispatcher()
    result = await dispatcher.request(
        HostSetupRequestFrame(
            "request",
            SetupMethod.DETECT,
            {
                "import_path": "/tmp/fixture-import.json",
                "import_source": "acpx",
            },
        ),
        AsyncMock(),
    )
    assert result.error_status is None
    detect.assert_called_once_with(
        SetupDetectRequest(import_path="/tmp/fixture-import.json", import_source="acpx")
    )


async def test_active_operation_refuses_explicit_detection(monkeypatch):
    detect = Mock()
    monkeypatch.setattr("omnigent.onboarding.setup_service.detect_setup_connections", detect)
    dispatcher = HostSetupDispatcher()
    dispatcher._manager = Mock(has_active_operation=Mock(return_value=True))
    result = await dispatcher.request(
        HostSetupRequestFrame("request", SetupMethod.DETECT),
        AsyncMock(),
    )
    assert result.error_status == 409
    detect.assert_not_called()


async def test_missing_prerequisite_retains_safe_actionable_error():
    from omnigent.host.setup_operations import SetupOperationManager

    dispatcher = HostSetupDispatcher()
    dispatcher._manager = SetupOperationManager(executable_resolver=lambda _: None)
    result = await dispatcher.request(
        HostSetupRequestFrame("request", SetupMethod.START, {"action": "codex-login"}),
        AsyncMock(),
    )
    assert result.error_status == 503
    assert "tmux" in result.error
    assert "required" in result.error


async def test_verify_is_dispatched_to_host_operation_manager():
    dispatcher = HostSetupDispatcher()
    snapshot = Mock(as_dict=Mock(return_value={"state": "succeeded"}))
    manager = Mock(verify=AsyncMock(return_value=snapshot))
    dispatcher._manager = manager

    result = await dispatcher.request(
        HostSetupRequestFrame("request", SetupMethod.VERIFY, operation_id="operation"),
        AsyncMock(),
    )

    assert result.payload == {"state": "succeeded"}
    manager.verify.assert_awaited_once_with("operation")
