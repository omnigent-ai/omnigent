"""Real HTTP/SQL preference edits across two server processes and a restart.

Own environments. The saved-session journey uses a scripted model; other journeys
make no provider calls. No browser, live vendor, or native harness is exercised.
The CLI config explicitly enables the API on these isolated local servers.
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest

from tests._helpers.live_server import isolated_local_server, isolated_process_env


def test_shared_preferences_across_replicas_and_restart(tmp_path, db_uri):
    config = tmp_path / "server.yaml"
    config.write_text("shared_preferences_authority_id: live-preference-test\n")
    arguments = ("--config", str(config), "--database-uri", db_uri)
    server_env = {"OMNIGENT_LOCAL_SINGLE_USER": "0"}
    alice = {"X-Forwarded-Email": "alice@example.test"}
    path = "/v1/preferences/shared"
    with isolated_local_server(tmp_path / "first", extra_args=arguments, env=server_env) as first:
        with isolated_local_server(
            tmp_path / "second", extra_args=arguments, env=server_env
        ) as second:
            with httpx.Client(headers=alice, timeout=20, trust_env=False) as client:
                response = client.get(first + path)
                assert response.status_code == 200, response.text
                identity = response.json()["identity"]
                assert identity["authority_id"] == "live-preference-test"
                assert client.get(second + path).json()["identity"] == identity

                def operation(kind, name, value, version=0, resolves=()):
                    return {
                        "protocol_version": 1,
                        "identity": identity,
                        "operation_id": str(uuid4()),
                        "installation_id": "laptop",
                        "key": {"kind": kind, "name": name},
                        "expected_version": version,
                        "value": value,
                        "resolves": list(resolves),
                    }

                edits = [operation("model", "model", value) for value in ("model-a", "model-b")]

                def submit(pair):
                    url, edit = pair
                    result = client.post(url + path + "/operations", json=edit)
                    assert result.status_code == 200, result.text
                    return result.json()

                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(submit, zip((first, second), edits, strict=True)))
                assert sorted(item["status"] for item in results) == ["applied", "conflict"]
                snapshot = client.get(second + path).json()
                assert len(snapshot["conflicts"]) == 1
                conflict = snapshot["conflicts"][0]
                resolution = operation(
                    "model", "model", conflict["value"], 1, [conflict["operation_id"]]
                )
                resolved = submit((first, resolution))
                assert resolved["entry"]["version"] == 2
                assert submit((second, resolution)) == resolved
                # A second key forces pagination across replicas and the later restart.
                extra = operation("model", "opencode_model", "opencode-default")
                assert submit((second, extra))["status"] == "applied"
                saved = client.get(first + path).json()
                assert saved == client.get(second + path).json()
                assert saved["conflicts"] == []
                first_page = client.get(first + path, params={"limit": 1}).json()
                pages = [first_page]
                while pages[-1]["next_cursor"]:
                    reply = client.get(
                        second + path, params={"limit": 1, "cursor": pages[-1]["next_cursor"]}
                    )
                    assert reply.status_code == 200, reply.text
                    pages.append(reply.json())
                assert len(pages) == 2
                assert all(page["revision"] == saved["revision"] for page in pages)
                assert [entry for page in pages for entry in page["entries"]] == saved["entries"]
    with isolated_local_server(
        tmp_path / "restart", extra_args=arguments, env=server_env
    ) as restarted:
        with httpx.Client(
            base_url=restarted, headers=alice, timeout=20, trust_env=False
        ) as client:
            assert client.get(path).json() == saved
            resumed = client.get(path, params={"limit": 1, "cursor": first_page["next_cursor"]})
            assert resumed.status_code == 200 and resumed.json() == pages[1]
            assert client.post(path + "/operations", json=resolution).json() == resolved
            removal = operation("model", "model", None, 2)
            assert client.post(path + "/operations", json=removal).json()["entry"]["version"] == 3
            # Replaying an old operation returns its receipt without resurrecting the value.
            assert client.post(path + "/operations", json=edits[0]).json() == results[0]
            assert (
                client.get(path, params={"cursor": first_page["next_cursor"]}).status_code == 409
            )
            current = client.get(path).json()
            model = next(
                item
                for item in current["entries"]
                if item["key"] == {"kind": "model", "name": "model"}
            )
            assert model["value"] is None and model["version"] == 3


_CLIENT_PROCESS = """
import asyncio, json, sys
from pathlib import Path
from uuid import UUID
import httpx
from omnigent.preference_client import PreferenceClient
from omnigent.preference_sync import PreferenceNamespace, SharedPreferenceKey

root, installation, url, action, payload = sys.argv[1:]
local = PreferenceClient(Path(root) / 'cache.db', installation_id=installation,
    identity=PreferenceNamespace(authority_id='live-preference-client',
                                 user_id='alice@example.test', workspace_id='0'))
error = None
async def drive():
    async def lose_reply(response):
        if response.request.method == 'POST' and response.status_code == 200:
            raise httpx.ReadError('injected lost acknowledgment', request=response.request)
    async with httpx.AsyncClient(base_url=url, trust_env=False, timeout=10,
        headers={'X-Forwarded-Email': 'alice@example.test'},
        event_hooks={'response': [lose_reply]} if action == 'lose-ack' else {}) as http:
        await local.sync(http, page_size=2)
try:
    if action == 'enqueue':
        for edit in json.loads(payload):
            local.enqueue(SharedPreferenceKey.model_validate(edit['key']), edit['value'],
                resolves=tuple(UUID(item) for item in edit.get('resolves', [])))
    elif action == 'discard':
        local.discard(UUID(payload))
    else:
        asyncio.run(drive())
except httpx.HTTPError as exc:
    error = type(exc).__name__
snapshot = local.cached()
print(json.dumps({'snapshot': snapshot.model_dump(mode='json') if snapshot else None,
                  'pending': [item.model_dump(mode='json') for item in local.pending()],
                  'error': error}))
sys.exit(2 if error else 0)
"""


def test_durable_preference_clients_offline_reconnect_and_restart(tmp_path, db_uri):
    config = tmp_path / "server.yaml"
    config.write_text("shared_preferences_authority_id: live-preference-client\n")
    arguments = ("--config", str(config), "--database-uri", db_uri)
    server_env = {"OMNIGENT_LOCAL_SINGLE_USER": "0"}
    model_key = {"kind": "model", "name": "model"}

    def drive(installation, url, action="sync", edits=(), *, failure=False):
        root = tmp_path / installation
        root.mkdir(exist_ok=True)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                _CLIENT_PROCESS,
                str(root),
                installation,
                url,
                action,
                edits if action == "discard" else json.dumps(edits),
            ],
            env=isolated_process_env(root / "home", {}, None),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == (2 if failure else 0), result.stdout + result.stderr
        return json.loads(result.stdout)

    def edit(key, value, **extra):
        return {"key": key, "value": value, **extra}

    with isolated_local_server(tmp_path / "first", extra_args=arguments, env=server_env) as first:
        with isolated_local_server(
            tmp_path / "replica", extra_args=arguments, env=server_env
        ) as replica:
            drive("laptop", first)
            drive("remote", replica)
            edits = [
                edit(model_key, "model-a"),
                edit(model_key, "model-b"),
                edit({"kind": "model", "name": "opencode_model"}, "opencode-default"),
                edit({"kind": "harness", "name": "default"}, "pi-native"),
                edit({"kind": "harness_args", "name": "claude"}, []),
                edit(
                    {
                        "kind": "plugin",
                        "name": {"ecosystem": "claude", "source": "catalog", "name": "tools"},
                    },
                    False,
                ),
            ]
            queued = drive("laptop", "http://127.0.0.1:1", "enqueue", edits)
            assert len(queued["pending"]) == len(edits)
            saved = drive("laptop", first)["snapshot"]
            assert len(saved["entries"]) == 5 and saved["conflicts"] == []
            drive("remote", replica, "enqueue", [edit(model_key, "remote-choice")])
            conflict = drive("remote", replica)["snapshot"]["conflicts"][0]
            drive("laptop", first)
            blocked = drive(
                "laptop",
                first,
                "enqueue",
                [
                    edit(model_key, "laptop-choice", resolves=[conflict["operation_id"]]),
                    edit({"kind": "harness", "name": "default"}, "codex-native"),
                ],
            )["pending"][0]
            drive(
                "remote",
                replica,
                "enqueue",
                [edit(model_key, "remote-choice", resolves=[conflict["operation_id"]])],
            )
            drive("remote", replica)
            rejected = drive("laptop", first, failure=True)
            assert rejected["error"] == "HTTPStatusError"
            assert len(rejected["pending"]) == 2
            drive("laptop", first, "discard", blocked["operation_id"])
            recovered = drive("laptop", first)
            assert recovered["pending"] == [] and recovered["snapshot"]["conflicts"] == []
            assert any(
                entry["key"] == {"kind": "harness", "name": "default"}
                and entry["value"] == "codex-native"
                for entry in recovered["snapshot"]["entries"]
            )
            assert (
                next(
                    entry
                    for entry in recovered["snapshot"]["entries"]
                    if entry["key"] == model_key
                )["value"]
                == "remote-choice"
            )
            drive("laptop", first)
            drive("laptop", first, "enqueue", [edit(model_key, "lost-ack-choice")])
            uncertain = drive("laptop", first, "lose-ack", failure=True)
            assert len(uncertain["pending"]) == 1
            drive("remote", replica)
            drive("remote", replica, "enqueue", [edit(model_key, "newer-choice")])
            newer = drive("remote", replica)["snapshot"]
    # Both the client process and server processes have stopped; disk state remains.
    offline = drive("laptop", "http://127.0.0.1:1", failure=True)
    assert offline["pending"] == uncertain["pending"]
    with isolated_local_server(
        tmp_path / "restart", extra_args=arguments, env=server_env
    ) as restarted:
        retried = drive("laptop", restarted)
        assert retried["snapshot"] == newer and retried["pending"] == []
        drive("laptop", restarted, "enqueue", [edit(model_key, None)])
        cleared = drive("laptop", restarted)["snapshot"]
        assert (
            next(entry for entry in cleared["entries"] if entry["key"] == model_key)["value"]
            is None
        )
        assert drive("remote", restarted)["snapshot"] == cleared


@pytest.mark.parametrize("surface", ["cli", "host", "managed-host"])
def test_preference_cli_import_machine_override_and_restore(tmp_path, db_uri, surface):
    config = tmp_path / "cli-server.yaml"
    config.write_text("shared_preferences_authority_id: cli-preference-test\n")
    arguments = ("--config", str(config), "--database-uri", db_uri)
    home = tmp_path / "cli-home"
    home.mkdir()
    native = home / "claude"
    native.mkdir()
    (native / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"review@local-market": False}})
    )
    environment = isolated_process_env(
        home, {"CLAUDE_CONFIG_DIR": str(native)}, base_env={"PATH": os.environ["PATH"]}
    )
    source = tmp_path / "source.yaml"
    source.write_text(
        "model: imported-model\nharness:\n  default: pi-native\n"
        "  pi-native:\n    args: []\n    command: private-command\n"
    )
    from contextlib import ExitStack

    import yaml

    from tests._helpers.host import isolated_host

    with ExitStack() as stack:
        dropped_reply = tmp_path / "drop-broker-reply"
        bootstrap = "from omnigent.cli import main\nmain()\n"
        if surface == "managed-host":
            bootstrap = """
import asyncio
import os
from pathlib import Path
from omnigent.host.frames import HostPreferenceReplyFrame, decode_host_frame
from omnigent.server.host_registry import HostRegistry
from omnigent.cli import main
original = HostRegistry.send_text
def send(self, conn, payload, **kwargs):
    frame = decode_host_frame(payload)
    marker = Path(os.environ["TEST_DROP_BROKER_REPLY"])
    if (isinstance(frame, HostPreferenceReplyFrame)
            and frame.reply.result is not None and marker.exists()):
        marker.unlink()
        asyncio.create_task(conn.ws.close(code=1012))
        return
    return original(self, conn, payload, **kwargs)
HostRegistry.send_text = send
main()
"""
        server = stack.enter_context(
            isolated_local_server(
                tmp_path / "cli-server",
                extra_args=arguments,
                bootstrap=bootstrap,
                env={
                    "OMNIGENT_LOCAL_SINGLE_USER": "0",
                    "TEST_DROP_BROKER_REPLY": str(dropped_reply),
                }
                if surface == "managed-host"
                else {},
            )
        )

        def cli(*args):
            result = subprocess.run(
                [sys.executable, "-m", "omnigent", "preferences", "--server", server, *args],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            return result.stdout

        if surface in ("host", "managed-host"):
            host_id = uuid4().hex
            host_env = {"CLAUDE_CONFIG_DIR": str(native)}
            headers = {}
            if surface == "managed-host":
                from omnigent.db.utils import now_epoch
                from omnigent.host.identity import HOST_TOKEN_ENV_VAR, MANAGED_HOST_TOKEN_HEADER
                from omnigent.stores.host_store import HostStore

                token = "isolated-preference-host-token"
                HostStore(db_uri).register_managed_host(
                    host_id=host_id,
                    name=f"e2e-host-{host_id[:12]}",
                    user_id="alice@example.test",
                    token=token,
                    provider="test",
                    sandbox_id="isolated-host",
                    token_expires_at=now_epoch() + 3600,
                )
                host_env[HOST_TOKEN_ENV_VAR] = token
                headers = {"X-Forwarded-Email": "alice@example.test"}
                assert (
                    httpx.get(
                        server + "/v1/preferences/shared",
                        headers={MANAGED_HOST_TOKEN_HEADER: token},
                    ).status_code
                    == 401
                )
            host = stack.enter_context(
                isolated_host(
                    tmp_path / "host",
                    server,
                    config=yaml.safe_load(source.read_text()),
                    env=host_env,
                    host_id=host_id,
                    base_env={"PATH": os.environ["PATH"]},
                )
            )
            client = stack.enter_context(
                httpx.Client(base_url=server, timeout=20, trust_env=False, headers=headers)
            )
            host.wait_online(client)

            drop_once = surface == "managed-host"

            def action(payload):
                nonlocal drop_once
                drop = drop_once and payload["action"] == "apply_import"
                if drop:
                    dropped_reply.touch()
                    drop_once = False
                response = client.post(f"/v1/hosts/{host.host_id}/preferences", json=payload)
                if drop:
                    from tests._helpers.polling import poll_until

                    assert response.status_code == 503, response.text
                    assert not dropped_reply.exists()
                    # Only observe: reconnect must replay the receipt and finish the import.
                    state = {}

                    def delivered():
                        nonlocal state
                        snapshot = client.get("/v1/preferences/shared").json()
                        if len(snapshot.get("entries", [])) != 4:
                            return False
                        current = client.post(
                            f"/v1/hosts/{host.host_id}/preferences", json={"action": "status"}
                        )
                        state = current.json()
                        return current.status_code == 200 and state.get("pending") == []

                    poll_until(delivered, timeout_s=45, interval_s=0.2)
                    assert state["shared"]["revision"] == 4
                    return state
                assert response.status_code == 200, (
                    response.text + host.log_path.read_text()[-2000:]
                )
                return response.json()

            assert action({"action": "status"})["connected"] is False
            initial = action({"action": "preview_import", "plugins": ["claude"]})
            assert initial["preview"]["changes"] and initial["connected"] is False
            assert action({"action": "status"})["connected"] is False
            assert "shared_preferences" not in (host.home / ".omnigent/config.yaml").read_text()

            def cli(*args):
                from omnigent.preference_commands import _key

                command = args[0]
                if command == "import":
                    return json.dumps(
                        action({"action": "preview_import", "plugins": ["claude"]})["preview"]
                    )
                if command == "restore":
                    return json.dumps(action({"action": "preview_restore"})["preview"])
                if command == "apply":
                    from pathlib import Path

                    payload = {
                        "action": "apply_import",
                        "preview": json.loads(Path(args[1]).read_text()),
                    }
                elif command == "set":
                    assert args[1:3] == ("--scope", "machine")
                    payload = {
                        "action": "set_machine",
                        "key": _key(args[3]).model_dump(mode="json"),
                        "value": json.loads(args[4]),
                    }
                else:
                    payload = {"action": "status" if command == "show" else command}
                return json.dumps(action(payload))

        cli("connect")
        before = json.loads(cli("show"))
        assert before["shared"]["entries"] == [] and before["pending"] == []
        plan = tmp_path / "plan.json"
        plan.write_text(cli("import", str(source), "--plugins", "claude"))
        assert "private-command" not in plan.read_text()
        changes = json.loads(plan.read_text())["changes"]
        plugin = next(
            change["operation"]
            for change in changes
            if change["operation"]["key"]["kind"] == "plugin"
        )
        assert plugin["value"] is False and plugin["key"]["name"]["source"] == "local-market"
        cli("apply", str(plan))
        cli("set", "--scope", "machine", "model", '"experiment"')
        shown = json.loads(cli("show"))
        assert shown["machine"][0]["value"] == "experiment"
        assert "imported-model" in json.dumps(shown["shared"])
        if surface == "cli":
            import tempfile

            from omnigent.preference_wrapper import PREFERENCE_HANDOFF_FD_ENV

            prepared = cli("prepare-launch")
            assert json.loads(prepared)["harness"] == "pi-native"
            assert "private-command" not in prepared
            with tempfile.TemporaryFile() as handoff:
                handoff.write(prepared.encode())
                handoff.seek(0)
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        """
import json, sys
import click
from omnigent.config import load_global_config
from omnigent import preference_cli_startup as startup
from omnigent.preference_session import session_preference_snapshot
async def unexpected_refresh(_server):
    raise AssertionError("the wrapper already refreshed")
startup.prepare_server_preferences = unexpected_refresh
config = load_global_config()
config["server"] = sys.argv[1]
with click.Context(click.Command("run"), info_name="run"):
    resolved = startup.resolve_cli_preferences(config, {})
    output = {"model": resolved.config["model"], "snapshot": session_preference_snapshot()}
    print(json.dumps(output))
""",
                        server,
                    ],
                    env={**environment, PREFERENCE_HANDOFF_FD_ENV: str(handoff.fileno())},
                    pass_fds=(handoff.fileno(),),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            assert result.returncode == 0, result.stdout + result.stderr
            adopted = json.loads(result.stdout)
            assert adopted["model"] == "experiment"
            assert adopted["snapshot"] == json.loads(prepared)["launch"]
        cli("set", "--scope", "machine", "model", "null")
        assert json.loads(cli("show"))["machine"] == []
        restore = tmp_path / "restore.json"
        restore.write_text(cli("restore"))
        cli("apply", str(restore))
        assert all(
            entry["value"] is None for entry in json.loads(cli("show"))["shared"]["entries"]
        )


@pytest.mark.parametrize("model_source", ["agent", "preferences"])
def test_saved_session_preferences_resume_on_fresh_host(
    tmp_path, db_uri, mock_llm_server_url, model_source
):
    """Real server → two independent hosts → runner init; deterministic provider only."""
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from tests._helpers.host import isolated_host
    from tests._helpers.polling import poll_until
    from tests._helpers.session import bundle_files, post_session_bundle
    from tests.e2e.conftest import configure_mock_llm

    config = tmp_path / "server.yaml"
    config.write_text("shared_preferences_authority_id: resume-test\n")
    model = "mock-saved-session-model"
    bundle = bundle_files(
        {
            "config.yaml": (
                "spec_version: 1\nname: saved-preference-test\nprompt: Say hello.\n"
                "executor:\n  type: omnigent\n  config:\n    harness: openai-agents\n"
                + (f"  model: {model}\n" if model_source == "agent" else "")
            ).encode()
        }
    )
    with isolated_local_server(
        tmp_path / "server", extra_args=("--config", str(config), "--database-uri", db_uri)
    ) as server:
        with httpx.Client(base_url=server, timeout=40, trust_env=False) as client:
            identity = client.get("/v1/preferences/shared").json()["identity"]
            operation = {
                "protocol_version": 1,
                "identity": identity,
                "operation_id": str(uuid4()),
                "installation_id": "original-machine",
                "key": {"kind": "model", "name": "model"},
                "expected_version": 0,
                "value": model,
            }
            client.post("/v1/preferences/shared/operations", json=operation).raise_for_status()
            host_env = {
                "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
                "OPENAI_API_KEY": "mock-key",
                "OMNIGENT_SHARED_PREFERENCES": "1",
            }
            session_id = ""
            for name in ("original", "replacement"):
                reply = f"{name} runner used the saved session"
                configure_mock_llm(mock_llm_server_url, [{"text": reply}], key=model)
                with isolated_host(
                    tmp_path / name,
                    server,
                    base_env={"PATH": os.environ["PATH"]},
                    env=host_env,
                    config={"auth": {"type": "api_key"}, "model": "different-local-default"},
                ) as host:
                    host.wait_online(client)
                    if name == "replacement":
                        client.patch(
                            f"/v1/sessions/{session_id}", json={"runner_id": ""}
                        ).raise_for_status()
                    workspace = host.home / "workspace"
                    workspace.mkdir()
                    if name == "original":
                        prepared = client.post(
                            f"/v1/hosts/{host.host_id}/preferences",
                            json={"action": "prepare", "workspace": str(workspace)},
                        )
                        assert prepared.status_code == 200, prepared.text
                        snapshot = prepared.json()["launch"]
                        assert prepared.json()["defaults"]["model"] == model
                        created = post_session_bundle(
                            client.post,
                            "/v1/sessions",
                            bundle,
                            metadata={"preference_snapshot": snapshot},
                        )
                        assert created.status_code == 201, created.text
                        session_id = created.json()["session_id"]
                        client.post(
                            "/v1/preferences/shared/operations",
                            json={
                                **operation,
                                "operation_id": str(uuid4()),
                                "expected_version": 1,
                                "value": "later-default",
                            },
                        ).raise_for_status()
                    response = client.post(
                        f"/v1/hosts/{host.host_id}/runners",
                        json={"session_id": session_id, "workspace": str(workspace)},
                    )
                    assert response.status_code == 200, (
                        response.text + host.log_path.read_text()[-4000:]
                    )
                    runner_id = response.json()["runner_id"]
                    poll_until(
                        lambda runner=runner_id: (
                            client.get(f"/v1/runners/{runner}/status").json().get("online")
                        ),
                        timeout_s=30,
                        interval_s=0.2,
                    )
                    # Binding initializes the real runner with the saved envelope.
                    client.patch(
                        f"/v1/sessions/{session_id}", json={"runner_id": runner_id}
                    ).raise_for_status()
                    client.post(
                        f"/v1/sessions/{session_id}/events",
                        json={
                            "type": "message",
                            "data": {
                                "role": "user",
                                "content": [{"type": "input_text", "text": f"hello from {name}"}],
                            },
                        },
                    ).raise_for_status()
                    last = {}

                    def answered(expected=reply, saved_session=session_id):
                        nonlocal last
                        last = client.get(f"/v1/sessions/{saved_session}").json()
                        return last.get("status") == "failed" or expected in json.dumps(
                            last.get("items", [])
                        )

                    poll_until(answered, timeout_s=40, interval_s=0.2)
                    assert reply in json.dumps(last.get("items", [])), (
                        json.dumps(last) + host.log_path.read_text()[-4000:]
                    )
                    saved = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
                    assert saved.preference_snapshot == snapshot
                    handoffs = list(
                        host.home.rglob("preferences/runtime/launch-*/preferences.json")
                    )
                    assert len(handoffs) == 1
                    assert json.loads(handoffs[0].read_text()) == snapshot
                    from omnigent.native.owner_claim import read_owner_claim

                    assert read_owner_claim(handoffs[0].parent).pid != host.proc.pid
                    assert (
                        "shared_preferences:"
                        not in (host.home / ".omnigent/config.yaml").read_text()
                    )
                    client.post(
                        f"/v1/sessions/{session_id}/events", json={"type": "stop_session"}
                    ).raise_for_status()
                    poll_until(
                        lambda path=handoffs[0]: not path.exists(), timeout_s=15, interval_s=0.2
                    )
