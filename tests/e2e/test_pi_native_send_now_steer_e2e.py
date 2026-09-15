"""pi-native: web-UI "Send now" leaves a follow-up queued instead of steering.

The bug
-------
In a Pi native session, a user sends a prompt that keeps the agent working,
then queues a follow-up while it works, then clicks the queued row's
**Send now** action. The web chat view reports the immediate send as
successful, but the Pi CLI still shows the message *queued*: it was never
steered into the active turn.

The journey (all user-observable)
---------------------------------
1. Open a Pi native session and send a prompt that keeps the agent working.
2. While the agent is still working, submit another message so it appears in
   the queued-message row.
3. Click the row's **Send now** action.
4. The chat view visually treats the action as successful.
5. Switch to the session's CLI view.
6. The follow-up is still queued -- it was not steered into the active turn.

The mechanism (root-cause lead, NOT the journey)
-------------------------------------------------
The web "Send now" (steer) POST reaches the runner's
``PiNativeExecutor.enqueue_session_message``, which writes a
``{"type": "user_message", ...}`` payload into the bridge inbox via
``enqueue_user_message`` -- the *same* payload a normal turn writes, carrying
no "steer" intent. The resident Pi extension's inbox poller then delivers it
with a hard-coded ``pi.sendUserMessage(content, { deliverAs: "followUp" })``
(``omnigent/resources/pi_native/omnigent_pi_native_extension.js``). In Pi
0.84.2, ``deliverAs: "followUp"`` while a turn is streaming enqueues the
message onto the follow-up queue (delivered only *after* the turn finishes),
whereas ``deliverAs: "steer"`` injects it into the active turn. So a "Send
now" click is silently downgraded to a follow-up.

What this test drives (real path, real code)
---------------------------------------------
It exercises the genuine delivery path end to end with no product source
faked: the REAL bridge writer (``enqueue_user_message``, exactly what the web
"Send now" POST triggers in the runner) writes the inbox payload, the REAL
generated extension JS polls and delivers it, and the message is routed
through a REAL Pi ``AgentSession`` whose steer/follow-up queues are Pi's own.
A turn-in-flight is simulated by marking the session's active-run flag --
the only precondition the bug needs (an active turn when "Send now" is
clicked) that does not require a live model -- and the driver's extension
context exposes ``isIdle()`` wired to that same flag, exactly as Pi's real
ExtensionContext does (``isIdle: () => this.isIdle`` over
``!this._isAgentRunActive``).

The fail -> pass contract
-------------------------
Durable regression contract: a mid-turn "Send now" message must be **steered
into the active turn** -- it lands in Pi's steering queue and is delivered
with ``deliverAs: "steer"`` -- never left sitting in the follow-up queue.

- On the buggy build the extension delivers ``deliverAs: "followUp"``: the
  message lands in ``getFollowUpMessages()`` and ``getSteeringMessages()`` is
  empty -> this test FAILS.
- After a fix that delivers mid-turn messages with ``deliverAs: "steer"``,
  the message lands in ``getSteeringMessages()`` -> this test PASSES.

Usage::

    python -m pytest tests/e2e/test_pi_native_send_now_steer_e2e.py -v
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from omnigent.harnesses.pi_native.bridge import enqueue_user_message
from tests.e2e._harness_probes import cli_unavailable_reason

_REPO_ROOT = Path(__file__).resolve().parents[2]

_EXTENSION_PATH = (
    _REPO_ROOT / "omnigent" / "resources" / "pi_native" / "omnigent_pi_native_extension.js"
)

# The distinctive "Send now" message content, asserted through the queues.
_SEND_NOW_TEXT = "PLEASE-FOCUS-ON-THE-TESTS-NOW"

# Node driver: load the REAL extension, wire pi.sendUserMessage to a REAL Pi
# AgentSession with a simulated active turn, run the inbox poller once, and
# report which of Pi's own queues the message landed in.
#
# argv: [node, driver, extensionPath, piIndexPath, configPath]
_NODE_DRIVER = r"""
"use strict";
const extensionPath = process.argv[2];
const piIndexPath = process.argv[3];
const configPath = process.argv[4];

process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

// The extension posts transcript/status events via fetch; stub it so
// session_start's bookkeeping POSTs succeed without a real server.
global.fetch = async () => ({
  ok: true,
  status: 200,
  async json() { return {}; },
  async text() { return ""; },
});

// Capture the inbox poller instead of running it on a timer.
let pollInbox = null;
global.setInterval = (fn) => { pollInbox = fn; return { fake: true }; };
global.clearInterval = () => {};

(async () => {
  const { createAgentSession, SessionManager } = await import(piIndexPath);

  // A REAL Pi AgentSession -- its steering/follow-up queues are Pi's own.
  const { session } = await createAgentSession({
    sessionManager: SessionManager.inMemory ? SessionManager.inMemory() : undefined,
    noTools: true,
  });

  // Simulate a turn in flight: the user sent a prompt and the agent is
  // actively streaming when the follow-up's "Send now" arrives. This is the
  // only precondition the bug needs; it decides steer-vs-follow-up routing.
  session._isAgentRunActive = true;

  // Mirror Pi's real ExtensionContext idle signal, which is wired to the
  // same active-run flag (agent-session: `isIdle: () => this.isIdle` over
  // `!this._isAgentRunActive`). Extensions consult it to route deliveries.
  const isIdle = () => session.isIdle;

  const recorded = [];
  const pending = [];
  const pi = {
    _handlers: {},
    registerCommand() {},
    on(name, handler) { this._handlers[name] = handler; },
    setThinkingLevel() {},
    // Route exactly through the real AgentSession, as the harness does.
    sendUserMessage(content, options) {
      recorded.push({ content, options });
      pending.push(
        Promise.resolve(session.sendUserMessage(content, options)).catch(
          (e) => ({ err: String(e) }),
        ),
      );
    },
  };

  require(extensionPath)(pi);

  const ctx = {
    sessionManager: { getSessionId: () => "native-session-1" },
    ui: { setTitle() {}, setStatus() {}, notify() {} },
    model: undefined,
    modelRegistry: undefined,
    isIdle,
  };
  await pi._handlers.session_start({}, ctx);

  if (typeof pollInbox !== "function") {
    console.error("inbox poller was never started");
    process.exit(3);
  }
  pollInbox();
  await Promise.all(pending);
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));

  console.log(
    JSON.stringify({
      recorded,
      steering: session.getSteeringMessages(),
      followUp: session.getFollowUpMessages(),
    }),
  );
})().catch((e) => {
  console.error(e && e.stack ? e.stack : e);
  process.exit(2);
});
"""


def _resolve_pi_index() -> Path | None:
    """Locate the installed pi-coding-agent package's ``dist/index.js``.

    Walks up from the ``pi`` executable's realpath (``OMNIGENT_PI_PATH`` or
    ``PATH``) to the ``@earendil-works/pi-coding-agent`` package root.

    :returns: Path to ``dist/index.js``, or ``None`` when it can't be found.
    """
    import os

    pi_bin = os.environ.get("OMNIGENT_PI_PATH", "").strip() or shutil.which("pi")
    if not pi_bin:
        return None
    real = Path(pi_bin).resolve()
    for parent in [real, *real.parents]:
        pkg_json = parent / "package.json"
        if pkg_json.is_file():
            try:
                name = json.loads(pkg_json.read_text()).get("name")
            except (OSError, ValueError):
                name = None
            if name == "@earendil-works/pi-coding-agent":
                index = parent / "dist" / "index.js"
                return index if index.is_file() else None
    return None


@pytest.mark.timeout(120)
def test_pi_native_send_now_steers_into_active_turn(tmp_path: Path) -> None:
    """A mid-turn "Send now" must steer the active turn, not queue a follow-up.

    Drives the real bridge writer + real generated extension + a real Pi
    ``AgentSession`` (active turn simulated) and asserts the "Send now" message
    was steered (steering queue, ``deliverAs: "steer"``) rather than left
    queued as a follow-up. Fails on the current build (delivered as
    ``followUp``); passes once "Send now" is steered.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the pi-native extension e2e test")

    reason = cli_unavailable_reason("pi")
    if reason is not None:
        pytest.skip(f"pi-native send-now steering journey requires a runnable 'pi' CLI; {reason}.")

    pi_index = _resolve_pi_index()
    if pi_index is None:
        pytest.skip(
            "could not locate the @earendil-works/pi-coding-agent package "
            "(dist/index.js) from the 'pi' executable"
        )

    assert _EXTENSION_PATH.is_file(), f"generated extension not found at {_EXTENSION_PATH}"

    # Bridge dir + inbox, exactly as the runner lays it out per conversation.
    bridge_dir = tmp_path / "bridge"
    inbox_dir = bridge_dir / "inbox"
    inbox_dir.mkdir(parents=True)

    config_path = bridge_dir / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "serverUrl": "http://omnigent.test",
                "sessionId": "conv_send_now_steer",
                "inboxDir": str(inbox_dir),
                "authHeaders": {"authorization": "Bearer test"},
            }
        ),
        encoding="utf-8",
    )

    # THE "Send now" action: the runner enqueues the queued follow-up into the
    # bridge inbox through the very same writer the web steer POST triggers.
    message_id = enqueue_user_message(bridge_dir, _SEND_NOW_TEXT)
    assert list(inbox_dir.glob("*.json")), "bridge writer did not enqueue an inbox payload"

    driver_path = tmp_path / "driver.cjs"
    driver_path.write_text(_NODE_DRIVER, encoding="utf-8")

    result = subprocess.run(
        [node, str(driver_path), str(_EXTENSION_PATH), str(pi_index), str(config_path)],
        capture_output=True,
        check=False,
        text=True,
        cwd=str(tmp_path),
        timeout=90,
    )
    assert result.returncode == 0, (
        f"node driver failed (exit {result.returncode}).\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    last_line = result.stdout.strip().splitlines()[-1]
    outcome = json.loads(last_line)
    steering = outcome["steering"]
    follow_up = outcome["followUp"]
    recorded = outcome["recorded"]
    deliver_as = [entry.get("options", {}).get("deliverAs") for entry in recorded]

    assert steering == [_SEND_NOW_TEXT] and follow_up == [], (
        "A mid-turn 'Send now' message must be STEERED into the active Pi turn "
        "(land in the steering queue), but the generated extension delivered it "
        f"as a follow-up. Pi steering queue = {steering!r}; follow-up queue = "
        f"{follow_up!r}; extension delivered with deliverAs = {deliver_as!r} "
        f"(message id {message_id!r}). The web 'Send now' therefore reports "
        "success while Pi keeps the message queued until the whole turn ends, "
        "instead of steering it into the active turn. Mid-turn deliveries "
        "must use deliverAs 'steer'."
    )
    assert deliver_as == ["steer"], (
        "The 'Send now' message must be delivered to Pi with deliverAs 'steer' "
        f"to reach the active turn, but the extension used {deliver_as!r}."
    )
