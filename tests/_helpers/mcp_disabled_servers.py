"""Launch-wire activation shared by the four executor-hook scenarios."""

from omnigent.host.connect import _build_runner_env
from omnigent.host.frames import HostLaunchRunnerFrame, decode_host_frame, encode_host_frame
from omnigent.runner.identity import runner_mcp_disabled_servers_active_harnesses


def launch_activation(harnesses: list[str] | None) -> list[str] | None:
    """Send caller-owned activation through the codec and filtered runner environment."""
    frame = decode_host_frame(
        encode_host_frame(
            HostLaunchRunnerFrame(
                "request",
                "token",
                "/repo",
                mcp_disabled_servers_active_harnesses=harnesses,
            )
        )
    )
    assert isinstance(frame, HostLaunchRunnerFrame)
    env = _build_runner_env(
        {},
        server_url="http://server",
        runner_id="runner",
        binding_token="token",
        workspace="/repo",
        parent_pid=1,
        mcp_disabled_servers_active_harnesses=frame.mcp_disabled_servers_active_harnesses,
    )
    return runner_mcp_disabled_servers_active_harnesses(env)
