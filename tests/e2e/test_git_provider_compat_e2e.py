"""GitHub resource contracts across real server, runner, and host versions.

Only forge responses and model replies are fixtures. HTTP/WebSocket routing,
CLI exports, git, host launch, persistence, and runner-offline fallback are real.
The existing compatibility environment variables select released processes.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.conftest import (
    configure_mock_llm,
    lookup_agent_id,
    poll_session_until_terminal,
    send_user_message_to_session,
    upload_agent,
)
from tests.e2e.test_host_e2e import (
    _runner_pid_from_daemon_log,
    _spawn_host_daemon,
    _wait_for_host_online,
    _write_smoke_agent_yaml,
)

pytestmark = [pytest.mark.compat_smoke, pytest.mark.timeout(180)]

_URL = "https://github.com/example/compat/pull/42"
_PATCH = (
    "diff --git a/file.txt b/file.txt\n--- a/file.txt\n+++ b/file.txt\n"
    "@@ -1 +1 @@\n-before\n+after\n"
)
_GH = r"""
import base64, json, pathlib, sys
root = pathlib.Path(__file__).parent
data = json.loads((root / 'forge.json').read_text())
args = sys.argv[1:]
with (root / 'calls.jsonl').open('a') as log:
    log.write(json.dumps(args) + '\n')
mode = data.get('mode', 'ready')
if mode != 'ready' and (args[:2] == ['pr', 'view'] or args[0] == 'api'):
    raise SystemExit(1)
if args[:2] == ['auth', 'token']:
    print('fixture-token')
elif args[:2] == ['auth', 'status']:
    print(json.dumps({'hosts':{}}))
    raise SystemExit(1)
elif args[:2] == ['repo', 'view']:
    if mode == 'signed_out': raise SystemExit(1)
    print(json.dumps({'nameWithOwner':'example/compat'}))
elif args[:2] == ['repo', 'set-default']:
    print('example/compat')
elif args[:2] == ['pr', 'view']:
    pr = dict(data['pr'])
    if len(args) > 2 and args[2].isdigit():
        pr.update(number=int(args[2]), url=pr['url'].rsplit('/', 1)[0]+'/'+args[2])
    print(json.dumps(pr))
elif args[:2] == ['pr', 'diff']:
    print(data['patch'], end='')
elif args[0] == 'api':
    endpoint = next(arg for arg in args if arg.startswith('repos/'))
    if '/files?' in endpoint:
        files = [{'filename':'file.txt', 'status':'modified', 'additions':1, 'deletions':1}]
        print(json.dumps([files] if '--slurp' in args else files))
    elif '/contents/' in endpoint:
        content = 'after\n' if endpoint.endswith(data['head']) else 'before\n'
        encoded = base64.b64encode(content.encode()).decode()
        print(json.dumps({'encoding':'base64', 'content':encoded}))
    elif '/compare/' in endpoint:
        print(json.dumps({'merge_base_commit':{'sha':data['base']}}))
    else:
        print(json.dumps({
            'head':{'sha':data['head'], 'repo':{'full_name':'example/compat'}},
            'base':{'sha':data['base']}}))
else:
    raise SystemExit('unexpected fixture command: '+repr(args))
"""


def test_github_resources_survive_mixed_versions_and_parked_runner(
    live_server: str,
    http_client: httpx.Client,
    mock_llm_server_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read and mutate the same GitHub session through runner, then host."""

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(tmp_path), *args], text=True, stderr=subprocess.PIPE
        ).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Compatibility test")
    git("config", "user.email", "test@example.com")
    (tmp_path / "file.txt").write_text("before\n")
    git("add", "file.txt")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("checkout", "-b", "topic")
    (tmp_path / "file.txt").write_text("after\n")
    git("commit", "-am", "head")
    head = git("rev-parse", "HEAD")
    git("remote", "add", "origin", "https://github.com/example/compat.git")
    binary = tmp_path / "fixture-bin"
    binary.mkdir()
    (binary / "gh").write_text(f"#!{sys.executable}\n" + _GH)
    (binary / "gh").chmod(0o755)
    (binary / "forge.json").write_text(
        json.dumps(
            {
                "base": base,
                "head": head,
                "patch": _PATCH,
                "pr": {
                    "number": 42,
                    "url": _URL,
                    "title": "GitHub compatibility proof",
                    "state": "OPEN",
                    "isDraft": False,
                    "author": {"login": "octocat"},
                    "baseRefName": "main",
                    "headRefName": "topic",
                    "body": "PR description",
                    "comments": [
                        {
                            "author": {"login": "reviewer"},
                            "body": "Looks good",
                            "createdAt": "2026-10-01T00:00:00Z",
                        }
                    ],
                    "statusCheckRollup": [
                        {
                            "__typename": "CheckRun",
                            "name": "unit",
                            "status": "COMPLETED",
                            "conclusion": "SUCCESS",
                        }
                    ],
                },
            }
        )
    )
    monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
    # The host and every runner it launches share an isolated persistent config.
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / ".omnigent"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    configure_mock_llm(mock_llm_server_url, [{"text": "COMPAT_READY"}])
    daemon = _spawn_host_daemon(
        tmp_path=tmp_path, live_server=live_server, mock_llm_server_url=mock_llm_server_url
    )
    try:
        _wait_for_host_online(http_client, daemon.host_id)
        agent_dir = _write_smoke_agent_yaml(tmp_path)
        with (agent_dir / "host-e2e-agent.yaml").open("a") as spec:
            spec.write(
                f"os_env:\n  type: caller_process\n  cwd: {tmp_path}\n  sandbox:\n    type: none\n"
            )
        agent = upload_agent(http_client, agent_dir)
        response = http_client.post(
            "/v1/sessions", json={"agent_id": lookup_agent_id(http_client, agent)}
        )
        response.raise_for_status()
        session = response.json()["id"]
        launch = http_client.post(
            f"/v1/hosts/{daemon.host_id}/runners",
            json={"session_id": session, "workspace": str(tmp_path)},
            timeout=60,
        )
        launch.raise_for_status()
        runner = launch.json()["runner_id"]
        deadline = time.monotonic() + 30
        while not http_client.get(f"/v1/runners/{runner}/status").json().get("online"):
            assert time.monotonic() < deadline, "runner did not come online"
            time.sleep(0.2)
        http_client.patch(f"/v1/sessions/{session}", json={"runner_id": runner}).raise_for_status()
        response_id = send_user_message_to_session(
            http_client, session_id=session, content="Say COMPAT_READY"
        )
        turn = poll_session_until_terminal(
            http_client, session_id=session, response_id=response_id, timeout=60
        )
        assert turn["status"] == "completed", turn

        export = subprocess.run(
            [
                runner_executable(),
                "-m",
                "omnigent",
                "session",
                "export",
                "--id",
                session,
                "--server",
                live_server,
                "--output",
                str(tmp_path / "export.jsonl"),
            ],
            cwd=compat_runner_cwd(),
            env=apply_runner_env(dict(os.environ)),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert export.returncode == 0, export.stderr
        assert "COMPAT_READY" in (tmp_path / "export.jsonl").read_text()
        resource = f"/v1/sessions/{session}/resources/github"

        def get(suffix: str = "", **params: str) -> dict:
            result = http_client.get(resource + suffix, params=params, timeout=30)
            assert result.status_code == 200, result.text
            return result.json()

        def post(suffix: str, **body: str) -> dict:
            result = http_client.post(resource + suffix, json=body, timeout=30)
            assert result.status_code == 200, result.text
            return result.json()

        for transport in ("runner", "host"):
            if transport == "host":
                pid = _runner_pid_from_daemon_log(daemon.daemon_log)
                assert pid is not None
                os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 20
                while http_client.get(f"/v1/runners/{runner}/status").json().get("online"):
                    assert time.monotonic() < deadline, "runner did not go offline"
                    time.sleep(0.2)
                _wait_for_host_online(http_client, daemon.host_id)
            info = get()
            assert info["available"] and info["gh_available"] and info["authenticated"], info
            assert info["pr"]["title"] == "GitHub compatibility proof"
            assert info["pr"]["checks"]["passing"] == 1
            assert info["pr"]["body"] == "PR description"
            assert info["pr"]["comments"][0]["body"] == "Looks good"
            assert info["repo"]["name_with_owner"] == "example/compat"
            assert get("/changes")["data"][0]["path"] == "file.txt"
            assert get("/diff")["patch"] == _PATCH
            content = get("/diff/file.txt", base="main", head_sha=head, base_sha=base)
            assert (content["before"], content["after"]) == ("before\n", "after\n")
            post("/preferences", account="octocat", remote="origin")
            linked = _URL.removesuffix("42") + "43"
            post("/prs", url=linked, action="attach")
            selected = get(pr_url=linked)
            assert selected["pr"]["number"] == 43
            assert {pr["number"] for pr in selected["prs"]} == {42, 43}
            assert get("/diff", pr_url=linked)["patch"] == _PATCH
            post("/preferences", account="octocat", pr_url=linked)
            post("/prs", url=linked, action="remove")
            assert {pr["number"] for pr in get()["prs"]} == {42}
            (tmp_path / f"{transport}-proof.json").write_text(json.dumps(info, indent=2))
            post("/prs", url=_URL, action="remove")
            forge = json.loads((binary / "forge.json").read_text())
            for mode in ("no_pr", "signed_out"):
                forge["mode"] = mode
                (binary / "forge.json").write_text(json.dumps(forge))
                empty = get()
                assert empty["pr"] is None and empty["prs"] == [], empty
                assert empty["authenticated"] == (mode == "no_pr"), empty
                assert empty["available"] and empty["gh_available"], empty
            forge["mode"] = "ready"
            (binary / "forge.json").write_text(json.dumps(forge))
            post("/prs", url=_URL, action="attach")
    finally:
        daemon.proc.terminate()
        try:
            daemon.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.proc.kill()
            daemon.proc.wait(timeout=5)
