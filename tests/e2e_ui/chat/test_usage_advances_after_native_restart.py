"""Per-model token usage keeps advancing after the pi-native Pi process relaunches,
even though the relaunched process restarts its cumulative counters at 0."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.chat.test_session_usage_loading import _usage_panel

_MODEL = "claude-opus-4"
_EXTENSION_PATH = (
    Path(__file__).resolve().parents[3]
    / "omnigent"
    / "resources"
    / "pi_native"
    / "omnigent_pi_native_extension.js"
)

# One node process per launch: a fresh process is a relaunched Pi whose
# in-memory cumulative counters start back at 0. ``onlyRestore`` models an idle
# resume that fires session_start but no new turn.
_LAUNCH_SCRIPT = r"""
const extensionPath = process.argv[1];
const configPath = process.argv[2];
const turn = JSON.parse(process.argv[3]);

process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;
// The inbox poller's interval would otherwise keep the process alive.
global.setInterval = () => ({ fakeInterval: true });

const handlers = {};
const pi = {
  registerCommand() {},
  setThinkingLevel() {},
  on(name, handler) {
    handlers[name] = handler;
  },
};
require(extensionPath)(pi);

const ctx = {
  sessionManager: { getSessionId: () => "native-session-1" },
  ui: { setTitle() {}, setStatus() {}, notify() {} },
};

(async () => {
  if (handlers.session_start) await handlers.session_start({}, ctx);
  if (!turn.onlyRestore) {
    await handlers.message_end(
      {
        message: {
          role: "assistant",
          model: turn.model,
          timestamp: turn.timestamp,
          usage: {
            input: turn.input,
            output: turn.output,
            cacheRead: 0,
            cacheWrite: 0,
            totalTokens: turn.input + turn.output,
          },
        },
      },
      ctx,
    );
  }
  process.exit(0);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});"""


def _write_config(tmp_path: Path, base_url: str, session_id: str) -> tuple[Path, Path]:
    bridge_dir = tmp_path / "bridge"
    inbox_dir = tmp_path / "inbox"
    bridge_dir.mkdir()
    inbox_dir.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "serverUrl": base_url,
                "sessionId": session_id,
                "inboxDir": str(inbox_dir),
                "bridgeDir": str(bridge_dir),
                "authHeaders": {},
            }
        )
    )
    return config_path, bridge_dir


def _launch_extension(
    node: str,
    config_path: Path,
    *,
    model: str | None = None,
    timestamp: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    only_restore: bool = False,
) -> None:
    turn = {
        "model": model,
        "timestamp": timestamp,
        "input": input_tokens,
        "output": output_tokens,
        "onlyRestore": only_restore,
    }
    result = subprocess.run(
        [node, "-e", _LAUNCH_SCRIPT, str(_EXTENSION_PATH), str(config_path), json.dumps(turn)],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _wait_for_model_input(base_url: str, session_id: str, model: str, expected: int) -> None:
    deadline = time.monotonic() + 15
    seen: object = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(
                f"{base_url}/v1/sessions/{session_id}",
                params={"include_usage": "true"},
                timeout=10,
            )
        except httpx.HTTPError:
            # A transient transport error while the server settles is "not yet".
            time.sleep(0.25)
            continue
        if response.status_code != 200:
            # A transient non-2xx while the server settles is "not yet", not fatal.
            time.sleep(0.25)
            continue
        by_model = response.json().get("usage_by_model") or {}
        seen = by_model.get(model)
        if isinstance(seen, dict) and seen.get("input_tokens") == expected:
            return
        time.sleep(0.25)
    raise AssertionError(f"server never reported input_tokens={expected} for {model}: {seen!r}")


@pytest.mark.min_server_version("0.15.0")
def test_usage_display_advances_after_native_restart(
    request: pytest.FixtureRequest,
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to launch the real pi-native extension")
    base_url, session_id = seeded_session

    config_path, _bridge_dir = _write_config(tmp_path, base_url, session_id)

    _launch_extension(
        node, config_path, model=_MODEL, timestamp=1000, input_tokens=150_000, output_tokens=30_000
    )
    _wait_for_model_input(base_url, session_id, _MODEL, 150_000)

    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}", wait_until="domcontentloaded")
    expect(page.get_by_placeholder("Send a message…")).to_be_editable(timeout=30_000)
    panel = _usage_panel(page)
    panel.get_by_test_id("agent-info-usage-by-model").locator("summary").press("Enter")
    model_row = panel.get_by_test_id(f"agent-info-model-{_MODEL}")
    expect(model_row).to_contain_text("150K")
    expect(model_row).to_contain_text("30K")

    # Pi relaunches over the shared bridge dir; the next turn is a small
    # post-relaunch report that lands below the server's stored peak.
    _launch_extension(
        node, config_path, model=_MODEL, timestamp=2000, input_tokens=900, output_tokens=250
    )
    # Wait for the server to record the advanced total before reloading, so the
    # UI assertion observes the grown peak rather than racing the flush.
    _wait_for_model_input(base_url, session_id, _MODEL, 150_900)

    page.reload(wait_until="domcontentloaded")
    expect(page.get_by_placeholder("Send a message…")).to_be_editable(timeout=30_000)
    panel = _usage_panel(page)
    panel.get_by_test_id("agent-info-usage-by-model").locator("summary").press("Enter")
    model_row = panel.get_by_test_id(f"agent-info-model-{_MODEL}")
    # The row must grow past the pre-relaunch total rather than stay clamped at it.
    expect(model_row).to_contain_text("150.9K", timeout=8_000)
    expect(model_row).to_contain_text("30.3K")


@pytest.mark.min_server_version("0.15.0")
def test_restart_reasserts_persisted_baseline_server_never_recorded(
    seeded_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """An idle resume re-posts the persisted baseline, so a failed pre-exit flush
    does not leave the server undercounted forever."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to launch the real pi-native extension")
    base_url, session_id = seeded_session

    config_path, bridge_dir = _write_config(tmp_path, base_url, session_id)

    _launch_extension(
        node, config_path, model=_MODEL, timestamp=1000, input_tokens=150_000, output_tokens=30_000
    )
    _wait_for_model_input(base_url, session_id, _MODEL, 150_000)

    # postSessionUsage persists the running total before its best-effort POST, so
    # a flush that failed can leave the on-disk baseline ahead of the server.
    # Bump the persisted file above the server's recorded peak to model that.
    state = bridge_dir / "cumulative_usage.json"
    saved = json.loads(state.read_text())
    saved["cumulative_input_tokens"] = 160_000
    saved["cumulative_output_tokens"] = 32_000
    state.write_text(json.dumps(saved))

    # An idle resume (session_start only, no new turn) must still re-assert the
    # persisted baseline rather than suppress it as an already-posted total.
    _launch_extension(node, config_path, only_restore=True)
    _wait_for_model_input(base_url, session_id, _MODEL, 160_000)

    # The restore raises input and output together, so a regression that
    # re-asserted input while dropping output would pass the check above.
    response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}",
        params={"include_usage": "true"},
        timeout=10,
    )
    response.raise_for_status()
    model_usage = (response.json().get("usage_by_model") or {}).get(_MODEL)
    assert model_usage is not None, f"server reported no usage for {_MODEL}"
    assert model_usage["output_tokens"] == 32_000
