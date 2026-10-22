"""Real HTTP/SQL preference edits across two server processes and a restart.

Own environment; no provider calls, mock services, browser, or native harness.
The CLI config explicitly enables the API on these isolated local servers.
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx

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


def test_preference_cli_import_machine_override_and_restore(tmp_path, db_uri):
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
    with isolated_local_server(tmp_path / "cli-server", extra_args=arguments) as server:

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
        cli("set", "--scope", "machine", "model", "null")
        assert json.loads(cli("show"))["machine"] == []
        restore = tmp_path / "restore.json"
        restore.write_text(cli("restore"))
        cli("apply", str(restore))
        assert all(
            entry["value"] is None for entry in json.loads(cli("show"))["shared"]["entries"]
        )
