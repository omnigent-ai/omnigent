"""Attachment capability checks across mixed host/runner versions and replicas."""

from unittest.mock import Mock

import pytest

from omnigent.entities import MessageData, NewConversationItem, ResourceEventData
from omnigent.entities.session_observation import RunnerOfflineDecision
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import HostHelloFrame, decode_host_frame, encode_host_frame
from omnigent.inner.native_attachments import CAP_FILESYSTEM_ATTACHMENTS
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame, decode_frame, encode_frame
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._sessions.helpers import (
    _filesystem_attachment_in_history,
    require_filesystem_attachment_runtime,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore


@pytest.mark.parametrize("marker_page", ["expired", "current", "terminal-empty"])
def test_attachment_history_advances_by_source_cursor_after_marker_projection(
    db_uri: str, marker_page: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    files = SqlAlchemyFileStore(db_uri)
    parent = store.create_conversation()
    child = store.create_conversation(parent_conversation_id=parent.id, runner_id="runner-a")
    observed = store.record_session_observation(child.id, "running", response_id="child-turn")
    assert observed is not None
    decision = RunnerOfflineDecision(
        observation=observed,
        episode_id="offline-episode",
        error_labels={"omnigent.last_task_error_code": "runner_disconnected"},
    )
    assert store.record_runner_offline_decision(child.id, decision) is not None
    attached = files.create("sample.zip", bytes=4, session_id=parent.id)
    if marker_page != "terminal-empty":
        store.append(
            parent.id,
            [
                NewConversationItem(
                    type="message",
                    response_id="attached-turn",
                    data=MessageData(
                        role="user", content=[{"type": "input_file", "file_id": attached.id}]
                    ),
                )
            ],
        )
    markers = store.append(
        parent.id,
        [
            NewConversationItem(
                type="resource_event",
                response_id="child-turn",
                data=ResourceEventData(
                    event_type="session.subagent.returned",
                    resource_id=child.id,
                    resource_type="session",
                    resource={
                        "session_observation": observed.model_dump(),
                        "runner_offline_episode_id": decision.episode_id,
                    },
                ),
            )
            for _ in range(1000)
        ],
    )
    if marker_page != "current":
        store.replace_runner_id(child.id, "runner-b")
    first = store.list_items(parent.id, limit=1000, order="desc")
    assert bool(first.data) is (marker_page == "current")
    assert first.has_more is (marker_page != "terminal-empty")
    assert first.last_id == markers[0].id

    assert _filesystem_attachment_in_history(parent.id, store, files) == (
        None if marker_page == "terminal-empty" else "sample.zip"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host_capable", "runner_capable", "expected"),
    [
        (True, True, None),
        (True, None, None),
        (None, True, None),
        (False, None, ErrorCode.CONFLICT),
        (None, False, ErrorCode.CONFLICT),
        (True, False, ErrorCode.CONFLICT),
        (False, True, ErrorCode.CONFLICT),
        (None, None, ErrorCode.CONFLICT),
    ],
)
async def test_connected_builds_must_advertise_attachment_support(
    host_capable: bool | None, runner_capable: bool | None, expected: str | None
) -> None:
    """Legacy omitted tokens fail closed, including a stale runner under a new daemon."""
    hosts = HostRegistry()
    runners = TunnelRegistry()
    if host_capable is not None:
        hello = decode_host_frame(
            encode_host_frame(
                HostHelloFrame(
                    version="0.14.0",
                    frame_protocol_version=1,
                    name="host",
                    capabilities=[CAP_FILESYSTEM_ATTACHMENTS] if host_capable else [],
                )
            )
        )
        assert isinstance(hello, HostHelloFrame)
        hosts.register("host_test", Mock(), hello, owner=None)
    if runner_capable is not None:
        hello = decode_frame(
            encode_frame(
                HelloFrame(
                    runner_version="0.14.0",
                    frame_protocol_version=1,
                    capabilities=[CAP_FILESYSTEM_ATTACHMENTS] if runner_capable else [],
                )
            )
        )
        assert isinstance(hello, HelloFrame)
        runners.register("runner_test", Mock(), hello)
    kwargs = {
        "host_id": "host_test" if host_capable is not None else None,
        "runner_id": "runner_test" if runner_capable is not None else None,
        "host_registry": hosts,
        "tunnel_registry": runners,
    }
    if expected is None:
        require_filesystem_attachment_runtime(**kwargs)
    else:
        with pytest.raises(OmnigentError) as error:
            require_filesystem_attachment_runtime(**kwargs)
        assert error.value.code == expected


def test_attachment_check_on_wrong_replica_keeps_retry_signal() -> None:
    """Replica-local absence must not turn a reachable host into an upgrade error."""
    router = Mock()
    router.host_is_on_another_replica.return_value = True
    with pytest.raises(OmnigentError) as error:
        require_filesystem_attachment_runtime(
            host_id="host_remote",
            runner_id="runner_remote",
            host_registry=HostRegistry(),
            tunnel_registry=TunnelRegistry(),
            runner_router=router,
        )
    assert error.value.code == ErrorCode.WRONG_REPLICA
    router.host_is_on_another_replica.assert_called_once_with("host_remote")
