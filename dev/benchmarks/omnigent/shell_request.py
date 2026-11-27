"""Compare reusable and disposable Linux shell-request latency."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from functools import partial
from pathlib import Path

from omnigent.inner._bwrap_shell_request import request_launcher
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.sandbox import resolve_sandbox
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runner.tool_dispatch import _execute_os_env_tool
from omnigent.spec.types import AgentSpec


async def measure(workspace: Path, iterations: int) -> dict[str, object]:
    os_env = OSEnvSpec(
        type="caller_process",
        cwd=str(workspace),
        sandbox=OSEnvSandboxSpec(type="linux_bwrap"),
    )
    if request_launcher(resolve_sandbox(os_env, workspace)) is None:
        raise RuntimeError("Requires Linux with the isolated Bubblewrap request launcher")
    spec = AgentSpec(spec_version=1, os_env=os_env)
    (workspace / "note.txt").write_text("ready\n")
    registry = SessionResourceRegistry()
    execute = partial(_execute_os_env_tool, agent_spec=spec, conversation_id="shell-latency")
    samples: dict[str, list[float]] = {"warm": [], "disposable": []}
    try:
        ready = json.loads(
            await execute("sys_os_read", {"path": "note.txt"}, resource_registry=registry)
        )
        if ready.get("content") != "ready\n":
            raise RuntimeError(f"Could not warm the file-tool environment: {ready}")
        for mode, owner in (("warm", registry), ("disposable", None)):
            for _ in range(iterations):
                began = time.perf_counter()
                result = json.loads(
                    await execute(
                        "sys_os_shell",
                        {"command": "printf verified", "timeout": 10},
                        resource_registry=owner,
                    )
                )
                elapsed = time.perf_counter() - began
                if result.get("stdout") != "verified" or result.get("exit_code") != 0:
                    raise RuntimeError(f"Cannot measure an unsuccessful {mode} call: {result}")
                samples[mode].append(elapsed)
    finally:
        await registry.cleanup_session("shell-latency")
    return {
        "entry_point": "runner-local sys_os_shell",
        "backend": "linux_bwrap",
        "provider_calls": 0,
        "samples_seconds": samples,
        "median_seconds": {mode: statistics.median(values) for mode, values in samples.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    with tempfile.TemporaryDirectory(prefix="omnigent-shell-latency-") as directory:
        report = asyncio.run(measure(Path(directory), args.iterations))
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output is not None:
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
