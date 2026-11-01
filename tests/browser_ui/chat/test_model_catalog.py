"""Hermetic browser coverage for the in-session model catalog."""

from __future__ import annotations

import time
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, model_option


def _open_models(page: Page) -> None:
    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=20_000)
    gear.click()
    page.get_by_test_id("composer-agent-edit").click()
    expect(page.get_by_test_id("composer-agent-config-menu")).to_be_visible()


def test_catalog_rows_render_for_every_native_picker(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """The real chat page consumes runner-owned rows for every picker family."""
    chat = chat_session_contract
    chat.contract.json(f"/v1/sessions/{chat.session_id}/codex_goal", {"goal": None})
    for harness in (
        "claude-native",
        "codex-native",
        "cursor-native",
        "kiro-native",
        "opencode-native",
        "pi-native",
        "devin-native",
        "acp",
    ):
        wrapper = {
            "claude-native": "claude-code-native-ui",
            "codex-native": "codex-native-ui",
        }.get(harness)
        chat.update_session(labels={"omnigent.wrapper": wrapper} if wrapper else {})
        chat.set_catalog(
            harness=harness,
            models=[
                model_option("primary", display_name="Primary", is_default=True),
                model_option("alternate", display_name="Alternate"),
            ],
            selected_model="PRIMARY" if wrapper else "primary",
        )
        page.goto(chat.url)
        if wrapper:
            expect(page.get_by_test_id("composer-agent-model-value")).to_have_text("Primary")
        _open_models(page)

        rows = page.locator('[role="menuitemcheckbox"][data-model-id]')
        expect(rows).to_have_count(2)
        expect(rows.nth(0)).to_have_attribute("data-model-id", "primary")
        expect(rows.nth(0)).to_contain_text("Primary")
        expect(rows.nth(1)).to_have_attribute("data-model-id", "alternate")
        expect(rows.nth(1)).to_contain_text("Alternate")


def test_reported_model_highlights_only_an_exact_catalog_match(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """Reported provider IDs select exact rows; unknown reports stay honest."""
    chat = chat_session_contract
    chat.set_catalog(
        harness="claude-native",
        models=[
            model_option("sonnet", model="system.ai.claude-sonnet-5", display_name="Sonnet 5"),
            model_option("opus", model="system.ai.claude-opus-4-10", display_name="Opus 4.10"),
        ],
        selected_model="system.ai.claude-sonnet-5",
    )
    page.goto(chat.url)
    _open_models(page)
    expect(page.locator('[data-model-id="sonnet"]')).to_have_attribute("aria-checked", "true")
    expect(page.locator('[data-model-id="opus"]')).to_have_attribute("aria-checked", "false")

    chat.set_catalog(
        harness="claude-native",
        models=chat.models,
        selected_model="claude-opus-4-8[1m]",
    )
    page.goto(chat.url)
    _open_models(page)
    current = page.locator('[data-model-id="claude-opus-4-8[1m]"]')
    expect(current).to_have_count(1)
    expect(current).to_contain_text("(current)")
    expect(current).to_have_attribute("aria-checked", "true")
    expect(page.locator('[data-model-id="opus"]')).to_have_attribute("aria-checked", "false")


def test_selecting_a_catalog_row_patches_its_exact_id(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """A real picker selection reaches the session PATCH contract unchanged."""
    chat = chat_session_contract
    chat.set_catalog(
        harness="kiro-native",
        models=[
            model_option("primary", display_name="Primary", is_default=True),
            model_option("provider/alternate", display_name="Alternate"),
        ],
        selected_model="primary",
    )
    page.goto(chat.url)
    _open_models(page)
    session_path = f"/v1/sessions/{chat.session_id}"
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == session_path
            and response.status == 200
        )
    ) as patch_info:
        page.locator('[data-model-id="provider/alternate"]').click()

    expect(page.get_by_test_id("composer-agent-model-summary")).to_have_text("Alternate")
    assert patch_info.value.request.post_data_json == {"model_override": "provider/alternate"}
    assert chat.session_patches == [{"model_override": "provider/alternate"}]


def test_long_catalog_groups_by_creator_and_searches_by_keyboard(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """A long mixed catalog opens on the selected creator; search reaches a collapsed one."""
    chat = chat_session_contract
    rows = [
        ("anthropic/claude-opus-5", "Claude Opus 5"),
        ("anthropic/claude-sonnet-5", "Claude Sonnet 5"),
        ("amazon-bedrock/us.anthropic.claude-sonnet-5", "Claude Sonnet 5"),
        ("openai/gpt-5.5", "GPT-5.5"),
        ("openai/gpt-5.5-mini", "GPT-5.5 mini"),
        ("openai/gpt-5.5-nano", "GPT-5.5 nano"),
        ("openai/gpt-5.4", "GPT-5.4"),
        ("openai/gpt-5.4-mini", "GPT-5.4 mini"),
        ("openrouter/google/gemini-3-pro", "Gemini 3 Pro"),
        ("omnigent-openai/system.ai.glm-5-2", "GLM-5.2"),
        ("omnigent/databricks-kimi-k2-7-code", "Kimi K2.7 Code"),
        ("radius/balanced", "Balanced"),
    ]
    chat.set_catalog(
        harness="pi-native",
        models=[
            model_option(model_id, display_name=name, is_default=model_id == "openai/gpt-5.5")
            for model_id, name in rows
        ],
        selected_model="openai/gpt-5.5",
    )
    # Pi's picker selection is the session's requested model.
    chat.update_session(model_override="openai/gpt-5.5")
    page.goto(chat.url)
    _open_models(page)
    menu = page.get_by_test_id("composer-agent-config-menu")
    expect(menu.get_by_role("menuitem", name="OpenAI 5")).to_have_attribute(
        "aria-expanded", "true"
    )
    expect(menu.get_by_role("menuitem", name="Moonshot AI 1")).to_have_attribute(
        "aria-expanded", "false"
    )
    kimi = menu.locator('[data-model-id="omnigent/databricks-kimi-k2-7-code"]')
    expect(kimi).to_have_count(0)
    # Row names start where their group label does, past the chevron.
    label_left, row_left = menu.evaluate(
        """menu => [
            menu.querySelector('[aria-expanded="true"] span'),
            menu.querySelector('[role="group"] [role="menuitemcheckbox"]'),
        ].map(element => {
            const range = document.createRange();
            range.selectNodeContents(element);
            return range.getBoundingClientRect().left;
        })"""
    )
    assert abs(label_left - row_left) < 0.5, (label_left, row_left)

    # The menu keeps its height cap, and search stays reachable after scrolling.
    scrolled = menu.evaluate(
        "element => { element.scrollTop = element.scrollHeight; return element.scrollTop; }"
    )
    assert scrolled > 0
    search = menu.get_by_role("textbox", name="Search models")
    expect(search).to_be_in_viewport()
    box = menu.bounding_box()
    assert box is not None and box["height"] <= 576

    # A broad search keeps the list scrollable; its first match must start
    # below the pinned search rather than stay scrolled away beneath it.
    search.fill("a")
    search_box = search.bounding_box()
    first_group = menu.get_by_role("group").first.bounding_box()
    assert search_box is not None and first_group is not None
    assert search_box["y"] + search_box["height"] <= first_group["y"] + 1

    search.fill("moonshot")
    search.press("ArrowDown")
    expect(kimi).to_be_focused()
    session_path = f"/v1/sessions/{chat.session_id}"
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == session_path
            and response.status == 200
        )
    ):
        page.keyboard.press("Enter")

    expect(page.get_by_test_id("composer-agent-model-summary")).to_have_text("Kimi K2.7 Code")
    assert chat.session_patches == [{"model_override": "omnigent/databricks-kimi-k2-7-code"}]


def test_picker_open_and_selection_do_not_refetch_catalog_or_session(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """Opening and using preloaded rows does not trigger a click-time GET."""
    chat = chat_session_contract
    chat.set_catalog(
        harness="opencode-native",
        models=[
            model_option("primary", display_name="Primary", is_default=True),
            model_option("alternate", display_name="Alternate"),
        ],
        selected_model="primary",
    )
    page.goto(chat.url)
    chat.wait_for_stream()
    expect(page.get_by_test_id("composer-config-gear")).to_be_visible(timeout=20_000)
    page.wait_for_timeout(500)

    unexpected_gets: list[str] = []
    session_path = f"/v1/sessions/{chat.session_id}"

    def record_request(request: Request) -> None:
        path = urlparse(request.url).path
        if request.method == "GET" and (path == session_path or path.endswith("/model-options")):
            unexpected_gets.append(request.url)

    page.on("request", record_request)
    _open_models(page)
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == session_path
            and response.status == 200
        )
    ) as patch_info:
        page.locator('[data-model-id="alternate"]').click()
    expect(page.get_by_test_id("composer-agent-model-summary")).to_have_text("Alternate")
    page.wait_for_timeout(500)

    assert patch_info.value.request.post_data_json == {"model_override": "alternate"}
    assert chat.session_patches == [{"model_override": "alternate"}]
    assert unexpected_gets == []


def _serve_launching_sandbox(chat: ChatSessionContract, *, harness: str, wrapper: str) -> None:
    """Back a native session whose sandbox is provisioning and has no host yet."""
    chat.contract.json(
        "/v1/info",
        {
            "accounts_enabled": False,
            "single_user": True,
            "needs_setup": False,
            "managed_sandboxes_enabled": True,
            "sandbox_provider": "agent_sandbox",
            "sandbox_providers": ["agent_sandbox"],
            "sandbox_provider_capabilities": {"agent_sandbox": {"default_models": True}},
        },
    )
    chat.contract.json(
        f"/v1/sandbox-providers/agent_sandbox/harnesses/{harness}/model-options",
        {
            "configured": False,
            "models": [
                model_option("gateway/opus", display_name="Opus via gateway"),
                model_option("gateway/sonnet", display_name="Sonnet via gateway"),
            ],
            "configuration_revision": None,
            "provider_label": "Gateway",
            "default_model": None,
            "status": "ready",
        },
    )
    chat.set_catalog(harness=harness, models=[], selected_model="")
    chat.set_health(runner_online=False, host_online=True)
    chat.update_session(
        created_at=time.time(),
        host_id=None,
        workspace=None,
        llm_model=None,
        labels={"omnigent.wrapper": wrapper},
        sandbox_status={"stage": "provisioning", "provider": "agent_sandbox"},
    )


@pytest.mark.parametrize(
    ("harness", "wrapper"),
    [
        ("claude-native", "claude-code-native-ui"),
        ("codex-native", "codex-native-ui"),
        ("pi-native", "pi-native-ui"),
    ],
)
def test_launching_sandbox_offers_its_provider_catalog(
    page: Page,
    chat_session_contract: ChatSessionContract,
    harness: str,
    wrapper: str,
) -> None:
    """Before its sandbox has a host, a session offers the provider's preview rows."""
    chat = chat_session_contract
    _serve_launching_sandbox(chat, harness=harness, wrapper=wrapper)
    page.goto(chat.url)
    expect(page.get_by_test_id("runner-starting-indicator").first).to_contain_text(
        "Provisioning sandbox"
    )
    _open_models(page)

    rows = page.locator('[role="menuitemcheckbox"][data-model-id]')
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_contain_text("Opus via gateway")
    expect(rows.nth(1)).to_contain_text("Sonnet via gateway")
    session_path = f"/v1/sessions/{chat.session_id}"
    for model_id in ("gateway/sonnet", "gateway/opus"):
        row = page.locator(f'[data-model-id="{model_id}"]')
        if not row.is_visible():
            _open_models(page)
        # No runner exists to confirm the first pick, so the rows stay usable.
        expect(row).not_to_have_attribute("aria-disabled", "true", timeout=5_000)
        with page.expect_response(
            lambda response: (
                response.request.method == "PATCH"
                and urlparse(response.url).path == session_path
                and response.status == 200
            )
        ):
            row.click()
    # Saved for the launch to read.
    assert chat.session_patches == [
        {"model_override": "gateway/sonnet"},
        {"model_override": "gateway/opus"},
    ]


def test_session_catalog_replaces_the_launch_preview(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """The session's own catalog wins over the provider preview once it resolves."""
    chat = chat_session_contract
    _serve_launching_sandbox(chat, harness="claude-native", wrapper="claude-code-native-ui")
    page.goto(chat.url)
    chat.wait_for_stream()
    _open_models(page)
    rows = page.locator('[role="menuitemcheckbox"][data-model-id]')
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_have_attribute("data-model-id", "gateway/opus")

    chat.set_catalog(
        harness="claude-native",
        models=[
            model_option("opus", display_name="Opus (session)", is_default=True),
            model_option("haiku", display_name="Haiku (session)"),
        ],
        selected_model="opus",
    )
    chat.emit(
        {
            "event": "session.model_options",
            "data": {"type": "session.model_options", "conversation_id": chat.session_id},
        }
    )
    expect(page.locator('[data-model-id="gateway/opus"]')).to_have_count(0)
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_have_attribute("data-model-id", "opus")
    expect(rows.nth(1)).to_have_attribute("data-model-id", "haiku")


def test_bound_host_catalog_replaces_the_launch_preview(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """Once the launch binds its host, the host's catalog replaces the preview."""
    chat = chat_session_contract
    _serve_launching_sandbox(chat, harness="claude-native", wrapper="claude-code-native-ui")
    page.goto(chat.url)
    chat.wait_for_stream()
    _open_models(page)
    rows = page.locator('[role="menuitemcheckbox"][data-model-id]')
    expect(rows.nth(0)).to_have_attribute("data-model-id", "gateway/opus")

    # The host lists its catalog while the session's own catalog is still empty.
    chat.models = [
        model_option("opus", display_name="Opus (host)"),
        model_option("haiku", display_name="Haiku (host)"),
    ]
    chat.update_session(host_id=chat.host_id, model_options=[])
    chat.emit(
        {
            "event": "session.sandbox_status",
            "data": {
                "type": "session.sandbox_status",
                "conversation_id": chat.session_id,
                "stage": "connecting",
                "provider": "agent_sandbox",
            },
        }
    )
    expect(page.locator('[data-model-id="gateway/opus"]')).to_have_count(0)
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_have_attribute("data-model-id", "opus")
    expect(rows.nth(1)).to_have_attribute("data-model-id", "haiku")


@pytest.mark.parametrize("mobile", [False, True], ids=["desktop", "mobile"])
@pytest.mark.parametrize("entrypoint", ["tray", "workspace"])
def test_side_chat_model_settings_before_and_after_first_message(
    page: Page,
    chat_session_contract: ChatSessionContract,
    mobile: bool,
    entrypoint: str,
) -> None:
    """A generic fork receives draft settings; later edits target only that fork."""
    from tests.browser_ui.chat.session_contract import list_payload

    chat = chat_session_contract
    if mobile:
        page.set_viewport_size({"width": 390, "height": 844})
    chat.set_catalog(
        harness="claude-native",
        models=[
            model_option("sonnet", display_name="Sonnet", is_default=True),
            model_option("opus", display_name="Opus"),
        ],
        selected_model="sonnet",
    )
    chat.update_session(
        permission_level=4,
        runner_id="browser-runner",
        runner_online=True,
        reasoning_effort="high",
        labels={"omnigent.wrapper": "claude-code-native-ui"},
    )
    child_id = "browser-side-settings"
    child = {
        **chat._session(),
        "id": child_id,
        "title": "Side chat",
        "labels": {
            "omnigent.wrapper": "claude-code-native-ui",
            "omnigent.side_chat": "1",
            "omnigent.side_chat.source_id": chat.session_id,
        },
    }
    forks: list[dict] = []
    patches: list[dict] = []
    messages: list[dict] = []

    def fork(request: Request) -> dict:
        body = request.post_data_json
        forks.append(body)
        child.update(
            {key: body[key] for key in ("model_override", "reasoning_effort") if key in body}
        )
        return child

    def patch(request: Request) -> dict:
        body = request.post_data_json
        patches.append(body)
        child.update(body)
        return child

    def message(request: Request) -> dict:
        messages.append(request.post_data_json)
        return {"queued": True}

    contract = chat.contract
    path = f"/v1/sessions/{child_id}"
    contract.json(f"/v1/sessions/{chat.session_id}/fork", fork, method="POST")
    contract.json(path, lambda _request: child)
    contract.json(path, patch, method="PATCH")
    contract.json(f"{path}/events", message, method="POST")
    contract.json(f"{path}/agent", chat._agent())
    for resource in ("items", "child_sessions", "resources/terminals"):
        contract.json(f"{path}/{resource}", list_payload([]))
    contract.response(f"{path}/read-state", method="PUT")
    contract.route(f"**{path}/stream*", lambda route: route.continue_(url=chat.stream.url))

    page.goto(chat.url)
    chat.wait_for_stream()
    expect(page.get_by_placeholder("Send a message…")).to_be_visible()
    if entrypoint == "tray":
        page.get_by_test_id("composer-attach").click()
        page.get_by_role("menuitem", name="Start a new side chat").click()
    elif mobile:
        page.get_by_role("button", name="Conversation actions").click()
        page.get_by_role("menuitem", name="Side chats", exact=True).click()
        page.get_by_role("button", name="New side chat", exact=True).click()
    else:
        toggle = page.get_by_role("button", name="Expand right panel")
        if toggle.is_visible():
            toggle.click()
        page.get_by_role("complementary", name="Workspace").get_by_role(
            "button", name="Open new", exact=True
        ).click()
        page.get_by_role("menuitem", name="Side chat", exact=True).click()

    picker = page.get_by_role("button", name="Side chat model and effort")
    picker.click()
    page.get_by_test_id("side-chat-model-edit").click()
    page.get_by_role("menuitemcheckbox", name="Opus", exact=True).click()
    page.get_by_role("menuitemcheckbox", name="Low", exact=True).click()
    page.keyboard.press("Escape")
    # The tray creates a live fork; only an unsent draft displays the requested model.
    expect(picker).to_contain_text("Sonnet" if entrypoint == "tray" else "Opus")
    expect(picker).to_contain_text("Low")
    assert len(forks) == (1 if entrypoint == "tray" else 0)
    assert chat.session_patches == []
    page.get_by_test_id("side-chat-input").fill("Explain this in the side chat.")
    with page.expect_response(
        lambda response: (
            response.url.endswith(f"{path}/events") and response.request.method == "POST"
        )
    ):
        page.get_by_test_id("side-chat-send").click()
    expect(page.get_by_test_id("side-chat-input")).to_have_value("")
    assert len(forks) == 1
    if entrypoint == "workspace":
        assert forks[0]["model_override"] == "opus"
        assert forks[0]["reasoning_effort"] == "low"
    assert child["model_override"] == "opus"
    assert child["reasoning_effort"] == "low"
    assert forks[0]["side_chat"] is True
    assert len(messages) == 1
    chat.wait_for_stream()
    child["llm_model"] = "opus"
    chat.emit(
        {
            "event": "session.model",
            "data": {"conversation_id": child_id, "model": "opus"},
        }
    )
    expect(picker).to_contain_text("Opus")
    expect(page.get_by_test_id("composer-config-gear")).to_contain_text("Sonnet")
    picker.click()
    page.get_by_test_id("side-chat-model-edit").click()
    expect(page.get_by_role("menuitemcheckbox", name="Opus", exact=True)).to_have_attribute(
        "aria-checked", "true"
    )
    page.get_by_role("menuitemcheckbox", name="High", exact=True).click()
    expect(picker).to_contain_text("High")
    assert patches[-1] == {"reasoning_effort": "high"}
    assert chat.session_patches == []
    assert chat._session()["reasoning_effort"] == "high"
    assert chat._session()["llm_model"] == "sonnet"
