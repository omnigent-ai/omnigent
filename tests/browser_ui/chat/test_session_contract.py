"""Self-tests for the reusable browser-only chat backend."""

from __future__ import annotations

import base64

from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, message_item, model_option

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


def test_contract_drives_history_catalog_and_live_status(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """Exercise the fixture features later chat slices depend on."""
    chat = chat_session_contract
    chat.seed_transcript(24)
    chat.set_catalog(
        harness="claude-native",
        models=[
            model_option("sonnet", display_name="Sonnet", is_default=True),
            model_option("opus", display_name="Opus"),
        ],
        selected_model="sonnet",
    )
    page.goto(chat.url)
    chat.wait_for_stream()

    expect(page.get_by_text("Request 24", exact=False)).to_be_visible(timeout=20_000)
    expect(page.get_by_text("print('browser turn 24')", exact=False)).to_be_visible()
    session = page.evaluate(
        """async sessionId => {
            const response = await fetch(`/v1/sessions/${sessionId}`);
            return response.json();
        }""",
        chat.session_id,
    )
    assert session["harness"] == "claude-native"
    assert [model["id"] for model in session["model_options"]] == ["sonnet", "opus"]
    configure = page.get_by_role("button", name="Configure session")
    configure.click()
    page.get_by_role("menuitem", name="Model: Default").click()
    expect(page.get_by_role("menuitemcheckbox", name="Sonnet")).to_be_visible()
    expect(page.get_by_role("menuitemcheckbox", name="Opus")).to_be_visible()
    page.keyboard.press("Escape")

    working = page.get_by_test_id("working-indicator")
    chat.emit_busy("fixture-turn")
    expect(working).to_be_visible(timeout=10_000)
    reconnect_data = page.evaluate(
        """async sessionId => {
            const controller = new AbortController();
            const response = await fetch(`/v1/sessions/${sessionId}/stream`, {
                signal: controller.signal,
            });
            const reader = response.body.getReader();
            const chunks = [];
            const timer = setTimeout(() => controller.abort(), 100);
            try {
                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    chunks.push(value);
                }
            } catch (error) {
                if (error.name !== "AbortError") throw error;
            } finally {
                clearTimeout(timer);
            }
            return new TextDecoder().decode(
                chunks.reduce((all, chunk) => {
                    const joined = new Uint8Array(all.length + chunk.length);
                    joined.set(all);
                    joined.set(chunk, all.length);
                    return joined;
                }, new Uint8Array()),
            );
        }""",
        chat.session_id,
    )
    assert "session.status" not in reconnect_data
    chat.emit_idle("fixture-turn")
    expect(working).to_be_hidden(timeout=10_000)


def test_wait_for_stream_waits_for_reconnect_after_reload(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    chat = chat_session_contract
    page.goto(chat.url)
    chat.wait_for_stream()

    page.reload()
    chat.wait_for_stream()
    chat.emit_busy("reloaded-turn")

    expect(page.get_by_test_id("working-indicator")).to_be_visible(timeout=10_000)


def test_contract_can_hold_and_release_session_skills(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    chat = chat_session_contract
    chat.set_skills([{"name": "review", "description": "Review the current change."}])
    release = chat.hold_skills()

    skills_url = f"{chat.base_url}/v1/skills?session_id={chat.session_id}"
    with page.expect_request(skills_url):
        page.goto(chat.url)

    for _ in range(100):
        if chat.skill_requests:
            break
        page.wait_for_timeout(10)
    assert chat.skill_requests == [
        {
            "url": f"{chat.base_url}/v1/skills?session_id={chat.session_id}",
            "method": "GET",
            "body": None,
        }
    ]
    with page.expect_response(skills_url) as response_info:
        release()

    assert response_info.value.json() == {
        "skills": [{"name": "review", "description": "Review the current change."}]
    }


def test_contract_set_items_replaces_history_with_copies(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    chat = chat_session_contract
    chat.set_items([message_item("old", "user", "Old", response_id="old-response")])
    replacement = message_item("replacement", "user", "New", response_id="new-response")
    chat.set_items([replacement])
    replacement["role"] = "assistant"
    page.goto(chat.url)

    history = page.evaluate(
        """async sessionId => {
            const response = await fetch(`/v1/sessions/${sessionId}/items`);
            return response.json();
        }""",
        chat.session_id,
    )

    assert [item["id"] for item in history["data"]] == ["replacement"]
    assert history["data"][0]["role"] == "user"


def test_contract_updates_session_and_health_before_and_after_navigation(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    chat = chat_session_contract
    chat.update_session(
        created_at=123,
        sandbox_status={"stage": "provisioning"},
        permission_level="view",
        runner_id="runner-starting",
        host_id=None,
        workspace=None,
    )
    chat.set_health(runner_online=False, host_online=False)
    page.goto(chat.url)

    before = page.evaluate(
        """async sessionId => ({
            session: await fetch(`/v1/sessions/${sessionId}`).then(response => response.json()),
            health: await fetch("/health").then(response => response.json()),
        })""",
        chat.session_id,
    )
    assert before["session"]["created_at"] == 123
    assert before["session"]["sandbox_status"] == {"stage": "provisioning"}
    assert before["session"]["permission_level"] == "view"
    assert before["session"]["runner_id"] == "runner-starting"
    assert before["session"]["host_id"] is None
    assert before["session"]["workspace"] is None
    assert before["health"]["sessions"][chat.session_id] == {
        "runner_online": False,
        "host_online": False,
    }

    chat.update_session(sandbox_status={"stage": "failed", "error": "launch failed"})
    chat.set_health(runner_online=True)
    after = page.evaluate(
        """async sessionId => ({
            session: await fetch(`/v1/sessions/${sessionId}`).then(response => response.json()),
            health: await fetch("/health").then(response => response.json()),
        })""",
        chat.session_id,
    )
    assert after["session"]["sandbox_status"] == {
        "stage": "failed",
        "error": "launch failed",
    }
    assert after["health"]["sessions"][chat.session_id] == {
        "runner_online": True,
        "host_online": False,
    }


def test_contract_records_and_persists_session_patches(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    chat = chat_session_contract
    page.goto(chat.url)

    sessions = page.evaluate(
        """async sessionId => {
            const response = await fetch(`/v1/sessions/${sessionId}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ model_override: "opus", silent: true }),
            });
            const patched = await response.json();
            const current = await fetch(`/v1/sessions/${sessionId}`).then(result => result.json());
            return { patched, current };
        }""",
        chat.session_id,
    )

    assert chat.session_patches == [{"model_override": "opus", "silent": True}]
    assert sessions["patched"]["model_override"] == "opus"
    assert "silent" not in sessions["patched"]
    assert sessions["current"]["model_override"] == "opus"


def test_contract_rejects_unknown_session_patch_fields(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    chat = chat_session_contract
    page.goto(chat.url)

    result = page.evaluate(
        """async sessionId => {
            const response = await fetch(`/v1/sessions/${sessionId}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ title: "ignored", unexpected: true }),
            });
            return { status: response.status, body: await response.json() };
        }""",
        chat.session_id,
    )

    assert result == {
        "status": 422,
        "body": {
            "detail": [
                {
                    "type": "extra_forbidden",
                    "loc": ["body", "unexpected"],
                    "msg": "Extra inputs are not permitted",
                    "input": True,
                }
            ]
        },
    }
    assert chat.session_patches == []


def test_contract_records_binary_upload_and_returns_file_resource(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    chat = chat_session_contract
    chat.reject_uploads = False
    page.goto(chat.url)

    resource = page.evaluate(
        """async ({ sessionId, png }) => {
            const bytes = Uint8Array.from(atob(png), char => char.charCodeAt(0));
            const form = new FormData();
            form.append("file", new File([bytes], "pixel.png", { type: "image/png" }));
            const response = await fetch(`/v1/sessions/${sessionId}/resources/files`, {
                method: "POST",
                body: form,
            });
            return response.json();
        }""",
        {"sessionId": chat.session_id, "png": base64.b64encode(_PNG).decode()},
    )

    assert resource == {
        "id": "browser-upload-1",
        "object": "session.resource",
        "type": "file",
        "session_id": chat.session_id,
        "name": "pixel.png",
        "metadata": {
            "filename": "pixel.png",
            "bytes": len(_PNG),
            "created_at": 1_704_067_200,
            "source_metadata": None,
        },
    }
    assert len(chat.upload_requests) == 1
    request = chat.upload_requests[0]
    assert request["content_type"].startswith("multipart/form-data; boundary=")
    assert request["body_length"] > len(_PNG)
    assert _PNG in request["body_bytes"]
