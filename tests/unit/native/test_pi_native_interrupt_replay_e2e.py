"""End-to-end tests for pi-native bridge interrupt replay semantics."""

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


def _prepare_bridge(tmp_path: Path) -> tuple[Path, Path, Path]:
    """
    Prepare a real pi-native bridge directory and generated extension files.

    This uses the same bridge helpers the runner uses for native Pi sessions, so
    the test covers the Python enqueue contract, generated config, extension
    loading, and the JS inbox poller together.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    extension_path, config_path = pi_native_bridge.write_extension_files(
        bridge_dir,
        session_id="conv_f18_e2e",
        server_url="",
        conversation_url="",
    )
    return bridge_dir, extension_path, config_path


def _run_extension_scenario(
    tmp_path: Path,
    *,
    extension_path: Path,
    config_path: Path,
    scenario: str,
) -> subprocess.CompletedProcess[str]:
    """
    Run a focused Node scenario against a generated pi-native extension file.

    The scenario script mocks only the Pi event bus/context. Interrupt delivery
    is not mocked: the Python side has already written an inbox payload via
    ``enqueue_interrupt()``, and the real extension poller consumes it.
    """
    script = tmp_path / f"{scenario}.mjs"
    script.write_text(
        textwrap.dedent(
            r"""
            import { createRequire } from "module";
            import fs from "fs";

            const require = createRequire(import.meta.url);
            const extensionPath = process.env.PI_NATIVE_EXTENSION_PATH;
            const configPath = process.env.OMNIGENT_PI_NATIVE_CONFIG;
            const scenario = process.env.PI_NATIVE_INTERRUPT_SCENARIO;
            const config = JSON.parse(fs.readFileSync(configPath, "utf8"));

            function assert(cond, message) {
              if (!cond) throw new Error(message);
            }

            function makeCtx(idle) {
              const ctx = {
                abortCount: 0,
                abort() {
                  this.abortCount += 1;
                },
              };
              if (idle !== undefined) ctx.isIdle = () => idle;
              return ctx;
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
            const pi = {
              on(name, fn) {
                handlers[name] = fn;
              },
              registerCommand() {},
              sendUserMessage() {},
            };

            require(extensionPath)(pi);

            try {
              if (scenario === "idle_interrupt_then_next_turn") {
                const idleCtx = makeCtx(true);
                await handlers.session_start({}, idleCtx);
                await waitForInboxEmpty();
                assert(
                  idleCtx.abortCount === 0,
                  `idle interrupt aborted idle ctx ${idleCtx.abortCount} time(s)`,
                );

                const nextCtx = makeCtx(false);
                await handlers.agent_start({}, nextCtx);
                await handlers.turn_start({ turnIndex: 1 }, nextCtx);
                const toolResult = await handlers.tool_call(
                  { toolCallId: "t1", toolName: "do_thing", input: {} },
                  nextCtx,
                );
                assert(
                  nextCtx.abortCount === 0,
                  `idle interrupt poisoned next turn with ${nextCtx.abortCount} abort(s)`,
                );
                assert(
                  !toolResult || toolResult.block !== true,
                  `idle interrupt blocked next turn tool_call: ${JSON.stringify(toolResult)}`,
                );
              } else if (scenario === "agent_loop_interrupt_without_is_idle") {
                const turnCtx = makeCtx(undefined);
                await handlers.session_start({}, turnCtx);
                await handlers.agent_start({}, turnCtx);
                await waitForInboxEmpty();
                assert(
                  turnCtx.abortCount >= 1,
                  `agent-loop interrupt did not abort without isIdle (${turnCtx.abortCount})`,
                );

                await handlers.turn_start({ turnIndex: 1 }, turnCtx);
                const toolResult = await handlers.tool_call(
                  { toolCallId: "t1", toolName: "do_thing", input: {} },
                  turnCtx,
                );
                assert(
                  toolResult && toolResult.block === true,
                  `agent-loop interrupt did not replay/block: ${JSON.stringify(toolResult)}`,
                );
              } else if (scenario === "mid_turn_interrupt_replays") {
                const turnCtx = makeCtx(false);
                await handlers.session_start({}, turnCtx);
                await handlers.agent_start({}, turnCtx);
                await handlers.turn_start({ turnIndex: 1 }, turnCtx);
                await waitForInboxEmpty();
                assert(
                  turnCtx.abortCount >= 1,
                  `mid-turn interrupt did not abort live turn (${turnCtx.abortCount})`,
                );

                const toolResult = await handlers.tool_call(
                  { toolCallId: "t1", toolName: "do_thing", input: {} },
                  turnCtx,
                );
                assert(
                  toolResult && toolResult.block === true,
                  `mid-turn interrupt did not replay/block: ${JSON.stringify(toolResult)}`,
                );
              } else {
                throw new Error(`unknown scenario: ${scenario}`);
              }
            } finally {
              if (pi.__omnigentInboxPoller) clearInterval(pi.__omnigentInboxPoller);
            }
            """
        ),
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "OMNIGENT_PI_NATIVE_CONFIG": str(config_path),
        "PI_NATIVE_EXTENSION_PATH": str(extension_path),
        "PI_NATIVE_INTERRUPT_SCENARIO": scenario,
    }
    return subprocess.run(
        ["node", str(script)],
        cwd=Path(__file__).resolve().parents[3],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def test_python_bridge_idle_interrupt_does_not_poison_next_turn(tmp_path: Path) -> None:
    """
    F18 end-to-end regression: Python bridge interrupt -> JS poller -> next turn.

    The interrupt is queued through ``pi_native_bridge.enqueue_interrupt()``
    before the extension starts, matching the runner's on-disk contract. The
    extension consumes it while the mocked Pi context reports idle. The next
    legitimate turn begins within the original 30 second replay window and must
    not be aborted or have its tool call blocked.
    """
    bridge_dir, extension_path, config_path = _prepare_bridge(tmp_path)
    pi_native_bridge.enqueue_interrupt(bridge_dir)

    result = _run_extension_scenario(
        tmp_path,
        extension_path=extension_path,
        config_path=config_path,
        scenario="idle_interrupt_then_next_turn",
    )

    assert result.returncode == 0, result.stderr


def test_python_bridge_mid_turn_interrupt_still_replays(tmp_path: Path) -> None:
    """
    End-to-end regression guard for legitimate mid-turn interrupts.

    The same Python bridge enqueue path must still abort an already-started Pi
    turn and keep replaying within the window so an in-flight tool call is
    blocked with ``Interrupted by user``.
    """
    bridge_dir, extension_path, config_path = _prepare_bridge(tmp_path)
    pi_native_bridge.enqueue_interrupt(bridge_dir)

    result = _run_extension_scenario(
        tmp_path,
        extension_path=extension_path,
        config_path=config_path,
        scenario="mid_turn_interrupt_replays",
    )

    assert result.returncode == 0, result.stderr


def test_python_bridge_agent_loop_interrupt_without_is_idle_still_replays(
    tmp_path: Path,
) -> None:
    """
    End-to-end guard for older SDKs that lack ``ExtensionContext.isIdle()``.

    If an interrupt lands after ``agent_start`` but before ``turn_start``, the
    extension has no ``activeResponseId`` yet. The no-``isIdle`` fallback must
    still treat this as a live agent loop so the interrupt aborts/replays instead
    of being dropped.
    """
    bridge_dir, extension_path, config_path = _prepare_bridge(tmp_path)
    pi_native_bridge.enqueue_interrupt(bridge_dir)

    result = _run_extension_scenario(
        tmp_path,
        extension_path=extension_path,
        config_path=config_path,
        scenario="agent_loop_interrupt_without_is_idle",
    )

    assert result.returncode == 0, result.stderr
