"""Real HTTP/SQL preference edits across two server processes and a restart.

Own environment; no provider calls, mock services, browser, or native harness.
The CLI config explicitly enables the API on these isolated local servers.
"""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx

from tests._helpers.live_server import isolated_local_server


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
