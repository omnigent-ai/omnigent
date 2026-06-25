"""End-to-end tests for the generated pi-native bridge extension."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_delivery_cap_drops_followup_without_failed_session_status(
    tmp_path: Path,
) -> None:
    """The extension must not terminal-fail a session when follow-up delivery caps.

    This runs the real JavaScript extension under Node with a real inbox payload
    and mocked Pi/fetch boundaries. Five consecutive ``sendUserMessage`` throws
    should consume the inbox file and emit an informational conversation item,
    never ``external_session_status`` with ``status: "failed"``.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the pi-native extension e2e test")

    extension_path = (
        Path(__file__).resolve().parents[1]
        / "omnigent"
        / "resources"
        / "pi_native"
        / "omnigent_pi_native_extension.js"
    )

    script = r"""
const assert = require("assert").strict;
const fs = require("fs");
const path = require("path");

const extensionPath = process.argv[1];
const tmpDir = process.argv[2];
const inboxDir = path.join(tmpDir, "inbox");
const payloadPath = path.join(inboxDir, "000-msg.json");
const configPath = path.join(tmpDir, "config.json");

fs.mkdirSync(inboxDir, { recursive: true });
fs.writeFileSync(
  payloadPath,
  JSON.stringify({ id: "msg-1", type: "user_message", content: "follow up" }),
);
fs.writeFileSync(
  configPath,
  JSON.stringify({
    serverUrl: "http://omnigent.test",
    sessionId: "session-1",
    inboxDir,
    authHeaders: { authorization: "Bearer test" },
  }),
);

process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

const postedEvents = [];
global.fetch = async (_url, request) => {
  postedEvents.push(JSON.parse(request.body));
  return { ok: true };
};

let pollInbox = null;
global.setInterval = (fn, _ms) => {
  pollInbox = fn;
  return { fakeInterval: true };
};

const handlers = {};
const sendAttempts = [];
const pi = {
  registerCommand() {},
  on(eventName, handler) {
    handlers[eventName] = handler;
  },
  sendUserMessage(content, options) {
    sendAttempts.push({ content, options });
    throw new Error("Pi is not ready");
  },
};

require(extensionPath)(pi);

(async () => {
  assert.equal(typeof handlers.session_start, "function");
  await handlers.session_start({}, {
    sessionManager: { getSessionId: () => "native-session-1" },
    ui: { setTitle() {}, setStatus() {}, notify() {} },
  });
  assert.equal(typeof pollInbox, "function");

  for (let attempt = 0; attempt < 5; attempt += 1) {
    pollInbox();
  }
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(
    sendAttempts,
    Array.from({ length: 5 }, () => ({
      content: "follow up",
      options: { deliverAs: "followUp" },
    })),
  );
  assert.equal(fs.existsSync(payloadPath), false);
  assert.equal(
    postedEvents.some(
      (event) =>
        event.type === "external_session_status" &&
        event.data &&
        event.data.status === "failed",
    ),
    false,
    JSON.stringify(postedEvents),
  );

  const dropNote = postedEvents.find(
    (event) =>
      event.type === "external_conversation_item" &&
      event.data &&
      event.data.item_type === "error" &&
      event.data.item_data &&
      event.data.item_data.code === "pi_followup_delivery_dropped",
  );
  assert.ok(dropNote, JSON.stringify(postedEvents));
  assert.equal(dropNote.data.item_data.source, "execution");
  assert.match(dropNote.data.response_id, /^pi-deliver-dropped-/);
  // The note must be actionable: include the dropped message id and a preview
  // of its content so an operator can identify what was lost.
  assert.match(dropNote.data.item_data.message, /msg-1/);
  assert.match(dropNote.data.item_data.message, /follow up/);
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""

    result = subprocess.run(
        [node, "-e", script, str(extension_path), str(tmp_path)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_compact_payload_triggers_ctx_compact_and_brackets_spinner(
    tmp_path: Path,
) -> None:
    """A ``compact`` inbox payload calls ``ctx.compact()`` and brackets the spinner.

    Runs the real JavaScript extension under Node. A queued ``compact`` payload
    must (1) call the resident ``ExtensionContext.compact()`` with the custom
    instructions and ``onComplete``/``onError`` callbacks, (2) post
    ``external_compaction_status`` ``in_progress`` BEFORE compact() so the web UI
    spinner is bracketed, and (3) post ``completed`` once Pi invokes
    ``onComplete``. The payload file must be consumed (unlinked) so compaction
    is not re-triggered every poll tick.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the pi-native extension e2e test")

    extension_path = (
        Path(__file__).resolve().parents[1]
        / "omnigent"
        / "resources"
        / "pi_native"
        / "omnigent_pi_native_extension.js"
    )

    script = r"""
const assert = require("assert").strict;
const fs = require("fs");
const path = require("path");

const extensionPath = process.argv[1];
const tmpDir = process.argv[2];
const inboxDir = path.join(tmpDir, "inbox");
const payloadPath = path.join(inboxDir, "000-compact.json");
const configPath = path.join(tmpDir, "config.json");

fs.mkdirSync(inboxDir, { recursive: true });
fs.writeFileSync(
  payloadPath,
  JSON.stringify({
    id: "compact-1",
    type: "compact",
    custom_instructions: "focus on the refactor",
  }),
);
fs.writeFileSync(
  configPath,
  JSON.stringify({
    serverUrl: "http://omnigent.test",
    sessionId: "session-1",
    inboxDir,
    authHeaders: { authorization: "Bearer test" },
  }),
);

process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

const postedEvents = [];
global.fetch = async (_url, request) => {
  postedEvents.push(JSON.parse(request.body));
  return { ok: true };
};

let pollInbox = null;
global.setInterval = (fn, _ms) => {
  pollInbox = fn;
  return { fakeInterval: true };
};

const handlers = {};
const compactCalls = [];
const pi = {
  registerCommand() {},
  on(eventName, handler) {
    handlers[eventName] = handler;
  },
  sendUserMessage() {},
};

require(extensionPath)(pi);

// The resident context the poller compacts. compact() is fire-and-forget; we
// simulate Pi finishing successfully by invoking the onComplete callback.
const ctx = {
  sessionManager: { getSessionId: () => "native-session-1" },
  ui: { setTitle() {}, setStatus() {}, notify() {} },
  abort() {},
  isIdle: () => false,
  compact(options) {
    compactCalls.push(options);
    if (options && typeof options.onComplete === "function") {
      options.onComplete({ summary: "done" });
    }
  },
};

(async () => {
  // session_start remembers ctx and starts the inbox poller.
  await handlers.session_start({}, ctx);
  assert.equal(typeof pollInbox, "function");

  pollInbox();
  await new Promise((resolve) => setImmediate(resolve));

  // 1) ctx.compact() was called exactly once with the custom instructions and
  // both lifecycle callbacks.
  assert.equal(compactCalls.length, 1, JSON.stringify(compactCalls));
  assert.equal(compactCalls[0].customInstructions, "focus on the refactor");
  assert.equal(typeof compactCalls[0].onComplete, "function");
  assert.equal(typeof compactCalls[0].onError, "function");

  // 2/3) The spinner is bracketed: in_progress posted before compact(), then
  // completed from onComplete. No failed edge on the happy path.
  const compactionStatuses = postedEvents
    .filter((event) => event.type === "external_compaction_status")
    .map((event) => event.data && event.data.status);
  assert.deepEqual(
    compactionStatuses,
    ["in_progress", "completed"],
    JSON.stringify(postedEvents),
  );

  // The payload file is consumed so compaction is not re-triggered each tick.
  assert.equal(fs.existsSync(payloadPath), false);

  // A second poll tick must NOT re-trigger compaction (file gone + deduped).
  pollInbox();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(compactCalls.length, 1, "compaction must not re-trigger");
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""

    result = subprocess.run(
        [node, "-e", script, str(extension_path), str(tmp_path)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_compact_payload_failure_dismisses_spinner(tmp_path: Path) -> None:
    """When ``ctx.compact()`` reports an error, the extension posts ``failed``.

    Pi's ``compact()`` surfaces failures through the ``onError`` callback. The
    extension must publish ``external_compaction_status`` ``failed`` so the web
    UI's "Compacting…" spinner is dismissed rather than stranded.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the pi-native extension e2e test")

    extension_path = (
        Path(__file__).resolve().parents[1]
        / "omnigent"
        / "resources"
        / "pi_native"
        / "omnigent_pi_native_extension.js"
    )

    script = r"""
const assert = require("assert").strict;
const fs = require("fs");
const path = require("path");

const extensionPath = process.argv[1];
const tmpDir = process.argv[2];
const inboxDir = path.join(tmpDir, "inbox");
const payloadPath = path.join(inboxDir, "000-compact.json");
const configPath = path.join(tmpDir, "config.json");

fs.mkdirSync(inboxDir, { recursive: true });
fs.writeFileSync(
  payloadPath,
  JSON.stringify({ id: "compact-1", type: "compact" }),
);
fs.writeFileSync(
  configPath,
  JSON.stringify({
    serverUrl: "http://omnigent.test",
    sessionId: "session-1",
    inboxDir,
  }),
);

process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

const postedEvents = [];
global.fetch = async (_url, request) => {
  postedEvents.push(JSON.parse(request.body));
  return { ok: true };
};

let pollInbox = null;
global.setInterval = (fn, _ms) => {
  pollInbox = fn;
  return { fakeInterval: true };
};

const handlers = {};
const pi = {
  registerCommand() {},
  on(eventName, handler) {
    handlers[eventName] = handler;
  },
  sendUserMessage() {},
};

require(extensionPath)(pi);

const ctx = {
  sessionManager: { getSessionId: () => "native-session-1" },
  ui: { setTitle() {}, setStatus() {}, notify() {} },
  abort() {},
  isIdle: () => false,
  compact(options) {
    if (options && typeof options.onError === "function") {
      options.onError(new Error("compaction blew up"));
    }
  },
};

(async () => {
  await handlers.session_start({}, ctx);
  pollInbox();
  await new Promise((resolve) => setImmediate(resolve));

  const compactionStatuses = postedEvents
    .filter((event) => event.type === "external_compaction_status")
    .map((event) => event.data && event.data.status);
  // in_progress raised the spinner; failed (from onError) dismisses it.
  // completed must never fire on an errored compaction.
  assert.deepEqual(
    compactionStatuses,
    ["in_progress", "failed"],
    JSON.stringify(postedEvents),
  );
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
"""

    result = subprocess.run(
        [node, "-e", script, str(extension_path), str(tmp_path)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
