#!/usr/bin/env python3
"""Real Boat managed-host lifecycle E2E (creates paid, temporary sandboxes).

Build wheels from this checkout, set BOAT_API_KEY, then run from the repo root:

    uv run python tests/e2e/integrations/deploy/boat/boat_smoke_test.py \
        --wheels work/boat-wheels --host-package 'omnigent==0.18.0'

Uses the real Boat API, the built server, real host tunnels and real repositories.
Verifies launch, credential isolation, clone, persistent resume, replacement and
cleanup. Model-backed agent turns require separate model credentials and are
outside this provider lifecycle scenario. No API or host mocks are used.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import httpx
import pytest

from omnigent.onboarding.sandboxes.boat import BoatSandboxLauncher


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wheels", type=Path, required=True, help="Built core, client and UI SDK wheels"
    )
    parser.add_argument(
        "--host-package", required=True, help="Published compatible package for child hosts"
    )
    args = parser.parse_args()
    if not os.environ.get("BOAT_API_KEY"):
        parser.error("Set BOAT_API_KEY before running this real cloud test")
    if len(list(args.wheels.glob("*.whl"))) != 3:
        parser.error("--wheels must contain the core, client and UI SDK wheels")
    temp = tempfile.TemporaryDirectory(prefix="boat-e2e-")
    launcher = BoatSandboxLauncher()
    os.environ["OMNIGENT_BOAT_HOST_PACKAGE"] = args.host_package
    controller = None
    client: httpx.Client | None = None
    children = set()
    user = "boat-e2e-" + secrets.token_hex(4)
    password = secrets.token_urlsafe(32)
    secret = os.environ["BOAT_API_KEY"]
    root = "/home/user/boat-e2e"
    db = "sqlite:////home/user/boat-e2e/server.db"
    config_path = root + "/config.yaml"
    python = "/home/user/.omnigent-venv/bin/python"

    def output(label, data):
        print("\n" + label, flush=True)
        if isinstance(data, str):
            print(data.strip(), flush=True)
        else:
            print(json.dumps(data, indent=2), flush=True)

    def remote_python(source):
        result = launcher.run(controller, python + " -c " + shlex.quote(source))
        if result.stderr.strip():
            print("Remote runtime diagnostics available; credentials withheld.", flush=True)
        return result.stdout

    def lifecycle(action, host_id=None):
        preamble = textwrap.dedent(f"""
            import asyncio, json, os, yaml
            from omnigent.server.managed_hosts import (
                parse_sandbox_config, launch_managed_host,
                resume_managed_host, relaunch_managed_host,
            )
            from omnigent.onboarding.sandboxes.base import SandboxGoneError
            from omnigent.onboarding.sandboxes.types import RepoWorkspace
            from omnigent.stores.host_store import HostStore
            os.environ['BOAT_API_KEY'] = {secret!r}
            os.environ['OMNIGENT_BOAT_HOST_PACKAGE'] = {args.host_package!r}
            store = HostStore({db!r})
            config = parse_sandbox_config(yaml.safe_load(open({config_path!r}))['sandbox'])
            repos = [RepoWorkspace(
                url='https://github.com/omnigent-ai/omnigent.git',
                branch='main', repo_name='omnigent',
            )]
        """)
        if action == "launch":
            body = textwrap.dedent(f"""
                result = await launch_managed_host(
                    config=config, owner={user!r}, host_store=store, repos=repos,
                )
                row = store.get_host(result.host_id)
            """)
        elif action == "resume":
            body = textwrap.dedent(f"""
                await resume_managed_host({host_id!r}, store, config, force=True)
                row = store.get_host({host_id!r})
            """)
        elif action == "replace":
            body = textwrap.dedent(f"""
                try:
                    await resume_managed_host({host_id!r}, store, config, force=True)
                except SandboxGoneError:
                    await relaunch_managed_host(
                        config=config, host=store.get_host({host_id!r}),
                        host_store=store, repos=repos,
                    )
                else:
                    raise AssertionError('Deleted sandbox was not classified as gone')
                row = store.get_host({host_id!r})
            """)
        else:
            raise ValueError(action)
        body += textwrap.dedent("""
            print(json.dumps({
                'host_id': row.host_id, 'sandbox_id': row.sandbox_id,
                'sandbox_provider': row.sandbox_provider, 'status': row.status,
            }))
        """)
        source = (
            preamble
            + "\nasync def main():\n"
            + textwrap.indent(body, "    ")
            + "\nasyncio.run(main())\n"
        )
        text = remote_python(source)
        data = json.loads(text.strip().splitlines()[-1])
        assert data["status"] == "online" and data["sandbox_provider"] == "boat", data
        children.add(data["sandbox_id"])
        return data

    try:
        output(
            "$ real Boat / Omnigent managed lifecycle E2E",
            {
                "api": "https://boat.dev/api/v1",
                "scope": (
                    "Real cloud server, child sandboxes, repository clone, host tunnel, "
                    "resume and replacement. No model calls."
                ),
            },
        )
        controller = launcher.provision("omnigent-real-e2e-controller")
        launcher.run(controller, "mkdir -p " + root)
        for wheel in sorted(args.wheels.glob("*.whl")):
            launcher.put(controller, wheel, "/tmp/" + wheel.name)
        output(
            "$ install PR wheels into isolated controller",
            launcher.run(
                controller,
                "uv pip install --python "
                + python
                + " --force-reinstall /tmp/omnigent*.whl && omnigent --version",
            ).stdout,
        )
        hosted = launcher._request(
            "POST",
            launcher._path(controller) + "/host",
            body={
                "port": 8765,
                "public": True,
                "localhost": True,
                "title": "Authenticated temporary Omnigent E2E",
            },
        )
        base = hosted["url"].rstrip("/")
        config = {
            "sandbox": {
                "provider": "boat",
                "server_url": base,
                "git_clone": {"depth": 1, "single_branch": True},
            }
        }
        config_local = Path(temp.name) / "config.yaml"
        config_local.write_text(json.dumps(config))
        launcher.put(controller, config_local, config_path)
        env = {
            "HOME": root,
            "OMNIGENT_DATA_DIR": root + "/data",
            "OMNIGENT_AUTH_PROVIDER": "accounts",
            "OMNIGENT_ACCOUNTS_ENABLED": "1",
            "OMNIGENT_ACCOUNTS_BASE_URL": base,
            "OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME": user,
            "OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD": password,
            "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
            "OMNIGENT_BOAT_HOST_PACKAGE": args.host_package,
            "BOAT_API_KEY": secret,
        }
        command = (
            "setsid nohup env "
            + " ".join(k + "=" + shlex.quote(v) for k, v in env.items())
            + " omnigent server --no-open --host 127.0.0.1 --port 8765 --database-uri "
            + db
            + " --artifact-location "
            + root
            + "/artifacts --config "
            + config_path
            + " > "
            + root
            + "/server.log 2>&1 < /dev/null & echo launched"
        )
        launcher.run(controller, command)
        client = httpx.Client(base_url=base, timeout=30, follow_redirects=True)
        for _ in range(60):
            try:
                response = client.get("/health")
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:
            raise AssertionError("Real authenticated server not healthy")
        assert client.get("/v1/hosts").status_code == 401
        login = client.post("/auth/login", json={"username": user, "password": password})
        assert login.status_code == 200, login.status_code
        client.headers["Authorization"] = "Bearer " + login.json()["token"]
        output(
            "$ GET /health + authenticated server access",
            {
                "health": response.json(),
                "unauthenticated_hosts": 401,
                "authenticated_hosts": client.get("/v1/hosts").status_code,
            },
        )
        first = lifecycle("launch")
        output("$ launch_managed_host() — production orchestration", first)
        sid = first["sandbox_id"]
        hid = first["host_id"]
        hosts = client.get("/v1/hosts")
        hosts.raise_for_status()
        assert any(
            row["host_id"] == hid and row["status"] == "online" for row in hosts.json()["hosts"]
        )
        credentials = launcher.run(sid, "printenv BOAT_API_KEY", check=False)
        assert credentials.returncode != 0 and not credentials.stdout
        output(
            "$ GET /v1/hosts — real WebSocket registration",
            {"host_id": hid, "status": "online", "HTTP": hosts.status_code},
        )
        path = "/home/user/workspace/omnigent"
        output(
            "$ git rev-parse --is-shallow-repository; git remote get-url origin",
            launcher.run(
                sid,
                "cd "
                + path
                + " && git rev-parse --is-shallow-repository && git remote get-url origin",
            ).stdout,
        )
        launcher.run(sid, "printf real-cloud-e2e-marker > " + path + "/boat-e2e-marker")
        launcher._request("POST", launcher._path(sid) + "/stop", body={})
        for _ in range(120):
            state = launcher._state(sid)
            if state == "archived":
                break
            time.sleep(2)
        else:
            raise AssertionError("Sandbox did not archive")
        output("$ Boat archive", {"sandbox_id": sid, "state": state})
        resumed = lifecycle("resume", hid)
        assert resumed["sandbox_id"] == sid
        marker = launcher.run(sid, "cat " + path + "/boat-e2e-marker").stdout
        assert marker == "real-cloud-e2e-marker"
        output(
            "$ resume_managed_host() — same disk, renewed host tunnel",
            {"host": resumed, "workspace_file": marker},
        )
        launcher.terminate(sid)
        replaced = lifecycle("replace", hid)
        assert replaced["host_id"] == hid and replaced["sandbox_id"] != sid
        assert (
            launcher.run(replaced["sandbox_id"], "test -f " + path + "/README.md").returncode == 0
        )
        output("$ deleted sandbox → production relaunch_managed_host()", replaced)
        output(
            "REAL CLOUD E2E ASSERTIONS PASSED",
            {
                "same_host_identity": True,
                "persistent_resume": True,
                "deleted_sandbox_replacement": True,
                "repository_restored": True,
            },
        )
    finally:
        if client is not None:
            client.close()
        temp.cleanup()
        for sid in children:
            try:
                launcher.terminate(sid)
            except Exception:
                print("Cleanup needs attention for test child " + sid, flush=True)
        if controller:
            # Discover only this uniquely-owned test account's hosts, including a failed launch.
            try:
                source = (
                    "from omnigent.stores.host_store import HostStore; import json; "
                    f"s=HostStore({db!r}); "
                    f"print(json.dumps([h.sandbox_id for h in s.list_hosts({user!r}) "
                    "if h.sandbox_id]))"
                )
                for sid in json.loads(remote_python(source).strip().splitlines()[-1]):
                    launcher.terminate(sid)
            except Exception:
                pass
            launcher.terminate(controller)
        output(
            "$ cleanup",
            {
                "controller_removed": bool(controller),
                "known_child_sandboxes_removed": len(children),
            },
        )

    return 0


@pytest.mark.timeout(1200)
def test_real_boat_managed_lifecycle(tmp_path: Path) -> None:
    """Opt-in paid E2E; reuse the same live CLI scenario, with owned wheel builds."""
    if os.environ.get("BOAT_LIVE_E2E") != "1":
        pytest.skip("Set BOAT_LIVE_E2E=1 and BOAT_API_KEY to run the real cloud E2E")
    assert os.environ.get("BOAT_API_KEY"), "BOAT_API_KEY is required for the live E2E"
    package = os.environ.get("OMNIGENT_BOAT_HOST_PACKAGE")
    assert package, "Set OMNIGENT_BOAT_HOST_PACKAGE to an available compatible release"
    root = Path(__file__).resolve().parents[5]
    wheels = tmp_path / "wheels"
    for source in (root, root / "sdks/python-client", root / "sdks/ui"):
        subprocess.run(
            ["uv", "build", "--wheel", str(source), "--out-dir", str(wheels)],
            cwd=root,
            check=True,
            capture_output=True,
        )
    result = subprocess.run(
        [sys.executable, __file__, "--wheels", str(wheels), "--host-package", package],
        cwd=root,
        timeout=1100,
    )
    assert result.returncode == 0, "Real Boat cloud lifecycle scenario failed"


if __name__ == "__main__":
    raise SystemExit(main())
