// Unit test for the pi-native bridge extension's interrupt / replay logic.
//
// Regression coverage for F18 (SDK_INTEGRATION_BUG_AUDIT.md): an interrupt that
// arrives while Pi is idle used to arm a 30s replay window that aborted the next
// legitimately-started turn. ExtensionContext.abort() is a silent no-op when the
// agent is idle (it does not throw), so the old requestInterrupt() armed the
// window unconditionally and replayPendingInterrupt() then killed the next turn.
//
// This test drives the real extension through its public surface: it registers
// the event handlers with a mock `pi`, and delivers interrupts through the real
// inbox poller (a temp inbox directory). No network is used (postEvent fails
// closed when config has no serverUrl).
//
// Run with: node omnigent/resources/pi_native/omnigent_pi_native_extension.test.js
//
// Manual reproduction of the original bug (for context):
//   1. Start a native Pi session linked to Omnigent and let it go idle.
//   2. Hit "stop"/interrupt while no turn is running (between turns).
//   3. Send a fresh user message within 30 seconds.
//   Before the fix: the fresh turn is aborted immediately at agent_start /
//   turn_start (and tool calls are blocked) before producing output. After the
//   fix: the idle interrupt is dropped and the fresh turn runs normally.

const fs = require("fs");
const os = require("os");
const path = require("path");

const EXT_PATH = path.resolve(__dirname, "omnigent_pi_native_extension.js");

const harnesses = [];

// Build a fresh extension instance with its own temp inbox directory. Each call
// produces independent closure state (activeResponseId, pendingInterruptUntil,
// latestContext, ...).
function makeHarness({ captureEvents = false } = {}) {
  const inboxDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-native-inbox-"));
  const configPath = path.join(inboxDir, "config.json");
  // A serverUrl + sessionId make postEvent attempt a real fetch; with a mock
  // global fetch that lets a test capture the posted event bodies. Without
  // them postEvent fails closed (the interrupt tests rely on that).
  const config = captureEvents
    ? { inboxDir, serverUrl: "http://mock", sessionId: "conv_test" }
    : { inboxDir };
  fs.writeFileSync(configPath, JSON.stringify(config));
  process.env.OMNIGENT_PI_NATIVE_CONFIG = configPath;

  // Capture posted event bodies (status edges etc.) instead of hitting network.
  const postedEvents = [];
  if (captureEvents) {
    global.fetch = async (url, opts) => {
      try {
        if (opts && typeof opts.body === "string" && String(url).endsWith("/events")) {
          postedEvents.push(JSON.parse(opts.body));
        }
      } catch (_err) {}
      return { ok: true, status: 204, json: async () => ({}) };
    };
  }

  const handlers = {};
  const pi = {
    on: (name, fn) => {
      handlers[name] = fn;
    },
    registerCommand: () => {},
    sendUserMessage: () => {},
  };

  // Fresh module-function invocation -> fresh closures.
  delete require.cache[EXT_PATH];
  const mod = require(EXT_PATH);
  mod(pi);

  const h = { pi, handlers, inboxDir, postedEvents };
  harnesses.push(h);
  return h;
}

function statusEdges(postedEvents) {
  return postedEvents
    .filter((e) => e && e.type === "external_session_status" && e.data)
    .map((e) => ({ status: e.data.status, responseId: e.data.response_id }));
}

// ctx mock. `idle` may be true/false (exposes isIdle()) or undefined (no isIdle
// method at all, exercising the activeResponseId fallback path).
function makeCtx({ idle } = {}) {
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

// Drop an interrupt into the inbox and wait until the poller has consumed it
// (the poller unlinks the file after invoking handleInterrupt -> requestInterrupt).
async function deliverInterrupt(h) {
  const file = path.join(h.inboxDir, `int-${Date.now()}-${Math.random().toString(36).slice(2)}.json`);
  fs.writeFileSync(file, JSON.stringify({ type: "interrupt" }));
  const deadline = Date.now() + 3000;
  while (fs.existsSync(file)) {
    if (Date.now() > deadline) throw new Error("interrupt file was not consumed by poller");
    await sleep(20);
  }
  // The poller runs requestInterrupt synchronously before unlinking, so by the
  // time the file is gone the interrupt has been processed.
}

function assert(name, cond, detail) {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? "  -- " + detail : ""}`);
  if (!cond) process.exitCode = 1;
}

async function testIdleInterruptDoesNotPoisonNextTurn() {
  const h = makeHarness();
  const idleCtx = makeCtx({ idle: true });
  await h.handlers.session_start({}, idleCtx);

  await deliverInterrupt(h);

  assert(
    "idle interrupt (isIdle) does not abort the idle context",
    idleCtx.abortCount === 0,
    `abortCount=${idleCtx.abortCount}`,
  );

  // A fresh, legitimate turn starts within the (old) 30s window.
  const turnCtx = makeCtx({ idle: false });
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t1", toolName: "do_thing", input: {} },
    turnCtx,
  );

  assert(
    "fresh turn after idle interrupt is NOT aborted",
    turnCtx.abortCount === 0,
    `abortCount=${turnCtx.abortCount}`,
  );
  assert(
    "fresh turn's tool_call is NOT blocked after idle interrupt",
    !toolResult || toolResult.block !== true,
    JSON.stringify(toolResult),
  );
}

async function testIdleInterruptFallbackNoIsIdle() {
  // No isIdle() on ctx -> requestInterrupt falls back to !activeResponseId.
  // Between turns activeResponseId is null, so this must behave as idle.
  const h = makeHarness();
  const idleCtx = makeCtx({}); // no isIdle method
  await h.handlers.session_start({}, idleCtx);

  await deliverInterrupt(h);

  assert(
    "idle interrupt (activeResponseId fallback) does not arm the window",
    idleCtx.abortCount === 0,
    `abortCount=${idleCtx.abortCount}`,
  );

  const turnCtx = makeCtx({}); // no isIdle method
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t1", toolName: "do_thing", input: {} },
    turnCtx,
  );

  assert(
    "fresh turn after fallback idle interrupt is NOT aborted",
    turnCtx.abortCount === 0,
    `abortCount=${turnCtx.abortCount}`,
  );
  assert(
    "fresh turn's tool_call is NOT blocked (fallback)",
    !toolResult || toolResult.block !== true,
    JSON.stringify(toolResult),
  );
}

async function testMidTurnInterruptStillAborts() {
  // Regression guard: a genuine mid-turn interrupt must still abort and replay.
  const h = makeHarness();
  const turnCtx = makeCtx({ idle: false });
  await h.handlers.session_start({}, turnCtx); // starts the inbox poller
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);

  await deliverInterrupt(h);

  assert(
    "mid-turn interrupt aborts the live turn",
    turnCtx.abortCount >= 1,
    `abortCount=${turnCtx.abortCount}`,
  );

  // Replay must keep aborting within the window and block in-flight tool calls.
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t1", toolName: "do_thing", input: {} },
    turnCtx,
  );
  assert(
    "mid-turn interrupt blocks subsequent tool_call (replay)",
    !!toolResult && toolResult.block === true,
    JSON.stringify(toolResult),
  );
}

async function testAgentLoopInterruptFallbackNoIsIdleBeforeTurnStart() {
  // No isIdle(), and an interrupt lands after agent_start but before
  // turn_start. Older SDKs without isIdle() still need to treat this as part of
  // the live agent loop, not as an idle interrupt to drop.
  const h = makeHarness();
  const turnCtx = makeCtx({}); // no isIdle method
  await h.handlers.session_start({}, turnCtx); // starts the inbox poller
  await h.handlers.agent_start({}, turnCtx);

  await deliverInterrupt(h);

  assert(
    "agent-loop interrupt aborts before turn_start (active loop fallback)",
    turnCtx.abortCount >= 1,
    `abortCount=${turnCtx.abortCount}`,
  );

  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t1", toolName: "do_thing", input: {} },
    turnCtx,
  );
  assert(
    "agent-loop interrupt before turn_start replays to block tool_call",
    !!toolResult && toolResult.block === true,
    JSON.stringify(toolResult),
  );
}

async function testMidTurnInterruptFallbackNoIsIdle() {
  // No isIdle() but an agent loop is active -> must still arm.
  const h = makeHarness();
  const turnCtx = makeCtx({}); // no isIdle method
  await h.handlers.session_start({}, turnCtx); // starts the inbox poller
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);

  await deliverInterrupt(h);

  assert(
    "mid-turn interrupt aborts (activeResponseId fallback)",
    turnCtx.abortCount >= 1,
    `abortCount=${turnCtx.abortCount}`,
  );
}

async function testAgentStartClearsStaleWindow() {
  // Belt-and-suspenders: even if a window is armed during a live turn, a brand
  // new agent loop must start clean and not abort its first tool call.
  const h = makeHarness();
  const turnCtx = makeCtx({ idle: false });
  await h.handlers.session_start({}, turnCtx); // starts the inbox poller
  await h.handlers.agent_start({}, turnCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, turnCtx);
  await deliverInterrupt(h);
  assert(
    "window armed during live turn (precondition)",
    turnCtx.abortCount >= 1,
    `abortCount=${turnCtx.abortCount}`,
  );

  // A new agent loop begins (e.g. the user's next message) within 30s.
  const nextCtx = makeCtx({ idle: false });
  await h.handlers.agent_start({}, nextCtx);
  await h.handlers.turn_start({ turnIndex: 1 }, nextCtx);
  const toolResult = await h.handlers.tool_call(
    { toolCallId: "t2", toolName: "do_thing", input: {} },
    nextCtx,
  );

  assert(
    "new agent loop clears stale window (no abort)",
    nextCtx.abortCount === 0,
    `abortCount=${nextCtx.abortCount}`,
  );
  assert(
    "new agent loop's tool_call is NOT blocked",
    !toolResult || toolResult.block !== true,
    JSON.stringify(toolResult),
  );
}

// The web store only clears its local "streaming" flag when a turn's `idle`
// status edge carries the same response_id as the `running` edge that opened
// it. A fresh id per edge left the composer stuck queueing until a tab switch
// reset the store. Assert agent_start/agent_end share one id.
async function testRunningIdleShareResponseId() {
  const h = makeHarness({ captureEvents: true });
  const ctx = makeCtx({ idle: false });

  await h.handlers.agent_start({}, ctx);
  await h.handlers.agent_end({ messages: [] }, ctx);

  const edges = statusEdges(h.postedEvents);
  const running = edges.find((e) => e.status === "running");
  const idle = edges.find((e) => e.status === "idle");

  assert(
    "agent_start posts a running edge with a response_id",
    running !== undefined && typeof running.responseId === "string" && running.responseId.length > 0,
    JSON.stringify(running),
  );
  assert(
    "agent_end posts an idle edge with a response_id",
    idle !== undefined && typeof idle.responseId === "string" && idle.responseId.length > 0,
    JSON.stringify(idle),
  );
  assert(
    "running and idle edges share the same response_id",
    running && idle && running.responseId === idle.responseId,
    `running=${running && running.responseId} idle=${idle && idle.responseId}`,
  );

  // A second turn mints a fresh id, still paired across its own running/idle.
  await h.handlers.agent_start({}, ctx);
  await h.handlers.agent_end({ messages: [] }, ctx);
  const edges2 = statusEdges(h.postedEvents);
  const running2 = edges2.filter((e) => e.status === "running");
  const idle2 = edges2.filter((e) => e.status === "idle");
  assert(
    "second turn pairs its own running/idle id and differs from the first",
    running2.length === 2 &&
      idle2.length === 2 &&
      running2[1].responseId === idle2[1].responseId &&
      running2[1].responseId !== running2[0].responseId,
    `turn1=${running2[0].responseId} turn2=${running2[1].responseId}`,
  );
}

(async () => {
  try {
    await testRunningIdleShareResponseId();
    await testIdleInterruptDoesNotPoisonNextTurn();
    await testIdleInterruptFallbackNoIsIdle();
    await testMidTurnInterruptStillAborts();
    await testAgentLoopInterruptFallbackNoIsIdleBeforeTurnStart();
    await testMidTurnInterruptFallbackNoIsIdle();
    await testAgentStartClearsStaleWindow();
  } finally {
    for (const h of harnesses) {
      if (h.pi.__omnigentInboxPoller) clearInterval(h.pi.__omnigentInboxPoller);
      try {
        fs.rmSync(h.inboxDir, { recursive: true, force: true });
      } catch (_err) {}
    }
  }
})();
