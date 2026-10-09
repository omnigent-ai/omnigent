"""The same rollout faults driven through the web client API and the composer."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, Route, WebSocketRoute, expect

from tests._helpers.replica_handoff import HandoffLab, eventually, handoff_lab

_STORE = "(await import('/src/store/chatStore.ts')).useChatStore.getState()"
CASES = (
    "wrong_replica",
    "lost_ack",
    "lost_forward",
    "completed_turn",
    "lost_idle",
    "missing_header",
    "stale_host",
    "terminal_reveal",
    "stopped_turn",
    "mirrored_history",
    "lost_runner_ack",
)


@pytest.fixture(name="replica_lab")
def replica_lab(tmp_path: Path, mock_llm_server_url: str) -> Iterator[HandoffLab]:
    if os.environ.get("OMNIGENT_E2E_REPLICA_HANDOFF") != "1":
        pytest.skip("set OMNIGENT_E2E_REPLICA_HANDOFF=1; requires web dependencies and Chromium")
    with handoff_lab(tmp_path, mock_llm_server_url) as lab:
        yield lab


class Driver:
    def __init__(self, page: Page, lab: HandoffLab, *, ui: bool) -> None:
        self.page = page
        self.lab = lab
        self.ui = ui
        self.errors: list[str] = []
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.add_init_script(
            f"localStorage.setItem('omnigent:imports-reviewed:{lab.host_id}', 'reviewed')"
        )
        page.goto(f"{lab.ui_url}/c/{lab.session_id}?view=chat")
        expect(page.get_by_placeholder("Send a message…")).to_be_visible(timeout=60_000)
        expect(page.get_by_text("Ready for the rolling update.", exact=True)).to_be_visible(
            timeout=30_000
        )
        page.evaluate("""() => {
          window.handoffErrors = [];
          const scan = () => {
            const selector = '[data-testid="error-pill"], [data-sonner-toast][data-type="error"]';
            for (const el of document.querySelectorAll(selector)) {
              if (el.getBoundingClientRect().height) window.handoffErrors.push(el.innerText);
            }
          };
          new MutationObserver(scan).observe(document.body, {childList:true, subtree:true});
          scan();
        }""")

    def wait(self, check: Callable[[], Any], what: str, timeout: float = 30) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if result := check():
                return result
            self.page.wait_for_timeout(50)
        raise AssertionError(f"Timed out waiting for {what}")

    def send(self, text: str) -> None:
        if self.ui:
            composer = self.page.get_by_label("Message the agent")
            composer.fill(text)
            self.page.get_by_role("button", name="Send", exact=True).click()
        else:
            self.page.evaluate(
                f"""async args => {{
              const store = {_STORE};
              void store.send(args.text, args.agentId);
            }}""",
                {"text": text, "agentId": self.lab.agent_id},
            )

    def queue(self, text: str) -> None:
        if self.ui:
            self.page.locator('textarea[aria-label="Message the agent"]').fill(text)
            self.page.get_by_role("button", name="Send", exact=True).click()
        else:
            self.page.evaluate(f"async text => {{ ({_STORE}).enqueueMessage(text); }}", text)

    def assert_clean(self, prompt: str, reply: str, *, turns: int = 1) -> None:
        lab = self.lab
        self.page.wait_for_timeout(100)
        visible_errors = self.page.evaluate("window.handoffErrors")
        assert not visible_errors, (
            f"browser displayed a send error during recovery: {visible_errors}"
        )
        self.wait(lambda: reply in lab.messages("assistant"), "saved reply")
        self.wait(lambda: lab.snapshot()["status"] == "idle", "session to settle")
        self.page.wait_for_timeout(500)
        assert len(lab.model_requests()) - lab.baseline_calls == turns, (
            "the same prompt ran more than once"
        )
        assert lab.messages("user").count(prompt) == 1
        assert lab.messages("assistant").count(reply) == 1
        expect(
            self.page.locator('[data-testid="assistant-text-section"]').filter(has_text=reply)
        ).to_have_count(1, timeout=10_000)
        state = self.page.evaluate(f"""async () => {{
          const s = {_STORE};
          return {{status: s.status, failedSendDraft: s.failedSendDraft,
                   errors: s.blocks.filter(b => b.type === 'error')}};
        }}""")
        assert not state["errors"], f"client kept an error after delivery: {state['errors']}"
        assert state["failedSendDraft"] is None, f"delivered prompt restored as draft: {state}"
        assert not self.page.evaluate("window.handoffErrors"), (
            "browser displayed a transient or persistent send error"
        )
        assert not self.errors
        expect(self.page.get_by_placeholder("Send a message…")).to_have_value("")


def run_handoff_case(page: Page, lab: HandoffLab, case: str, *, ui: bool) -> None:
    if case == "terminal_reveal":

        def terminal_ready() -> bool:
            response = lab.client.get(f"/v1/sessions/{lab.session_id}/resources/terminals")
            response.raise_for_status()
            return bool(response.json()["data"])

        eventually(terminal_ready, "runner-created terminal", 30)
        sockets: list[WebSocketRoute] = []

        def attach(ws: WebSocketRoute) -> None:
            sockets.append(ws)
            ws.connect_to_server()

        page.route_web_socket(re.compile(r"/resources/terminals/.*/attach"), attach)
    driver = Driver(page, lab, ui=ui)
    proxy = lab.proxy
    prompt = f"Check the rolling update: {case}."
    reply = f"The {case} message completed once."
    lab.configure([{"text": reply}, {"text": f"DUPLICATE: {reply}"}])
    if case == "terminal_reveal":
        surface = page.get_by_test_id("main-terminal-view")
        terminal = surface.get_by_test_id("terminal-view")
        expect(surface).to_have_attribute("data-visible", "false")
        expect(terminal).to_have_attribute("data-state", "connected", timeout=30_000)
        page.clock.install()
        page.get_by_test_id("view-mode-terminal").click()
        sockets[-1].close(code=4400)
        expect(surface.get_by_test_id("terminal-reconnecting")).to_be_visible()
        page.screenshot(path=lab.root / "terminal-reconnecting.png")
        page.clock.fast_forward(1000)
        expect(terminal).to_have_attribute("data-state", "connected", timeout=30_000)
        page.clock.fast_forward(31_000)
        page.get_by_test_id("view-mode-chat").click()
        for attempt in range(16):
            before = len(sockets)
            sockets[-1].close(code=4400)
            expect(terminal).to_have_attribute("data-state", "closed")
            page.clock.fast_forward(31_000)
            if attempt < 15:
                driver.wait(
                    lambda before=before: len(sockets) > before, "background terminal reconnect"
                )
                expect(terminal).to_have_attribute("data-state", "connected", timeout=30_000)
            else:
                assert len(sockets) == before, (
                    "the terminal kept retrying after its budget expired"
                )
        page.get_by_test_id("view-mode-terminal").click()
        expect(surface).to_have_attribute("data-visible", "true")
        expect(terminal).to_have_attribute("data-state", "connected", timeout=10_000)
        expect(page.get_by_text(re.compile("Bridge closed"))).to_have_count(0)
        page.screenshot(path=lab.root / "terminal-recovered.png")
        page.get_by_test_id("view-mode-chat").click()
        driver.send(prompt)
    elif case == "stale_host":
        proxy.gate("updates", hold=True)
        page.clock.install()
        page.clock.pause_at(datetime.now(UTC) + timedelta(seconds=1))
        page.wait_for_timeout(100)
        replacement_host = lab.move_to_new_host()
        assert (
            page.evaluate(
                "async id => (await import('/src/lib/sessionHost.ts')).getSessionHost(id)",
                lab.session_id,
            )
            == lab.host_id
        ), "the old page learned the new host before its routing miss"
        driver.send(prompt)
        driver.wait(
            lambda: any(r["status"] == 400 for r in proxy.seen("message_response")),
            "a real rejection from the replica selected by the stale host key",
        )
        proxy.cut(tunnels=False)
        page.clock.resume()
        driver.assert_clean(prompt, reply)
        requests = proxy.seen("message_response")
        assert requests[0]["host_key"] == lab.host_id
        assert requests[-1]["host_key"] == replacement_host
        assert requests[-1]["status"] == 202
        return
    elif case == "wrong_replica":
        proxy.configure(target=lab.b.base_url)
        driver.send(prompt)
        driver.wait(
            lambda: any(
                r["status"] == 400
                and isinstance(r["body"], dict)
                and isinstance(r["body"].get("error"), dict)
                and r["body"]["error"].get("code") == "wrong_replica"
                for r in proxy.seen("message_response")
            ),
            "real wrong_replica response",
        )
        assert lab.messages("user").count(prompt) == 0, "wrong_replica must reject before saving"
        lab.handoff()
    elif case == "lost_ack":
        proxy.gate("browser", hold=True)

        def lose_response(route: Route) -> None:
            if route.request.method != "POST" or prompt not in (route.request.post_data or ""):
                route.continue_()
                return
            response = route.fetch()
            proxy.note("lost_ack", status=response.status, body=response.json())
            # The real server accepts it, but no HTTP response reaches fetch.
            route.abort("connectionclosed")

        page.route(f"**/v1/sessions/{lab.session_id}/events", lose_response)
        driver.send(prompt)
        driver.wait(lambda: proxy.seen("lost_ack"), "accepted POST response to be lost")
        assert proxy.seen("lost_ack")[0]["status"] == 202
        page.wait_for_timeout(1000)
        proxy.gate("browser", hold=False)
    elif case == "lost_forward":
        proxy.configure(drop_forward=True)
        driver.send(prompt)
        driver.wait(
            lambda: any(
                r["status"] == 503 and "message was persisted" in str(r["body"])
                for r in proxy.seen("message_response")
            ),
            "real saved-message 503",
        )
        assert lab.messages("user").count(prompt) == 1
        page.wait_for_timeout(500)
        lab.handoff()
    elif case == "lost_runner_ack":
        followup = "Continue after the runner's acknowledgement was lost."
        followup_reply = "The next turn completed after the lost acknowledgement."
        lab.configure([{"text": reply, "pause_after": 1}, {"text": followup_reply}])
        proxy.configure(drop_forward_response=True)
        driver.send(prompt)
        driver.wait(lambda: proxy.seen("lost_runner_ack"), "accepted runner response to be lost")
        assert proxy.seen("lost_runner_ack")[0]["status"] == 202
        driver.wait(lab.model_paused, "accepted turn to reach the model")
        driver.wait(
            lambda: any(r["status"] == 503 for r in proxy.seen("message_response")),
            "server to report its lost forward response",
        )
        assert len(lab.model_requests()) - lab.baseline_calls == 1
        assert lab.messages("user").count(prompt) == 1
        # The browser reconnects after the runner, so a transient receipt alone
        # cannot settle this send. The new stream must read the runner's receipts.
        proxy.gate("browser_connect", hold=True)
        lab.handoff()
        lab.release_model()
        driver.wait(lambda: reply in lab.messages("assistant"), "accepted turn's saved reply")
        driver.wait(lambda: lab.snapshot()["status"] == "idle", "accepted turn to settle")
        assert not any(r["target"] == lab.b.base_url for r in proxy.seen("browser_stream")), (
            "the browser reconnected before the runner finished"
        )
        proxy.gate("browser_connect", hold=False)
        driver.send(followup)
        # Sends share a chain: the next POST proves the original send settled.
        driver.wait(lambda: followup in lab.messages("user"), "follow-up after the lost receipt")
        driver.assert_clean(followup, followup_reply, turns=2)
        return
    elif case == "lost_idle":
        proxy.configure(drop_status="idle")
        driver.send(prompt)
        driver.wait(lambda: proxy.seen("lost_status"), "final idle edge to lose its connection")
        driver.wait(lambda: reply in lab.messages("assistant"), "completed reply before handoff")
        assert lab.snapshot()["status"] == "running", "the old server did not lose the idle edge"
        followup = "Continue after the runner reconnects."
        followup_reply = "The next turn completed after reconnecting."
        lab.configure([{"text": followup_reply}])
        driver.queue(followup)
        lab.handoff()
        driver.wait(
            lambda: followup in lab.messages("user"),
            "queued follow-up to send after reconnect; "
            "the lost idle event left the composer stuck",
        )
        driver.assert_clean(followup, followup_reply, turns=2)
        return
    elif case == "mirrored_history":
        lab.configure([{"text": reply}, {"text": "UNSOLICITED: mirrored history ran as input."}])
        driver.send(prompt)
        driver.assert_clean(prompt, reply)
        mirror = "This is a transcript copy, already handled elsewhere."
        lab.client.post(
            f"/v1/sessions/{lab.session_id}/events",
            json={
                "type": "external_conversation_item",
                "data": {
                    "item_type": "message",
                    "item_data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": mirror}],
                        "history_only": False,
                    },
                    "response_id": "turn_external",
                },
            },
        ).raise_for_status()
        expect(page.get_by_text(mirror, exact=True)).to_be_visible()
        assert len(lab.model_requests()) - lab.baseline_calls == 1
        lab.handoff()
        driver.wait(
            lambda: any(
                record["target"] == lab.b.base_url for record in proxy.seen("history_delivered")
            ),
            "reconnect scan to read mirrored history",
        )
        page.wait_for_timeout(1000)
        assert len(lab.model_requests()) - lab.baseline_calls == 1, (
            "reconnect executed transcript-only history as a new model turn"
        )
        driver.assert_clean(prompt, reply)
        expect(page.get_by_text(mirror, exact=True)).to_be_visible()
        return
    elif case == "stopped_turn":
        lab.configure([{"text": "This turn will be interrupted.", "pause_after": 1}])
        driver.send("Interrupt this turn before it finishes.")
        driver.wait(lab.model_paused, "turn to start before Stop")
        if ui:
            page.get_by_role("button", name="Interrupt", exact=True).click()
        else:
            page.evaluate(f"async () => {{ ({_STORE}).stop(); }}")
        driver.wait(lambda: lab.snapshot()["status"] == "idle", "interrupted turn to stop")
        markers = driver.wait(
            lambda: [
                item
                for item in lab.items()
                if str(item.get("response_id", "")).startswith("cancel_")
                and item.get("role") == "user"
            ],
            "the runner's interruption marker to be saved",
        )
        lab.release_model()
        lab.configure([{"text": reply}, {"text": "UNSOLICITED: the Stop marker ran as a prompt."}])
        driver.send(prompt)
        driver.assert_clean(prompt, reply, turns=2)
        lab.handoff()
        driver.wait(
            lambda: any(
                record["target"] == lab.b.base_url for record in proxy.seen("history_delivered")
            ),
            "reconnect scan to read the completed history after Stop",
        )
        driver.assert_clean(prompt, reply, turns=2)
        marker_ids = {item["id"] for item in markers}
        assert not any(
            event.get("type") == "session.input.consumed"
            and event.get("data", {}).get("item_id") in marker_ids
            for record in proxy.seen("runner_events")
            for event in record["events"]
        ), "runner acknowledged internal history as a new prompt"
        return
    elif case in {"completed_turn", "missing_header"}:
        lab.configure([{"text": reply, "pause_after": 1}, {"text": f"DUPLICATE: {reply}"}])
        driver.send(prompt)
        driver.wait(lab.model_paused, "model to pause after its opening event")
        driver.wait(
            lambda: any(
                event.get("type") == "response.in_progress"
                for record in proxy.seen("browser_events")
                for event in record["events"]
            ),
            "opening response event in the old browser stream",
        )
        if case == "completed_turn":
            proxy.gate("history", hold=True)
            proxy.gate("runner_stream", hold=True)
        lab.handoff()
        if case == "completed_turn":
            driver.wait(
                lambda: any(
                    prompt in str(record["body"]) and record["target"] == lab.b.base_url
                    for record in proxy.seen("history_read")
                ),
                "real trailing-user history snapshot",
            )
            lab.release_model()
            driver.wait(
                lambda: proxy.runner_snapshot(lab.session_id).get("status") == "idle",
                "the existing SDK turn to finish locally",
            )
            assert reply not in lab.messages("assistant"), (
                "reply reached storage before reconnect recovery"
            )
            proxy.gate("history", hold=False)
            page.wait_for_timeout(1000)
            proxy.gate("runner_stream", hold=False)
        else:
            driver.wait(
                lambda: any(
                    record["target"] == lab.b.base_url for record in proxy.seen("browser_stream")
                ),
                "browser stream on the replacement",
            )
            driver.wait(
                lambda: any(
                    record["target"] == lab.b.base_url
                    and record["method"] == "POST"
                    and record["path"] == "/v1/sessions"
                    for record in proxy.seen("runner_response")
                ),
                "replacement session initialization to finish during the active turn",
            )
            driver.wait(
                lambda: any(
                    record["target"] == lab.b.base_url
                    for record in proxy.seen("history_delivered")
                ),
                "reconnect history to arrive while the model is still running",
            )
            assert proxy.runner_snapshot(lab.session_id)["status"] == "running"
            lab.release_model()
            driver.wait(lambda: reply in lab.messages("assistant"), "reply saved by replacement")
            driver.wait(lambda: lab.snapshot()["status"] == "idle", "turn to finish")
            driver.wait(
                lambda: any(
                    event.get("type") == "response.completed"
                    for record in proxy.seen("browser_events")
                    if record["target"] == lab.b.base_url
                    for event in record["events"]
                ),
                "completion on the replacement stream",
            )
            opening = next(
                (
                    event["response"]["id"]
                    for record in proxy.seen("browser_events")
                    if record["target"] == lab.a.base_url
                    for event in record["events"]
                    if event.get("type") == "response.in_progress"
                ),
                None,
            )
            assert opening is not None, "original stream did not deliver the response header"
            replacement = [
                event
                for record in proxy.seen("browser_events")
                if record["target"] == lab.b.base_url
                for event in record["events"]
            ]
            saved = next(
                (e["item"] for e in replacement if e["type"] == "response.output_item.done"),
                None,
            )
            assert saved is not None, "replacement stream did not deliver the saved reply"
            assert saved["response_id"] != opening, "replacement did not miss the original header"
            assert any(
                e["type"] == "response.completed" and e["response"]["id"] == opening
                for e in replacement
            )
            assert lab.messages("assistant").count(reply) == 1
            assert len(lab.model_requests()) - lab.baseline_calls == 1
            # Reconnect without reloading the page: the old streamed block remains
            # while the app reconciles the reply's newly persisted item ID.
            before = len(proxy.seen("browser_stream"))
            proxy.cut(tunnels=False)
            driver.wait(
                lambda: len(proxy.seen("browser_stream")) > before, "history reconciliation"
            )
    else:
        raise AssertionError(f"Unknown handoff case: {case}")
    driver.assert_clean(prompt, reply)
