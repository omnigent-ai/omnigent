"""Use `python -m dev.repro_env exec -- <command>` inside each agent shell."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path

import httpx

from .runtime import serve
from .transport import Relay


@contextmanager
def command_environment(output: Path):
    state = json.loads((output / "environment.json").read_text())
    if state["status"] != "ready":
        raise RuntimeError(f"Reproduction environment is {state['status']}; inspect {output}")
    with (
        Relay(unix_target=output / "server.sock") as server,
        Relay(unix_target=output / "model.sock") as model,
    ):
        env = dict(os.environ)
        env.update(
            OMNIGENT_REPRO_SERVER_URL=f"http://127.0.0.1:{server.port}",
            OMNIGENT_REPRO_MODEL_URL=f"http://127.0.0.1:{model.port}",
            OMNIGENT_REPRO_RUNNER_ID=state["runner_id"],
        )
        for key in ("NO_PROXY", "no_proxy"):
            env[key] = ",".join(filter(None, (env.get(key), "localhost,127.0.0.1,::1")))
        with httpx.Client(trust_env=False, timeout=5) as client:
            client.get(f"{env['OMNIGENT_REPRO_MODEL_URL']}/stats").raise_for_status()
            status = client.get(
                f"{env['OMNIGENT_REPRO_SERVER_URL']}/v1/runners/{state['runner_id']}/status"
            )
            status.raise_for_status()
            if not status.json().get("online"):
                raise RuntimeError(f"Reproduction runner is offline; inspect {output}")
        yield env


def execute(output: Path, command: list[str]) -> int:
    if (output / "execution-context.json").is_file():
        from .execution import run

        return run(output, command, prepare=lambda: command_environment(output))
    with command_environment(output) as env:
        return subprocess.call(command, env=env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".omnigent/repro-env"))
    commands = parser.add_subparsers(dest="action", required=True)
    start = commands.add_parser("serve")
    start.add_argument("--lease-seconds", type=int, default=21600)
    run = commands.add_parser("exec")
    run.add_argument("command", nargs=argparse.REMAINDER)
    commands.add_parser("stop")
    commands.add_parser("status")
    check = commands.add_parser("doctor")
    check.add_argument("--plan", type=Path, required=True)
    check.add_argument("--host", help="Exact host ID required by the journey")
    check.add_argument("--json", action="store_true", help="Emit only the observation JSON")
    check.add_argument(
        "--check",
        nargs=3,
        action="append",
        default=[],
        metavar=("REQUIREMENT_ID", "FACT", "EXPECTED_JSON"),
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if args.action == "serve":
        return serve(output, args.lease_seconds)
    if args.action == "stop":
        (output / "stop").touch()
        return 0
    if args.action == "status":
        print((output / "environment.json").read_text())
        return 0
    if args.action == "doctor":
        from .doctor import doctor

        doctor(output, args.plan, args.check, args.host, json_only=args.json)
        return 0
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("exec requires a command after --")
    return execute(output, command)


if __name__ == "__main__":
    raise SystemExit(main())
