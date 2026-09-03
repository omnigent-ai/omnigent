"""End-to-end test: web "Send now" must steer into an active native Pi turn.

Regression guard for the Pi native follow-up steering bug: clicking **Send
now** on a queued web message mid-turn flows through
``PiNativeExecutor.enqueue_session_message`` -> ``enqueue_user_message`` ->
the generated extension's inbox poller -> ``pi.sendUserMessage``. The Pi SDK
delivers ``deliverAs: "steer"`` into the ACTIVE turn (after the current tool
calls, before the next LLM call) but holds ``deliverAs: "followUp"`` until the
whole agent loop finishes -- so a follow-up delivery leaves the message
visibly queued in the Pi CLI even though the web chat view reported success.

Mirrors ``tests/test_pi_native_interrupt_replay_e2e.py``: the Python bridge
enqueues through the same helpers the runner uses, and the REAL generated
extension runs under Node against a mocked Pi context that emulates the SDK's
documented mid-turn queue semantics.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from omnigent import pi_native_bridge

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is required to execute the pi-native extension",
)


def test_send_now_message_is_steered_into_active_pi_turn(tmp_path: Path) -> None:
    """
    A mid-turn web "Send now" message must be steered into the active Pi turn.

    The message is queued through ``pi_native_bridge.enqueue_user_message()``
    (the exact call ``PiNativeExecutor.enqueue_session_message`` makes for a
    web-originated steer) while the mocked Pi context reports a running turn.
    The real extension's inbox poller must hand it to ``pi.sendUserMessage``
    with steering delivery -- a follow-up delivery leaves it queued in the Pi
    CLI until the turn ends, which is the reported bug.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    extension_path, config_path = pi_native_bridge.write_extension_files(
        bridge_dir,
        session_id="conv_send_now_e2e",
        server_url="",
        conversation_url="",
    )
    pi_native_bridge.enqueue_user_message(bridge_dir, "steer me into the active turn")

    script = tmp_path / "send_now_scenario.mjs"
    script.write_text(
        textwrap.dedent(
            r"""
            import { createRequire } from "module";
            import fs from "fs";

            const require = createRequire(import.meta.url);
            const extensionPath = process.env.PI_NATIVE_EXTENSION_PATH;
            const configPath = process.env.OMNIGENT_PI_NATIVE_CONFIG;
            const config = JSON.parse(fs.readFileSync(configPath, "utf8"));

            function assert(cond, message) {
              if (!cond) throw new Error(message);
            }

            function sleep(ms) {
              return new Promise((resolve) => setTimeout(resolve, ms));
            }

            async function waitForInboxEmpty() {
              const deadline = Date.now() + 3000;
              while (true) {
                const pending = fs
                  .readdirSync(config.inboxDir)
                  .filter((name) => name.endsWith(".json"));
                if (pending.length === 0) return;
                if (Date.now() > deadline) {
                  throw new Error(`inbox did not drain: ${pending.join(",")}`);
                }
                await sleep(20);
              }
            }

            const handlers = {};
            // Emulate the Pi SDK's documented sendUserMessage semantics while a
            // turn is streaming: deliverAs "steer" is injected into the ACTIVE
            // turn; deliverAs "followUp" (or no mode) stays queued until the
            // whole agent loop finishes. The bug is user-visible as the latter:
            // the Pi CLI keeps showing the message in its queue.
            const steeredIntoActiveTurn = [];
            const leftQueuedAsFollowUp = [];
            const pi = {
              on(name, fn) {
                handlers[name] = fn;
              },
              registerCommand() {},
              sendUserMessage(content, options) {
                if (options && options.deliverAs === "steer") {
                  steeredIntoActiveTurn.push(content);
                } else {
                  leftQueuedAsFollowUp.push(content);
                }
              },
            };

            require(extensionPath)(pi);

            // A live turn: the ExtensionContext reports not-idle throughout.
            const turnCtx = {
              isIdle: () => false,
              abort() {},
            };

            try {
              await handlers.session_start({}, turnCtx);
              await handlers.agent_start({}, turnCtx);
              await handlers.turn_start({ turnIndex: 1 }, turnCtx);
              await waitForInboxEmpty();
              await sleep(50);

              assert(
                leftQueuedAsFollowUp.length === 0,
                'Send now message was queued as a Pi follow-up (deliverAs ' +
                  '"followUp") instead of steered into the active turn: ' +
                  JSON.stringify(leftQueuedAsFollowUp),
              );
              assert(
                steeredIntoActiveTurn.some((text) =>
                  String(text).includes("steer me into the active turn"),
                ),
                "Send now message was never delivered to the active Pi turn",
              );
            } finally {
              if (pi.__omnigentInboxPoller) clearInterval(pi.__omnigentInboxPoller);
            }
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "OMNIGENT_PI_NATIVE_CONFIG": str(config_path),
        "PI_NATIVE_EXTENSION_PATH": str(extension_path),
    }
    result = subprocess.run(
        ["node", str(script)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
