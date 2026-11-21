"""Reading a pull request on a configured code host instance, across real processes.

The server turns instances on, the host loads its own instance file and advertises
it, and a pull request on the instance's host is read through the instance's CLI by
the host's runner and, once that runner is gone, by the host itself. Only the CLI's
answers are fixtures, and no plain ``gh`` is on ``PATH``.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.server_runner import server_runner
from tests.e2e.conftest import lookup_agent_id, upload_agent
from tests.e2e.test_host_e2e import _runner_pid_from_daemon_log, _write_smoke_agent_yaml

pytestmark = pytest.mark.timeout(300)

_HOST = "code.example.test"
_URL = f"https://{_HOST}/example/app/pull/42"
_PATCH = (
    "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
    "@@ -1 +1 @@\n-before\n+after\n"
)
# The instance's CLI: logs each call with its environment's names, answers reads only.
_INSTANCE_CLI = r"""
import json, os, pathlib, sys
root = pathlib.Path(__file__).parent
args = sys.argv[1:]
with (root / 'instance-calls.jsonl').open('a') as log:
    log.write(json.dumps({'argv': args, 'env': sorted(os.environ),
                          'gh_host': os.environ.get('GH_HOST')}) + '\n')
fixture = json.loads((root / 'fixture.json').read_text())
pr = {'number': 42, 'url': fixture['url'], 'title': 'Instance read proof', 'state': 'OPEN',
      'isDraft': False, 'author': {'login': 'octocat'}, 'baseRefName': 'main',
      'headRefName': 'topic', 'body': 'PR description', 'comments': [],
      'statusCheckRollup': []}
if args[:2] == ['api', 'user']:
    print(json.dumps({'login': 'octocat'}))
elif args[:2] == ['pr', 'view']:
    print(json.dumps(pr))
elif args[:2] == ['pr', 'diff']:
    print(fixture['patch'], end='')
elif args[:1] == ['api'] and any('/files' in arg for arg in args):
    files = [{'filename': 'file.txt', 'status': 'modified', 'additions': 1, 'deletions': 1}]
    print(json.dumps([files] if '--slurp' in args else files))
else:
    raise SystemExit(1)
"""


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)
    return path


def _calls(path: Path) -> list:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_pull_request_on_an_instance_host_is_read_through_the_instance_cli(
    tmp_path: Path,
) -> None:
    instance_bin = tmp_path / "instance-bin"
    instance_cli = _script(instance_bin / "example-cli", _INSTANCE_CLI)
    (instance_bin / "fixture.json").write_text(json.dumps({"url": _URL, "patch": _PATCH}))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(workspace)], check=True)
    base_env = {name: os.environ[name] for name in ("LANG", "TMPDIR") if name in os.environ}
    # No plain gh anywhere on PATH: the instance's CLI must be enough on its own.
    base_env["PATH"] = os.pathsep.join(
        directory
        for directory in os.environ["PATH"].split(os.pathsep)
        if directory and not (Path(directory) / "gh").exists()
    )
    assert shutil.which("gh", path=base_env["PATH"]) is None

    with server_runner(
        tmp_path / "stack",
        server_env={"OMNIGENT_FEATURES": "code_host_instances"},
        base_env=base_env,
        workspace=workspace,
    ) as stack:
        config_dir = stack.runner_home / ".omnigent"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "code_host_instances.toml").write_text(
            "[[instance]]\n"
            'id = "internal"\nprovider = "github"\ndisplay_name = "Internal code host"\n'
            f'hosts = ["{_HOST}"]\ncli = ["{instance_cli}"]\nenv = ["HOME"]\nauth = "cli"\n'
            'capabilities = ["line_counts", "linked_pr_diff", "comment_posting"]\n'
        )
        # A token in the host's environment must never reach the instance's CLI.
        host_log = tmp_path / "host-process.log"
        stack.start_host(
            env={"GH_TOKEN": "synthetic-host-token", PROCESS_LOG_FILE_ENV_VAR: str(host_log)}
        )

        with httpx.Client(base_url=stack.base_url, timeout=60, trust_env=False) as client:
            (host,) = [
                row
                for row in client.get("/v1/hosts").json()["hosts"]
                if row["host_id"] == stack.host_id
            ]
            assert host["code_host_instances"] == [
                {"id": "internal", "display_name": "Internal code host", "hosts": [_HOST]}
            ]
            assert str(instance_cli) not in json.dumps(host)

            agent_dir = _write_smoke_agent_yaml(tmp_path)
            with (agent_dir / "host-e2e-agent.yaml").open("a") as spec:
                spec.write(
                    "os_env:\n  type: caller_process\n  cwd: .\n  sandbox:\n    type: none\n"
                )
            agent = upload_agent(client, agent_dir)
            created = client.post(
                "/v1/sessions", json={"agent_id": lookup_agent_id(client, agent)}
            )
            created.raise_for_status()
            session = created.json()["id"]
            launch = client.post(
                f"/v1/hosts/{stack.host_id}/runners",
                json={"session_id": session, "workspace": str(workspace)},
            )
            launch.raise_for_status()
            runner = launch.json()["runner_id"]
            deadline = time.monotonic() + 60
            while not client.get(f"/v1/runners/{runner}/status").json().get("online"):
                assert time.monotonic() < deadline, "runner did not come online"
                time.sleep(0.2)
            client.patch(f"/v1/sessions/{session}", json={"runner_id": runner}).raise_for_status()
            resource = f"/v1/sessions/{session}/resources/github"
            attached = client.post(f"{resource}/prs", json={"url": _URL, "action": "attach"})
            assert attached.status_code == 200, attached.text

            for transport in ("runner", "host"):
                if transport == "host":
                    pid = _runner_pid_from_daemon_log(host_log)
                    assert pid is not None
                    os.kill(pid, signal.SIGTERM)
                    deadline = time.monotonic() + 30
                    while client.get(f"/v1/runners/{runner}/status").json().get("online"):
                        assert time.monotonic() < deadline, "runner did not go offline"
                        time.sleep(0.2)
                info = client.get(resource, params={"pr_url": _URL})
                assert info.status_code == 200, info.text
                payload = info.json()
                assert payload["pr"]["title"] == "Instance read proof", (transport, payload)
                (listed,) = [pr for pr in payload["prs"] if pr["url"] == _URL]
                assert listed["provider_display"]["display_name"] == "Internal code host"
                offered = {name for name, on in payload["capabilities"].items() if on}
                assert "comment_posting" not in offered, (transport, offered)
                diff = client.get(f"{resource}/diff", params={"pr_url": _URL})
                assert diff.status_code == 200 and diff.json()["patch"] == _PATCH, diff.text

    instance_calls = _calls(instance_bin / "instance-calls.jsonl")
    assert instance_calls, "the instance's CLI was never run"
    for call in instance_calls:
        assert call["gh_host"] == _HOST
        assert set(call["env"]) <= {
            "PATH",
            "HOME",
            "GH_HOST",
            "LC_CTYPE",
            "__CF_USER_TEXT_ENCODING",
        }
