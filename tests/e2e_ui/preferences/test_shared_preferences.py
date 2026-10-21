"""Settings adoption and scope controls through a real server, host tunnel and cache."""

import json
import os
import re
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from playwright.sync_api import expect

from tests._helpers.host import isolated_host
from tests._helpers.live_server import isolated_local_server


@pytest.mark.parametrize("width", [1280, 390])
def test_settings_import_scopes_conflicts_and_restore(
    page, built_spa, tmp_path, output_path, width
):
    page.set_viewport_size({"width": width, "height": 1000})
    config = tmp_path / "server.yaml"
    config.write_text("shared_preferences_authority_id: browser-preferences\n")
    native = tmp_path / "claude"
    native.mkdir()
    (native / "settings.json").write_text(json.dumps({"enabledPlugins": {"review@team": False}}))
    evidence = Path(output_path)
    evidence.mkdir(parents=True, exist_ok=True)
    with isolated_local_server(
        tmp_path / "server", extra_args=("--config", str(config))
    ) as server:
        with (
            httpx.Client(base_url=server, timeout=20, trust_env=False) as client,
            isolated_host(
                tmp_path / "host",
                server,
                name="Preference test machine",
                base_env={"PATH": os.environ["PATH"]},
                env={"CLAUDE_CONFIG_DIR": str(native)},
                config={
                    "model": "imported-model",
                    "harness": {
                        "default": "pi-native",
                        "pi-native": {"args": [], "command": "private-command"},
                    },
                },
            ) as host,
        ):
            host.wait_online(client)
            endpoint = f"/v1/hosts/{host.host_id}/preferences"
            page.goto(f"{server}/settings/preferences")
            expect(
                page.get_by_role("heading", name="Shared preferences", exact=True)
            ).to_be_visible(timeout=30_000)
            preview = page.get_by_role("button", name="Preview this machine", exact=True)
            expect(preview).to_be_enabled(timeout=30_000)
            preview.click()
            dialog = page.get_by_role("dialog", name="Review imported defaults")
            expect(dialog).to_be_visible()
            expect(dialog.get_by_text("imported-model", exact=True)).to_be_visible()
            expect(dialog.get_by_text("Disabled", exact=True)).to_be_visible()
            assert "private-command" not in dialog.inner_text()
            assert client.post(endpoint, json={"action": "status"}).json()["connected"] is False
            page.screenshot(path=str(evidence / f"preview-{width}.png"))
            dialog.get_by_role("button", name="Apply and follow").click()
            expect(dialog).not_to_be_visible()
            expect(
                page.get_by_role("heading", name="This machine follows the workspace")
            ).to_be_visible()
            saved = page.get_by_role("region", name="Saved preferences")
            expect(saved.get_by_text("imported-model", exact=True)).to_be_visible()
            expect(saved.get_by_text("No default arguments", exact=True)).to_be_visible()
            expect(saved.get_by_text("Disabled", exact=True)).to_be_visible()

            page.get_by_role("radio", name="This machine", exact=True).check()
            page.get_by_label("Model ID", exact=True).fill("experiment")
            page.get_by_role("button", name="Save on this machine").click()
            expect(saved.get_by_text("experiment", exact=True)).to_be_visible()
            state = client.post(endpoint, json={"action": "status"}).json()
            assert state["machine"][0]["value"] == "experiment"
            assert (
                next(
                    entry["value"]
                    for entry in state["shared"]["entries"]
                    if entry["key"] == {"kind": "model", "name": "model"}
                )
                == "imported-model"
            )
            row = saved.get_by_role("article", name="Model", exact=True)
            row.get_by_role("button", name="Follow workspace", exact=True).click()
            expect(saved.get_by_text("experiment", exact=True)).not_to_be_visible()
            assert client.post(endpoint, json={"action": "status"}).json()["machine"] == []

            page.get_by_label("Setting", exact=True).select_option("arguments")
            page.get_by_label("Harness for arguments", exact=True).fill("pi-native")
            for index, argument in enumerate(["--flag", "two\nlines", "--flag"], start=1):
                page.get_by_role("button", name="Add argument", exact=True).click()
                page.get_by_role("textbox", name=f"Argument {index}", exact=True).fill(argument)
            page.get_by_role("button", name="Save on this machine").click()
            args_row = saved.get_by_role("article", name="pi-native arguments", exact=True)
            expect(
                args_row.get_by_text('"--flag" "two\\nlines" "--flag"', exact=True)
            ).to_be_visible()
            args_row.get_by_role("button", name="Edit machine override", exact=True).click()
            expect(page.get_by_role("textbox", name="Argument 2", exact=True)).to_have_value(
                "two\nlines"
            )
            state = client.post(endpoint, json={"action": "status"}).json()
            assert state["machine"][0]["value"] == ["--flag", "two\nlines", "--flag"]
            args_row.get_by_role("button", name="Follow workspace", exact=True).click()
            expect(args_row.get_by_text("Follow workspace", exact=True).first).to_be_visible()
            page.get_by_label("Setting", exact=True).select_option("model")

            page.get_by_role("radio", name="Follow me", exact=True).check()
            page.get_by_label("Model ID", exact=True).fill(" ")
            page.get_by_role("button", name="Save shared default").click()
            expect(page.get_by_role("alert")).to_contain_text("rejected this value")
            expect(page.get_by_label("Model ID", exact=True)).to_be_enabled()
            expect(page.get_by_role("button", name="Retry save", exact=True)).not_to_be_visible()

            # Another machine writes after the browser read. The browser must keep its old base.
            snapshot = client.get("/v1/preferences/shared").json()
            model = next(
                entry
                for entry in snapshot["entries"]
                if entry["key"] == {"kind": "model", "name": "model"}
            )
            operation = {
                "protocol_version": 1,
                "identity": snapshot["identity"],
                "operation_id": str(uuid4()),
                "installation_id": "another-machine",
                "key": model["key"],
                "expected_version": model["version"],
                "value": "other-machine",
            }
            assert (
                client.post("/v1/preferences/shared/operations", json=operation).status_code == 200
            )
            page.get_by_role("radio", name="Follow me", exact=True).check()
            page.get_by_label("Model ID", exact=True).fill("my-choice")
            page.get_by_role("button", name="Save shared default").click()
            conflicts = page.get_by_role("region", name="Competing edits")
            expect(conflicts.get_by_text("Shared: other-machine", exact=True)).to_be_visible()
            expect(conflicts.get_by_text("Proposed: my-choice", exact=True)).to_be_visible()
            conflicts.scroll_into_view_if_needed()
            page.screenshot(path=str(evidence / f"conflict-{width}.png"))
            conflicts.get_by_role("button", name="Use proposed").click()
            expect(conflicts).not_to_be_visible()
            expect(saved.get_by_text("my-choice", exact=True)).to_be_visible()

            deliveries = []

            def lose_first_reply(route):
                deliveries.append(route.request.post_data_json)
                response = route.fetch()
                if len(deliveries) == 1:
                    assert response.status == 200
                    route.abort("failed")
                else:
                    route.fulfill(response=response)

            page.route("**/v1/preferences/shared/operations", lose_first_reply)
            page.get_by_label("Model ID", exact=True).fill("retry-choice")
            page.get_by_role("button", name="Save shared default").click()
            retry = page.get_by_role("button", name="Retry save", exact=True)
            expect(retry).to_be_visible()
            expect(retry).to_be_enabled()
            committed_revision = client.get("/v1/preferences/shared").json()["revision"]
            page.get_by_role("button", name="Refresh", exact=True).click()
            expect(saved.get_by_text("retry-choice", exact=True)).to_be_visible()
            retry.click()
            expect(retry).not_to_be_visible()
            assert deliveries[0] == deliveries[1]
            assert deliveries[0]["protocol_version"] == 2
            assert deliveries[0]["observed_revision"] < committed_revision
            assert client.get("/v1/preferences/shared").json()["revision"] == committed_revision
            page.unroute("**/v1/preferences/shared/operations", lose_first_reply)

            page.get_by_role("button", name="Review restore", exact=True).click()
            restore = page.get_by_role("dialog", name="Review restore")
            expect(restore).to_be_visible()
            restore.get_by_role("button", name="Restore these values").click()
            expect(restore).not_to_be_visible()
            # Restore's old model base conflicts instead of erasing the later explicit choice.
            expect(conflicts).to_be_visible()
            expect(conflicts.get_by_text("Shared: retry-choice", exact=True)).to_be_visible()
            conflicts.get_by_role("button", name="Keep shared").click()
            expect(conflicts).not_to_be_visible()
            final = client.get("/v1/preferences/shared").json()
            assert final["conflicts"] == []
            assert (
                next(entry["value"] for entry in final["entries"] if entry["key"] == model["key"])
                == "retry-choice"
            )
            assert all(
                entry["value"] is None
                for entry in final["entries"]
                if entry["key"] != model["key"]
            )
            page.screenshot(path=str(evidence / f"restored-{width}.png"))
            assert (
                json.loads((native / "settings.json").read_text())["enabledPlugins"]["review@team"]
                is False
            )


@pytest.mark.parametrize("width", [1280, 390])
def test_composer_preserves_requested_choices_and_explicit_recovery(
    page, built_spa, tmp_path, output_path, width
):
    page.set_viewport_size({"width": width, "height": 1000})
    config = tmp_path / "server.yaml"
    config.write_text("shared_preferences_authority_id: composer-preferences\n")
    evidence = Path(output_path)
    evidence.mkdir(parents=True, exist_ok=True)
    with isolated_local_server(
        tmp_path / "server", extra_args=("--config", str(config))
    ) as server:
        with (
            httpx.Client(base_url=server, timeout=20, trust_env=False) as client,
            isolated_host(
                tmp_path / "host",
                server,
                name="Composer machine",
                base_env={"PATH": os.environ["PATH"]},
            ) as host,
        ):
            host.wait_online(client)
            initial = client.get("/v1/preferences/shared").json()
            for key, value in [
                ({"kind": "harness", "name": "default"}, "pi-native"),
                ({"kind": "model", "name": "model"}, "unavailable-saved-model"),
                ({"kind": "harness_args", "name": "pi-native"}, []),
                (
                    {
                        "kind": "plugin",
                        "name": {"ecosystem": "claude", "source": "team", "name": "review"},
                    },
                    False,
                ),
            ]:
                client.post(
                    "/v1/preferences/shared/operations",
                    json={
                        "protocol_version": 1,
                        "identity": initial["identity"],
                        "operation_id": str(uuid4()),
                        "installation_id": "source",
                        "key": key,
                        "value": value,
                        "expected_version": 0,
                    },
                ).raise_for_status()
            revision = client.get("/v1/preferences/shared").json()["revision"]
            preparations = []

            def record_request(request):
                if request.method == "POST" and request.url.endswith("/preferences"):
                    preparations.append(request.post_data_json)

            page.on("request", record_request)
            page.goto(server)
            notice = page.get_by_test_id("new-session-preferences")
            expect(notice).to_be_visible(timeout=30_000)
            expect(notice).not_to_contain_text("Loading shared preferences", timeout=20_000)
            expect(page.get_by_role("button", name=re.compile("^Pi")).first).to_be_visible()
            expect(page.get_by_test_id("new-chat-landing-harness-fallback")).not_to_be_visible()
            page.screenshot(path=str(evidence / f"composer-adopted-{width}.png"))
            assert client.get("/v1/preferences/shared").json()["revision"] == revision
            count = len(preparations)
            page.get_by_test_id("new-chat-landing-input").fill("Do not send this draft")
            page.wait_for_timeout(500)
            assert len(preparations) == count

            def unavailable(route):
                route.abort("failed")

            page.route("**/v1/hosts/*/preferences", unavailable)
            page.reload()
            expect(page.get_by_role("button", name="Retry preferences", exact=True)).to_be_visible(
                timeout=15_000
            )
            expect(page.get_by_test_id("new-chat-landing-submit")).to_be_disabled()
            page.screenshot(path=str(evidence / f"composer-recovery-{width}.png"))
            page.unroute("**/v1/hosts/*/preferences", unavailable)
            page.get_by_role("button", name="Retry preferences", exact=True).click()
            expect(
                page.get_by_role("button", name="Use temporary defaults", exact=True)
            ).not_to_be_visible(timeout=15_000)
            expect(page.get_by_role("button", name=re.compile("^Pi")).first).to_be_visible()
            page.route("**/v1/hosts/*/preferences", unavailable)
            page.reload()
            page.get_by_role("button", name="Use temporary defaults", exact=True).click(
                timeout=15_000
            )
            expect(notice).to_contain_text("Temporary defaults for this session")
            page.screenshot(path=str(evidence / f"composer-temporary-{width}.png"))
            assert client.get("/v1/preferences/shared").json()["revision"] == revision
