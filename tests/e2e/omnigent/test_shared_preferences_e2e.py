"""Real HTTP/SQL preference edits across two server processes and a restart.

Own environment; no provider calls, mock services, browser, or native harness.
The CLI config explicitly enables the API on these isolated local servers.
"""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import yaml

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


def test_configuration_save_import_uses_real_cli_and_preserves_local_settings(tmp_path, db_uri):
    config = tmp_path / "server.yaml"
    config.write_text("shared_preferences_authority_id: configuration-import-test\n")
    arguments = ("--config", str(config), "--database-uri", db_uri)
    homes = [tmp_path / name for name in ("source", "target")]
    for home in homes:
        (home / ".omnigent").mkdir(parents=True)
    source, target = homes
    source_config = source / ".omnigent/config.yaml"
    target_config = target / ".omnigent/config.yaml"
    source_config.write_text(
        yaml.safe_dump(
            {
                "model": "model-from-source",
                "opencode_model": "provider/model",
                "harness": {
                    "default": "pi-native",
                    "codex-native": {
                        "args": ["--config", "a", "--config", "b"],
                        "command": "/source/codex",
                    },
                },
                "providers": {"private": "source-credential"},
            }
        )
    )
    target_config.write_text(
        yaml.safe_dump(
            {
                "model": "target-old",
                "harness": {"codex-native": {"command": "/target/codex"}},
                "providers": {"private": "target-credential"},
                "host": {"id": "target-host"},
            }
        )
    )
    (source / "plugin-optout.json").write_text(
        json.dumps({"opted_out": ["lite", "liteswap"], "disable_all": True})
    )
    (target / "plugin-optout.json").write_text(
        json.dumps({"opted_out": ["previous"], "disable_all": False})
    )

    def run(home, server, *command, stdin=""):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "omnigent",
                "config",
                "cloud",
                "--server",
                server,
                "--plugin-opt-outs",
                str(home / "plugin-optout.json"),
                *command,
            ],
            env=isolated_process_env(
                home,
                {
                    "OMNIGENT_CONFIG_HOME": str(home / ".omnigent"),
                    "OMNIGENT_SKIP_ONBOARD": "1",
                    "OMNIGENT_NO_UPDATE_CHECK": "1",
                },
                None,
            ),
            input=stdin,
            text=True,
            capture_output=True,
            timeout=40,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    with isolated_local_server(tmp_path / "first", extra_args=arguments) as server:
        run(source, server, "save", stdin="y\n")
        cloud = json.loads(run(target, server, "show"))
        assert cloud["configuration"]["model"] == "model-from-source"
        assert "source-credential" not in json.dumps(cloud)
        assert "/source/codex" not in json.dumps(cloud)
        assert yaml.safe_load(target_config.read_text())["model"] == "target-old"
        run(target, server, "import", stdin="y\n")
        imported = yaml.safe_load(target_config.read_text())
        assert imported["providers"] == {"private": "target-credential"}
        assert imported["host"] == {"id": "target-host"}
        assert imported["harness"]["codex-native"] == {
            "command": "/target/codex",
            "args": ["--config", "a", "--config", "b"],
        }
        assert json.loads((target / "plugin-optout.json").read_text()) == {
            "opted_out": ["lite", "liteswap"],
            "disable_all": True,
        }
        assert list((target / ".omnigent").glob("config.yaml.before-import-*"))
        source_config.write_text("model: second-model\n")
        run(source, server, "save", "--yes")
        assert yaml.safe_load(target_config.read_text())["model"] == "model-from-source"
    with isolated_local_server(tmp_path / "restart", extra_args=arguments) as server:
        assert json.loads(run(target, server, "show"))["configuration"]["model"] == "second-model"
        run(target, server, "import", "--yes")
        refreshed = yaml.safe_load(target_config.read_text())
        assert refreshed["model"] == "second-model" and "opencode_model" not in refreshed
        assert refreshed["harness"] == {"codex-native": {"command": "/target/codex"}}
        assert refreshed["providers"] == imported["providers"]
