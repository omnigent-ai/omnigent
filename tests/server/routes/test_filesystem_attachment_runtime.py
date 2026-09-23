"""Attachment capability checks across mixed host/runner versions and replicas."""

from unittest.mock import Mock

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import HostHelloFrame, decode_host_frame, encode_host_frame
from omnigent.inner.native_attachments import CAP_FILESYSTEM_ATTACHMENTS
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame, decode_frame, encode_frame
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._sessions.helpers import require_filesystem_attachment_runtime


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
