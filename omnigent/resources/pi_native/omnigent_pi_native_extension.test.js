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
function makeHarness({ captureEvents = false, existingTools = [] } = {}) {
  const fixtureRoot = fs.mkdtempSync(path.join(os.tmpdir(), "pi-native-inbox-"));
  const inboxDir = path.join(fixtureRoot, "inbox");
  fs.mkdirSync(inboxDir);
  const configPath = path.join(fixtureRoot, "config.json");
  // A serverUrl + sessionId make postEvent attempt a real fetch; with a mock
  // global fetch that lets a test capture the posted event bodies. Without
  // them postEvent fails closed (the interrupt tests rely on that).
  const config = captureEvents
    ? { inboxDir, bridgeDir: inboxDir, serverUrl: "http://mock", sessionId: "conv_test" }
    : { inboxDir, bridgeDir: inboxDir };
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
  const registeredTools = {};
  const pi = {
    on: (name, fn) => {
      handlers[name] = fn;
    },
    registerCommand: () => {},
    registerTool: (tool) => {
      registeredTools[tool.name] = tool;
    },
    getAllTools: () => existingTools,
    sendUserMessage: () => {},
  };

  // Fresh module-function invocation -> fresh closures.
  delete require.cache[EXT_PATH];
  const mod = require(EXT_PATH);
  mod(pi);

  const h = { pi, handlers, fixtureRoot, configPath, inboxDir, postedEvents, registeredTools };
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

async function testTaskPlanPublishesTodos() {
  const h = makeHarness({ captureEvents: true });
  await h.handlers.session_start({}, {});
  const tool = h.registeredTools.manage_todo_list;
  assert("registers manage_todo_list", !!tool);
  assert(
    "requires a task plan before multi-step work",
    tool.promptGuidelines.some((guideline) =>
      guideline.includes(
        "must call manage_todo_list before using other tools",
      ),
    ),
  );

  const result = await tool.execute("todo-1", {
    operation: "write",
    todoList: [
      {
        id: 1,
        title: "Trace rendering",
        description: "Tracing the shared event path",
        status: "in-progress",
      },
      {
        id: 2,
        title: "Run checks",
        description: "Run focused checks",
        status: "not-started",
      },
    ],
  });
  const event = h.postedEvents.find(
    (item) => item.type === "external_session_todos",
  );
  assert(
    "manage_todo_list publishes the shared todo event",
    JSON.stringify(event && event.data.todos) ===
      JSON.stringify([
        {
          content: "Trace rendering",
          status: "in_progress",
          activeForm: "Tracing the shared event path",
        },
        {
          content: "Run checks",
          status: "pending",
          activeForm: "Run focused checks",
        },
      ]),
    JSON.stringify(event),
  );
  assert(
    "manage_todo_list persists its current list in tool details",
    result.details.todos.length === 2 &&
      result.details.todos[0].status === "in-progress",
    JSON.stringify(result),
  );

  const restored = makeHarness({ captureEvents: true });
  await restored.handlers.session_start(
    {},
    {
      sessionManager: {
        getBranch: () => [
          {
            type: "message",
            message: {
              role: "toolResult",
              toolName: "manage_todo_list",
              details: { todos: result.details.todos },
            },
          },
        ],
      },
    },
  );
  const restoredEvent = restored.postedEvents.find(
    (item) => item.type === "external_session_todos",
  );
  assert(
    "session_start restores and republishes the latest task plan",
    restoredEvent && restoredEvent.data.todos.length === 2,
    JSON.stringify(restoredEvent),
  );
}

async function testExistingTaskToolIsMirroredWithoutConflict() {
  const h = makeHarness({
    captureEvents: true,
    existingTools: [{ name: "manage_todo_list" }],
  });
  await h.handlers.session_start({}, {});
  assert(
    "reuses an existing manage_todo_list tool",
    !h.registeredTools.manage_todo_list,
  );

  await h.handlers.tool_result(
    {
      toolCallId: "external-todo-1",
      toolName: "manage_todo_list",
      details: {
        todos: [
          {
            id: 1,
            title: "Use the shared tool",
            description: "Using the shared task tool",
            status: "in-progress",
          },
        ],
      },
    },
    {},
  );
  const event = h.postedEvents.find(
    (item) => item.type === "external_session_todos",
  );
  assert(
    "mirrors an existing task tool into the shared todo event",
    event && event.data.todos[0].content === "Use the shared tool",
    JSON.stringify(event),
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
  await h.handlers.message_end({
    message: { role: "assistant", stopReason: "stop", content: [] },
  }, ctx);
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
  await h.handlers.message_end({
    message: { role: "assistant", stopReason: "stop", content: [] },
  }, ctx);
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

async function uiSession(mode = "tui", missing = []) {
  const h = makeHarness({ captureEvents: true });
  const originalCalls = [];
  const ui = {};
  for (const [method, result] of Object.entries({
    confirm: true, select: "Allow", input: "typed", editor: "edited",
    notify: undefined, setStatus: undefined, setTitle: undefined,
  })) {
    ui[method] = function (...args) {
      assert(`${method}: fallback retains UI receiver`, this === ui);
      originalCalls.push({ method, args });
      return result;
    };
  }
  for (const method of missing) delete ui[method];
  const ctx = { mode, ui };
  await h.handlers.session_start({}, ctx);
  originalCalls.length = 0;
  return { ...h, ctx, originalCalls };
}

function dialogFetch(respond) {
  global.fetch = async (url, opts) => String(url).includes("pi-extension-ui")
    ? respond(opts) : { ok: true, status: 204 };
}

function dialogResponse(value) {
  return { ok: true, status: 200, text: async () => JSON.stringify(value) };
}

async function testTuiWrapParksConfirmWithoutCallingOriginal() {
  const { ctx, originalCalls } = await uiSession();
  dialogFetch((opts) => {
    const body = JSON.parse(opts.body);
    assert("confirm carries its correlation ID and request", body.elicitation_id &&
      body.request.method === "confirm" && body.request.title === "Clear?");
    return dialogResponse({ confirmed: true });
  });
  assert("confirm returns the server answer", await ctx.ui.confirm("Clear?", "Lost.") === true);
  assert("original TUI dialog is bypassed", !originalCalls.some((call) => call.method === "confirm"));
  ctx.ui.notify("hello");
  ctx.ui.setStatus("test", "ready");
  assert("notifications remain local", originalCalls.some((call) => call.method === "notify") && originalCalls.some((call) => call.method === "setStatus"));
}

async function testUnsupportedDialogsFallBackToTui() {
  for (const status of [404, 405, 403]) {
    const { ctx, originalCalls } = await uiSession();
    dialogFetch(() => ({ ok: false, status }));
    const opts = { timeout: 2000 };
    for (const [method, args, expected] of [
      ["confirm", ["Continue?", "Details", opts], true],
      ["select", ["Choice?", ["Allow", "Block"], opts], "Allow"],
      ["input", ["Name?", "placeholder", opts], "typed"],
      ["editor", ["Notes", "prefill", opts], "edited"],
    ]) {
      const actual = await ctx.ui[method](...args);
      assert(`${status}: ${method} result`, actual === (status === 403 ? (method === "confirm" ? false : undefined) : expected));
      if (status !== 403) {
        const call = originalCalls.at(-1);
        assert(`${method}: preserves arguments`, call.method === method && call.args.every((arg, index) => arg === args[index]));
      }
    }
    assert(`${status}: only unsupported endpoints use TUI`, originalCalls.length === (status === 403 ? 0 : 4));
  }
}

async function testDialogCancellationDoesNotRepark() {
  for (const [response, expectedCalls] of [
    [dialogResponse({ cancelled: true }), 1],
    [{ ok: true, status: 200, text: async () => "" }, 3],
  ]) {
    const { ctx } = await uiSession();
    let calls = 0;
    dialogFetch(() => { calls += 1; return response; });
    assert("unanswered input returns undefined", await ctx.ui.input("Name?", "", { timeout: 6000 }) === undefined);
    assert("cancel stops and empty responses are bounded", calls === expectedCalls);
  }
}

async function testTuiWrapReparksSameElicitationId() {
  const { ctx, configPath } = await uiSession();
  const ids = [], headers = [];
  const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
  const setToken = (token) => fs.writeFileSync(configPath, JSON.stringify({ ...config, authHeaders: { authorization: token } }));
  setToken("first-token");
  dialogFetch((opts) => {
    const body = JSON.parse(opts.body);
    ids.push(body.elicitation_id);
    headers.push(opts.headers.authorization);
    assert("caller timeout reaches the server", body.request.timeout === 6000);
    if (ids.length === 1) {
      setToken("rotated-token");
      return { ok: true, status: 200, text: async () => "" };
    }
    return dialogResponse({ value: "Allow" });
  });
  const started = Date.now();
  assert("select returns chosen label", await ctx.ui.select("Choice?", ["Allow", "Block"], { timeout: 6000 }) === "Allow");
  assert("empty responses back off", Date.now() - started >= 900);
  assert("re-park preserves its ID", ids.length === 2 && ids[0] === ids[1]);
  assert("re-park refreshes the bearer", JSON.stringify(headers) === JSON.stringify(["first-token", "rotated-token"]));
}

async function testPartialUiStillInitializes() {
  const { ctx, registeredTools } = await uiSession("tui", ["editor"]);
  assert("missing editor stays absent", ctx.ui.editor === undefined);
  assert("startup registers task tool", !!registeredTools.manage_todo_list);
  dialogFetch(() => dialogResponse({ confirmed: true }));
  assert("remaining UI still works", await ctx.ui.confirm("Continue?", "") === true);
}

async function testActiveDialogCancellation() {
  for (const phase of ["already-aborted", "headers", "body", "backoff", "timeout"]) {
    const { ctx } = await uiSession();
    const controller = new AbortController();
    let requestStarted;
    const started = new Promise((resolve) => { requestStarted = resolve; });
    dialogFetch((opts) => {
      const wait = () => new Promise((_, reject) => {
        opts.signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
        requestStarted();
      });
      if (phase === "headers" || phase === "timeout") return wait();
      if (phase === "body") return { ok: true, status: 200, text: wait };
      setTimeout(requestStarted, 10);
      return { ok: false, status: 503 };
    });
    if (phase === "already-aborted") controller.abort();
    const pending = ctx.ui.confirm("Continue?", "Test only", {
      ...(phase !== "timeout" ? { signal: controller.signal } : {}),
      timeout: phase === "timeout" ? 50 : 2000,
    });
    if (phase !== "already-aborted" && phase !== "timeout") {
      await started;
      controller.abort();
    }
    let timer;
    try {
      const value = await Promise.race([
        pending,
        new Promise((resolve) => { timer = setTimeout(() => resolve("still-waiting"), 500); }),
      ]);
      assert(`${phase}: abort releases the caller`, value === false, String(value));
    } finally {
      clearTimeout(timer);
    }
    dialogFetch(() => dialogResponse({ confirmed: true }));
    assert(`${phase}: a later dialog works`, await ctx.ui.confirm("Again?", "Test only") === true);
  }
}

async function testRpcModeDoesNotWrapUi() {
  const { ctx, originalCalls } = await uiSession("rpc");
  assert("RPC mode keeps its original UI", await ctx.ui.confirm("Clear?", "Lost.") === true && originalCalls.some((call) => call.method === "confirm"));
}

async function testSessionStartupDoesNotCompleteATurn() {
  const h = makeHarness({ captureEvents: true });
  const ctx = makeCtx({ idle: true });
  ctx.sessionManager = { getSessionId: () => "saved-native-session" };

  await h.handlers.session_start({}, ctx);

  assert(
    "starting an idle or resumed session does not report a completed turn",
    statusEdges(h.postedEvents).length === 0,
    JSON.stringify(statusEdges(h.postedEvents)),
  );
}

async function testSessionStartMarksInputReady() {
  const h = makeHarness();
  const marker = path.join(h.inboxDir, "input_ready");
  assert("input-ready marker is absent before session_start", !fs.existsSync(marker));

  await h.handlers.session_start({}, makeCtx({ idle: true }));

  assert(
    "session_start writes the input-ready marker once the inbox poller is armed",
    fs.existsSync(marker) && !!h.pi.__omnigentInboxPoller,
  );
}

async function testQueuedPromptDuringStartupStaysRunningUntilAgentEnd() {
  const h = makeHarness({ captureEvents: true });
  const ctx = makeCtx({ idle: true });
  ctx.sessionManager = { getSessionId: () => "queued-native-session" };
  const captureFetch = global.fetch;
  let releaseStartupPatch;
  const startupPatchPending = new Promise((resolve) => {
    releaseStartupPatch = resolve;
  });
  global.fetch = async (url, opts) => {
    if (opts.method === "PATCH") await startupPatchPending;
    return captureFetch(url, opts);
  };
  let agentStartPromise;
  h.pi.sendUserMessage = () => {
    ctx.isIdle = () => false;
    agentStartPromise = h.handlers.agent_start({}, ctx);
  };
  const payloadPath = path.join(h.inboxDir, "first-prompt.json");
  fs.writeFileSync(
    payloadPath,
    JSON.stringify({ id: "first-prompt", type: "user_message", content: "Run the task" }),
  );
  const startup = h.handlers.session_start({}, ctx);
  try {
    const deadline = Date.now() + 3000;
    while (fs.existsSync(payloadPath)) {
      if (Date.now() > deadline) throw new Error("startup prompt was not consumed by poller");
      await sleep(20);
    }
    if (!agentStartPromise) throw new Error("the queued prompt did not start an agent loop");
    await agentStartPromise;
    releaseStartupPatch();
    await startup;

    const beforeEnd = statusEdges(h.postedEvents);
    assert(
      "finishing startup preserves the queued prompt's running turn",
      beforeEnd.length === 1 && beforeEnd[0].status === "running",
      JSON.stringify(beforeEnd),
    );

    await h.handlers.message_end({
      message: { role: "assistant", stopReason: "stop", content: [] },
    }, ctx);
    await h.handlers.agent_end({ messages: [] }, ctx);
    const afterEnd = statusEdges(h.postedEvents);
    assert(
      "only agent_end completes the queued prompt with the same response_id",
      afterEnd.length === 2 &&
        afterEnd[0].status === "running" &&
        afterEnd[1].status === "idle" &&
        afterEnd[0].responseId === afterEnd[1].responseId,
      JSON.stringify(afterEnd),
    );
  } finally {
    releaseStartupPatch();
    await startup;
    global.fetch = captureFetch;
  }
}

// A parent session file with child session files beneath it, watched by the real module.
// Posts are recorded once answered; an infinite delay never answers, and a stalled post
// fails only when aborted, recording why and how many posts had arrived by then.
function childFixture({ responseDelayMs = 0, postTimeoutMs, stalls = () => false } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "pi-children-"));
  harnesses.push({ pi: {}, fixtureRoot: root });
  const sessions = path.join(root, "sessions");
  const bridgeDir = path.join(root, "bridge");
  fs.mkdirSync(sessions);
  fs.mkdirSync(bridgeDir);
  const parentFile = path.join(sessions, "2026-01-01T00-00-00-000Z_parent.jsonl");
  fs.writeFileSync(parentFile, JSON.stringify({ type: "session", id: "parent" }) + "\n");
  const posts = [];
  const stalled = [];
  global.fetch = async (url, opts) => {
    const body = JSON.parse(opts.body);
    const sessionId = decodeURIComponent(String(url).split("/v1/sessions/")[1].split("/")[0]);
    if (responseDelayMs === Infinity) return new Promise(() => {});
    if (stalls(body)) {
      return new Promise((_resolve, reject) => opts.signal.addEventListener("abort", () => {
        stalled.push({ body, postsBefore: posts.length, reason: opts.signal.reason?.name });
        reject(opts.signal.reason);
      }));
    }
    if (responseDelayMs) await new Promise((resolve) => setTimeout(resolve, responseDelayMs));
    posts.push({ sessionId, body });
    const json = body.type === "external_acp_subagent_start"
      ? { child_session_id: `conv_${body.data.subagent_id}` }
      : {};
    return { ok: true, status: 202, json: async () => json };
  };
  const config = { serverUrl: "http://mock", sessionId: "conv_parent", bridgeDir, assistantItemPostTimeoutMs: postTimeoutMs };
  delete require.cache[EXT_PATH];
  const { createChildSessionWatcher } = require(EXT_PATH);
  const watcher = (now = Date.now) =>
    createChildSessionWatcher(config, { sessionFile: () => parentFile, now });
  return { sessions, bridgeDir, parentFile, posts, stalled, watcher };
}

function childSessionLines({ id, parentFile, agent, startedAt = 1000 }) {
  return [
    { type: "session", id, timestamp: new Date(startedAt).toISOString(), parentSession: parentFile },
    { type: "session_info", name: `subagent-${agent}-00000000-0000-4000-8000-000000000000-1` },
  ].map((line) => JSON.stringify(line) + "\n").join("");
}

function assistantLine(model, timestamp, input, output, stopReason = "toolUse") {
  return JSON.stringify({
    type: "message",
    message: {
      role: "assistant", model, timestamp, responseId: `r-${timestamp}`, stopReason,
      usage: { input, output, cacheRead: 10, cacheWrite: 0, totalTokens: input + output + 10, cost: { total: 0 } },
    },
  }) + "\n";
}

function childUsage(posts, sessionId) {
  return posts.filter((p) => p.sessionId === sessionId && p.body.type === "external_session_usage")
    .map((p) => p.body.data);
}

async function testChildSessionsAreRegisteredWithTheirOwnModels() {
  const f = childFixture();
  const childDir = path.join(f.sessions, path.basename(f.parentFile, ".jsonl"));
  const writeChild = (relative, text) => {
    fs.mkdirSync(path.dirname(path.join(childDir, relative)), { recursive: true });
    fs.writeFileSync(path.join(childDir, relative), text);
  };
  writeChild("run-a/run-0/session.jsonl",
    // Launched in the same millisecond: the UUIDv7 ids differ only at the end.
    childSessionLines({ id: "01a1f1a6-6b58-7306-959e-c50b9edd4745", parentFile: f.parentFile, agent: "worker" }) +
    assistantLine("model-a", 2000, 100, 10) +
    // The same message seen twice is counted once.
    assistantLine("model-a", 2000, 100, 10));
  writeChild("run-b/run-0/session.jsonl",
    childSessionLines({ id: "01a1f1a6-6b58-7306-959e-0a1b2c3d4e5f", parentFile: f.parentFile, agent: "worker", startedAt: 5000 }) +
    // Copied by a fork from the parent before the child started.
    assistantLine("model-parent", 4000, 999, 999) +
    assistantLine("model-b", 6000, 50, 5));
  // Same agent name and the same last eight id characters as the first child.
  writeChild("run-c/run-0/session.jsonl",
    childSessionLines({ id: "01a1f1a6-6b58-7306-959e-d61c9edd4745", parentFile: f.parentFile, agent: "worker" }) +
    assistantLine("model-c", 2000, 200, 20));
  // Pi's own /fork writes a top-level sibling that names the parent.
  fs.writeFileSync(path.join(f.sessions, "fork.jsonl"),
    childSessionLines({ id: "pi-fork", parentFile: f.parentFile, agent: "x" }) +
    assistantLine("model-fork", 2000, 1, 1));

  await f.watcher().poll();

  const starts = f.posts.filter((p) => p.body.type === "external_acp_subagent_start");
  assert("each child is registered under the parent once",
    JSON.stringify(starts.map((p) => [p.sessionId, p.body.data.subagent_id, p.body.data.title]).sort()) ===
      JSON.stringify([
        ["conv_parent", "01a1f1a6-6b58-7306-959e-0a1b2c3d4e5f", "worker"],
        ["conv_parent", "01a1f1a6-6b58-7306-959e-c50b9edd4745", "worker"],
        ["conv_parent", "01a1f1a6-6b58-7306-959e-d61c9edd4745", "worker"],
      ]),
    JSON.stringify(starts));
  const usageA = childUsage(f.posts, "conv_01a1f1a6-6b58-7306-959e-c50b9edd4745");
  const usageB = childUsage(f.posts, "conv_01a1f1a6-6b58-7306-959e-0a1b2c3d4e5f");
  const usageC = childUsage(f.posts, "conv_01a1f1a6-6b58-7306-959e-d61c9edd4745");
  assert("child usage is priced by its own model, deduped, without a zero Pi cost",
    usageA.length === 1 && usageA[0].model === "model-a" &&
      usageA[0].cumulative_input_tokens === 110 && usageA[0].cumulative_output_tokens === 10 &&
      usageA[0].cumulative_cache_read_input_tokens === 10 &&
      usageA[0].cost_complete === false && !("cumulative_cost_usd" in usageA[0]),
    JSON.stringify(usageA));
  assert("forked history is not billed to the child",
    usageB.length === 1 && usageB[0].model === "model-b" && usageB[0].cumulative_input_tokens === 60,
    JSON.stringify(usageB));
  assert("same-named children sharing an id suffix keep separate usage",
    usageC.length === 1 && usageC[0].cumulative_input_tokens === 210 && usageA[0]?.cumulative_input_tokens === 110,
    JSON.stringify(usageC));
  assert("a running child reports running",
    f.posts.some((p) => p.sessionId === "conv_01a1f1a6-6b58-7306-959e-c50b9edd4745" && p.body.type === "external_session_status" &&
      p.body.data.status === "running"));
}

async function testChildReadsIncrementallyAndSurvivesRestart() {
  const f = childFixture();
  const file = path.join(f.sessions, path.basename(f.parentFile, ".jsonl"), "run", "session.jsonl");
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const second = assistantLine("model-a", 3000, 20, 2, "stop");
  fs.writeFileSync(file,
    childSessionLines({ id: "child", parentFile: f.parentFile, agent: "worker" }) +
    assistantLine("model-a", 2000, 100, 10) + second.slice(0, 30));

  await f.watcher().poll();
  assert("a partial trailing line is not counted",
    childUsage(f.posts, "conv_child").at(-1)?.cumulative_input_tokens === 110,
    JSON.stringify(childUsage(f.posts, "conv_child")));

  fs.appendFileSync(file, second.slice(30));
  // A relaunched extension resumes from the persisted cursor and totals.
  const later = () => Date.now() + 60_000;
  await f.watcher(later).poll();
  const usage = childUsage(f.posts, "conv_child");
  assert("restart completes the line without double counting",
    usage.at(-1).cumulative_input_tokens === 140 && usage.at(-1).cumulative_output_tokens === 12,
    JSON.stringify(usage));
  assert("restart does not register the child again",
    f.posts.filter((p) => p.body.type === "external_acp_subagent_start").length === 1);
  assert("a quiet child that ended its turn reports idle without a terminal edge",
    f.posts.some((p) => p.sessionId === "conv_child" && p.body.type === "subagent.status") &&
      !f.posts.some((p) => p.body.type === "external_session_status" && p.body.data.status !== "running"));
  const state = JSON.parse(fs.readFileSync(path.join(f.bridgeDir, "pi_subagent_children.json"), "utf8"));
  assert("the cursor covers exactly the consumed bytes", state.children[0].offset === fs.statSync(file).size);
}

function writeChildFile(f, relative, text) {
  const file = path.join(f.sessions, path.basename(f.parentFile, ".jsonl"), relative);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, text);
  return file;
}

function childStatePath(f) {
  return path.join(f.bridgeDir, "pi_subagent_children.json");
}

async function testShutdownDrainsPastAStalledRegistration() {
  // Fast responses, but the whole drain outlasts one post timeout.
  const f = childFixture({
    responseDelayMs: 40, postTimeoutMs: 100,
    stalls: (body) => body.data?.subagent_id === "stalled",
  });
  const watcher = f.watcher();
  const registered = writeChildFile(f, "registered/session.jsonl",
    childSessionLines({ id: "registered", parentFile: f.parentFile, agent: "worker" }) +
    assistantLine("model-a", 2000, 100, 10));
  await watcher.poll();
  // Its final reply ends the turn moments before shutdown.
  fs.appendFileSync(registered, assistantLine("model-a", 3000, 20, 2, "stop"));
  writeChildFile(f, "stalled/session.jsonl",
    childSessionLines({ id: "stalled", parentFile: f.parentFile, agent: "worker" }) +
    assistantLine("model-a", 2000, 1, 1));
  await watcher.stop();
  const registeredUsage = f.posts.findIndex((p) =>
    p.body.type === "external_session_usage" && p.body.data.cumulative_input_tokens === 140);
  assert("a registered child's new usage is posted without waiting on a stalled registration",
    f.posts[registeredUsage]?.sessionId === "conv_registered" && registeredUsage < f.stalled[0]?.postsBefore,
    JSON.stringify(f.stalled));
  assert("a poll tries a stalled registration once", f.stalled.length === 1, JSON.stringify(f.stalled));
  assert("a child whose turn ended just before shutdown is reported idle",
    f.posts.some((p) => p.sessionId === "conv_registered" && p.body.type === "subagent.status"),
    JSON.stringify(f.posts));

  // A restarted watcher, with the stalled registration still pending and a poll in flight.
  watcher.start();
  watcher.poll();
  await watcher.stop();
  assert("shutdown abandons the in-flight poll's hung request, and each poll tries it once",
    f.stalled.length === 3 && f.stalled[1].reason === "AbortError" && f.stalled[2].reason === "TimeoutError",
    JSON.stringify(f.stalled));
}

async function testChildrenWrittenAfterTheLastPollAreReportedAtShutdown() {
  const f = childFixture();
  const watcher = f.watcher();
  await watcher.poll();
  // A tool result longer than one read chunk precedes the final usage.
  const bigLine = JSON.stringify({ type: "message", message: { role: "toolResult", content: "x".repeat(1100 * 1024) } }) + "\n";
  writeChildFile(f, "run/session.jsonl",
    childSessionLines({ id: "late", parentFile: f.parentFile, agent: "worker" }) +
    bigLine + assistantLine("model-a", 2000, 100, 10));
  const launcher = writeChildFile(f, "b-launcher/session.jsonl",
    childSessionLines({ id: "launcher", parentFile: f.parentFile, agent: "lead" }) +
    assistantLine("model-a", 2000, 30, 3));
  writeChildFile(f, "a-nested/session.jsonl",
    childSessionLines({ id: "nested01", parentFile: launcher, agent: "scout" }) +
    assistantLine("model-b", 2000, 40, 4));
  // No session_info title yet, written moments before shutdown.
  writeChildFile(f, "untitled/session.jsonl",
    JSON.stringify({ type: "session", id: "untitled", timestamp: new Date(1000).toISOString(), parentSession: f.parentFile }) +
    "\n" + assistantLine("model-a", 2000, 50, 5, "stop"));

  // Sorted enumeration lists the nested child's directory before its launcher's.
  const readdirSync = fs.readdirSync;
  fs.readdirSync = (...args) => readdirSync(...args).sort((a, b) => (a.name < b.name ? -1 : 1));
  const readSync = fs.readSync;
  let largestRead = 0;
  fs.readSync = (...args) => {
    largestRead = Math.max(largestRead, args[3]);
    return readSync(...args);
  };
  try {
    await watcher.stop();
  } finally {
    fs.readdirSync = readdirSync;
    fs.readSync = readSync;
  }
  assert("a finished child first seen at shutdown is reported idle",
    f.posts.some((p) => p.sessionId === "conv_untitled" && p.body.type === "subagent.status"), JSON.stringify(f.posts));
  assert("backlog reads stay within one chunk", largestRead <= 1024 * 1024, String(largestRead));
  const started = (subagentId, sessionId = "conv_parent") => f.posts.some((p) =>
    p.sessionId === sessionId && p.body.type === "external_acp_subagent_start" && p.body.data.subagent_id === subagentId);
  assert("a child written after the last poll is registered and reported past a chunk boundary at shutdown",
    started("late") && childUsage(f.posts, "conv_late").at(-1)?.cumulative_input_tokens === 110,
    JSON.stringify(f.posts.filter((p) => p.sessionId.includes("late"))));
  assert("a nested child enumerated before its launcher attaches to it at shutdown",
    started("launcher") && started("nested01", "conv_launcher") &&
      childUsage(f.posts, "conv_launcher").at(-1)?.cumulative_input_tokens === 40 &&
      childUsage(f.posts, "conv_nested01").at(-1)?.cumulative_input_tokens === 50,
    JSON.stringify(f.posts));
  assert("an untitled child written just before shutdown is reported under the fallback title",
    f.posts.some((p) => p.body.type === "external_acp_subagent_start" &&
      p.body.data.subagent_id === "untitled" && p.body.data.title === "subagent") &&
      childUsage(f.posts, "conv_untitled").at(-1)?.cumulative_input_tokens === 60,
    JSON.stringify(f.posts));
}

async function testAnAbortedPollStartsNoFurtherPosts() {
  let usageStarts = 0;
  let idleStarts = 0;
  let oldUsageStarted;
  const oldUsage = new Promise((resolve) => { oldUsageStarted = resolve; });
  // The in-flight poll's usage post hangs, as would any idle post started before the final usage.
  const f = childFixture({
    postTimeoutMs: 100,
    stalls: (body) => {
      if (body.type === "subagent.status") return ++idleStarts > 0 && usageStarts === 2;
      if (body.type !== "external_session_usage" || ++usageStarts !== 2) return false;
      oldUsageStarted();
      return true;
    },
  });
  let clock = Date.now();
  const watcher = f.watcher(() => clock);
  const file = writeChildFile(f, "run/session.jsonl",
    childSessionLines({ id: "child", parentFile: f.parentFile, agent: "worker" }) + assistantLine("model-a", 2000, 100, 10));
  await watcher.poll();
  fs.appendFileSync(file, assistantLine("model-a", 3000, 20, 2, "stop"));
  // Quiet long enough that the in-flight poll would post idle after its usage post.
  clock += 60_000;
  watcher.poll();
  await oldUsage;
  await watcher.stop();
  assert("an aborted poll starts no status post after its usage post is aborted",
    idleStarts === 1 && f.stalled.length === 1 && f.stalled[0].reason === "AbortError",
    JSON.stringify({ idleStarts, stalled: f.stalled }));
  assert("the final poll still delivers the child's usage and idle status",
    childUsage(f.posts, "conv_child").at(-1)?.cumulative_input_tokens === 140 &&
      f.posts.some((p) => p.sessionId === "conv_child" && p.body.type === "subagent.status"),
    JSON.stringify(f.posts));
}

async function testRediscoveredTitleKeepsOneChildIdentity() {
  const f = childFixture();
  const file = writeChildFile(f, "run/session.jsonl",
    JSON.stringify({ type: "session", id: "child", timestamp: new Date(1000).toISOString(), parentSession: f.parentFile }) +
    "\n" + assistantLine("model-a", 2000, 100, 10));
  // Past the title grace period, so the child registers under the fallback title.
  await f.watcher(() => Date.now() + 60_000).poll();
  fs.writeFileSync(childStatePath(f), "{not json");
  const longAgent = "w".repeat(200);
  fs.appendFileSync(file, JSON.stringify({
    type: "session_info", name: `subagent-${longAgent}-00000000-0000-4000-8000-000000000000-1`,
  }) + "\n");
  await f.watcher().poll();
  const starts = f.posts.filter((p) => p.body.type === "external_acp_subagent_start").map((p) => p.body.data);
  assert("a child rediscovered under its real title keeps one registration identity",
    starts.length === 2 && starts.every((data) => data.subagent_id === "child") && starts[0].title === "subagent",
    JSON.stringify(starts));
  const usageTargets = new Set(f.posts.filter((p) => p.body.type === "external_session_usage").map((p) => p.sessionId));
  assert("a rediscovered child reports usage to a single child session",
    usageTargets.size === 1 && childUsage(f.posts, "conv_child").at(-1)?.cumulative_input_tokens === 110,
    JSON.stringify([...usageTargets]));
  assert("an oversized session name is capped in the title", starts[1]?.title === longAgent.slice(0, 80),
    JSON.stringify(starts[1]));
}

async function testMalformedPersistedChildIsRebuiltFromItsFile() {
  const f = childFixture();
  const file = writeChildFile(f, "run/session.jsonl",
    childSessionLines({ id: "child", parentFile: f.parentFile, agent: "worker" }) + assistantLine("model-a", 2000, 100, 10));
  fs.writeFileSync(childStatePath(f), JSON.stringify({
    roots: [f.parentFile],
    children: [{
      file, key: "child", parentFile: f.parentFile, title: "worker", startedAt: 1000,
      childId: "conv_child", offset: "bad", lastStop: null, usage: null,
    }],
  }));
  await f.watcher().poll().catch(() => {});
  assert("a malformed persisted child is rebuilt from its file",
    childUsage(f.posts, "conv_child").at(-1)?.cumulative_input_tokens === 110, JSON.stringify(f.posts));
}

async function testShutdownStopsReadingAtItsDeadline() {
  const f = childFixture();
  const watcher = f.watcher();
  const file = writeChildFile(f, "run/session.jsonl",
    childSessionLines({ id: "child", parentFile: f.parentFile, agent: "worker" }) + assistantLine("model-a", 2000, 100, 10));
  await watcher.poll();
  const bigLine = JSON.stringify({ type: "message", message: { role: "toolResult", content: "x".repeat(1100 * 1024) } }) + "\n";
  fs.appendFileSync(file,
    bigLine + assistantLine("model-a", 3000, 20, 2) + bigLine + assistantLine("model-a", 4000, 30, 3));
  // Each full-chunk read appears to outlast the whole shutdown budget.
  const readSync = fs.readSync;
  const realNow = Date.now;
  let skew = 0;
  fs.readSync = (...args) => {
    if (args[3] === 1024 * 1024) skew += 10_000;
    return readSync(...args);
  };
  Date.now = () => realNow() + skew;
  try {
    await watcher.stop();
  } finally {
    fs.readSync = readSync;
    Date.now = realNow;
  }
  const offset = JSON.parse(fs.readFileSync(childStatePath(f), "utf8")).children[0].offset;
  const bytes = fs.readFileSync(file);
  assert("shutdown stops reading at its deadline with the cursor on a line boundary",
    offset < bytes.length && bytes[offset - 1] === 10, `${offset} of ${bytes.length}`);
  await f.watcher().poll();
  assert("the next launch resumes from the cursor without double counting",
    childUsage(f.posts, "conv_child").at(-1)?.cumulative_input_tokens === 180,
    JSON.stringify(childUsage(f.posts, "conv_child")));
}

async function testShutdownIsBoundedWhenTheServerNeverResponds() {
  const f = childFixture({ responseDelayMs: Infinity, postTimeoutMs: 20 });
  const file = path.join(f.sessions, path.basename(f.parentFile, ".jsonl"), "run", "session.jsonl");
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file,
    childSessionLines({ id: "hung", parentFile: f.parentFile, agent: "worker" }) + assistantLine("model-a", 2000, 1, 1));
  const watcher = f.watcher();
  watcher.poll();
  const startedAt = Date.now();
  await watcher.stop();
  assert("shutdown stops waiting for a server that never responds", Date.now() - startedAt < 2_000);
}

(async () => {
  try {
    await testChildSessionsAreRegisteredWithTheirOwnModels();
    await testChildReadsIncrementallyAndSurvivesRestart();
    await testShutdownDrainsPastAStalledRegistration();
    await testChildrenWrittenAfterTheLastPollAreReportedAtShutdown();
    await testAnAbortedPollStartsNoFurtherPosts();
    await testRediscoveredTitleKeepsOneChildIdentity();
    await testMalformedPersistedChildIsRebuiltFromItsFile();
    await testShutdownStopsReadingAtItsDeadline();
    await testShutdownIsBoundedWhenTheServerNeverResponds();
    await testSessionStartupDoesNotCompleteATurn();
    await testSessionStartMarksInputReady();
    await testQueuedPromptDuringStartupStaysRunningUntilAgentEnd();
    await testRunningIdleShareResponseId();
    await testTaskPlanPublishesTodos();
    await testExistingTaskToolIsMirroredWithoutConflict();
    await testIdleInterruptDoesNotPoisonNextTurn();
    await testIdleInterruptFallbackNoIsIdle();
    await testMidTurnInterruptStillAborts();
    await testAgentLoopInterruptFallbackNoIsIdleBeforeTurnStart();
    await testMidTurnInterruptFallbackNoIsIdle();
    await testAgentStartClearsStaleWindow();
    await testTuiWrapParksConfirmWithoutCallingOriginal();
    await testUnsupportedDialogsFallBackToTui();
    await testDialogCancellationDoesNotRepark();
    await testTuiWrapReparksSameElicitationId();
    await testPartialUiStillInitializes();
    await testActiveDialogCancellation();
    await testRpcModeDoesNotWrapUi();
  } finally {
    for (const h of harnesses) {
      if (h.pi.__omnigentInboxPoller) clearInterval(h.pi.__omnigentInboxPoller);
      try {
        fs.rmSync(h.fixtureRoot, { recursive: true, force: true });
      } catch (_err) {}
    }
  }
})();
