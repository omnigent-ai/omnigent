"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { it } = require("node:test");
const { createBrowserAutomationTransport } = require("../src/browserAutomationTransport");

const nextTurn = () =>
  new Promise((resolve) => {
    setImmediate(resolve);
  });

async function transportFixture(
  testContext,
  { attached: initiallyAttached = false, ...options } = {},
) {
  const commands = [];
  const lifecycle = [];
  let attached = initiallyAttached;
  const debuggerApi = Object.assign(new EventEmitter(), {
    isAttached: () => attached,
    attach(version) {
      attached = true;
      lifecycle.push(["attach", version]);
    },
    detach() {
      attached = false;
      lifecycle.push(["detach"]);
    },
    async sendCommand(...args) {
      commands.push(args);
      return { answer: "page" };
    },
  });
  const webContents = Object.assign(new EventEmitter(), {
    debugger: debuggerApi,
    getURL: () => "https://example.test/",
    getTitle: () => "Example",
  });
  const transport = await createBrowserAutomationTransport(webContents, options);
  testContext.after(() => transport.close());
  const messages = [];
  transport.onmessage = (message) => messages.push(JSON.parse(message));
  let nextId = 0;
  const send = async (method, params, sessionId) => {
    const id = ++nextId;
    transport.send(JSON.stringify({ id, method, params, sessionId }));
    await nextTurn();
    return messages.find((message) => message.id === id);
  };
  return { webContents, debuggerApi, transport, commands, lifecycle, messages, send };
}

it("exposes only its virtual tab and page through Puppeteer's browser handshake", async (testContext) => {
  const fixture = await transportFixture(testContext);
  assert.deepEqual((await fixture.send("Target.getBrowserContexts")).result, {
    browserContextIds: [],
  });
  await fixture.send("Target.setDiscoverTargets", { discover: true });
  const targets = fixture.messages.filter((message) => message.method === "Target.targetCreated");
  assert.deepEqual(
    targets.map((message) => message.params.targetInfo.type),
    ["tab", "page"],
  );
  assert.equal(targets[1].params.targetInfo.targetId, fixture.transport.pageTargetId);
  await fixture.send("Target.setAutoAttach", { autoAttach: true });
  const tabSession = fixture.messages.find(
    (message) => message.method === "Target.attachedToTarget",
  ).params.sessionId;
  await fixture.send("Target.setAutoAttach", { autoAttach: true }, tabSession);
  const pageAttached = fixture.messages.find(
    (message) => message.sessionId === tabSession && message.method === "Target.attachedToTarget",
  );
  assert.equal(pageAttached.params.targetInfo.targetId, fixture.transport.pageTargetId);
  assert.deepEqual(
    (await fixture.send("Runtime.runIfWaitingForDebugger", {}, tabSession)).result,
    {},
  );
  assert.deepEqual(
    fixture.commands,
    [],
    "virtual browser commands never reach a browser-wide connection",
  );
});

it(
  "connects the installed Puppeteer library to one fake Electron debugger",
  { timeout: 2_000 },
  async (testContext) => {
    const { connect } = await import("puppeteer-core");
    const fixture = await transportFixture(testContext);
    const browser = await connect({ transport: fixture.transport, defaultViewport: null });
    testContext.after(() => browser.disconnect());
    assert.equal(browser.targets().filter((target) => target.type() === "page").length, 1);
    assert.ok(fixture.commands.every(([method]) => !method.startsWith("Browser.")));
  },
);

it("routes the virtual page session to the supplied debugger and preserves protocol errors", async (testContext) => {
  const fixture = await transportFixture(testContext);
  const pageSession = (
    await fixture.send("Target.attachToTarget", { targetId: fixture.transport.pageTargetId })
  ).result.sessionId;
  const result = await fixture.send("Runtime.evaluate", { expression: "1" }, pageSession);
  assert.equal(result.sessionId, pageSession);
  assert.deepEqual(result.result, { answer: "page" });
  assert.deepEqual(fixture.commands, [["Runtime.evaluate", { expression: "1" }, undefined]]);
  fixture.debuggerApi.sendCommand = async () => {
    throw new Error("page stopped");
  };
  assert.match(
    (await fixture.send("Runtime.evaluate", {}, pageSession)).error.message,
    /page stopped/,
  );
});

it("refuses foreign targets, foreign sessions and desktop-owned tab lifecycle", async (testContext) => {
  const fixture = await transportFixture(testContext);
  for (const [method, params, sessionId] of [
    ["Target.attachToTarget", { targetId: "another-page" }],
    ["Target.closeTarget", { targetId: fixture.transport.pageTargetId }],
    ["Target.createTarget", { url: "https://example.test/" }],
    ["Browser.close"],
    ["Runtime.evaluate", { expression: "1" }, "another-session"],
  ]) {
    // oxlint-disable-next-line no-await-in-loop -- Inspect each refusal and its command list.
    assert.ok((await fixture.send(method, params, sessionId)).error, method);
  }
  assert.deepEqual(fixture.commands, []);
});

it("routes descendant iframe sessions without discovering other page targets", async (testContext) => {
  const fixture = await transportFixture(testContext);
  const pageSession = (
    await fixture.send("Target.attachToTarget", { targetId: fixture.transport.pageTargetId })
  ).result.sessionId;
  await fixture.send("Target.setAutoAttach", { autoAttach: true, flatten: true }, pageSession);
  assert.deepEqual(fixture.commands[0][1].filter, [
    { type: "iframe", exclude: false },
    { type: "worker", exclude: false },
    { exclude: true },
  ]);
  fixture.debuggerApi.emit("message", {}, "Target.attachedToTarget", {
    sessionId: "frame-session",
    targetInfo: { targetId: "frame-target", type: "iframe" },
  });
  fixture.debuggerApi.emit("message", {}, "Target.attachedToTarget", {
    sessionId: "foreign-page-session",
    targetInfo: { targetId: "foreign-page", type: "page" },
  });
  await fixture.send("Runtime.evaluate", { expression: "2" }, "frame-session");
  assert.deepEqual(fixture.commands.at(-1), [
    "Runtime.evaluate",
    { expression: "2" },
    "frame-session",
  ]);
  assert.ok((await fixture.send("Runtime.evaluate", {}, "foreign-page-session")).error);
  fixture.debuggerApi.emit(
    "message",
    {},
    "Runtime.consoleAPICalled",
    { type: "log" },
    "unknown-session",
  );
  fixture.debuggerApi.emit(
    "message",
    {},
    "Runtime.consoleAPICalled",
    { type: "log" },
    "frame-session",
  );
  await nextTurn();
  assert.equal(
    fixture.messages.filter((message) => message.method === "Runtime.consoleAPICalled").length,
    1,
  );
  fixture.debuggerApi.emit("message", {}, "Target.detachedFromTarget", {
    sessionId: "frame-session",
  });
  assert.ok((await fixture.send("Runtime.evaluate", {}, "frame-session")).error);
});

it("borrows an already attached guarded debugger without detaching its owner", async (testContext) => {
  let guardedCalls = 0;
  const guarded = Object.assign(new EventEmitter(), {
    isAttached: () => true,
    async sendCommand() {
      guardedCalls += 1;
      return {};
    },
  });
  const fixture = await transportFixture(testContext, { attached: true, debuggerClient: guarded });
  const pageSession = (
    await fixture.send("Target.attachToTarget", { targetId: fixture.transport.pageTargetId })
  ).result.sessionId;
  await fixture.send(
    "Target.setAutoAttach",
    { autoAttach: true, waitForDebuggerOnStart: true },
    pageSession,
  );
  guarded.emit("message", {}, "Target.attachedToTarget", {
    sessionId: "frame-session",
    targetInfo: { targetId: "frame-target", type: "iframe" },
  });
  await fixture.send(
    "Target.setAutoAttach",
    { autoAttach: true, waitForDebuggerOnStart: true },
    "frame-session",
  );
  await fixture.send("Runtime.evaluate", {}, pageSession);
  fixture.transport.close();
  assert.equal(guardedCalls, 3);
  assert.deepEqual(fixture.lifecycle, []);
  assert.equal(guarded.listenerCount("message"), 0);
  assert.deepEqual(fixture.commands, [
    [
      "Target.setAutoAttach",
      { autoAttach: false, waitForDebuggerOnStart: false, flatten: true },
      "frame-session",
    ],
    [
      "Target.setAutoAttach",
      { autoAttach: false, waitForDebuggerOnStart: false, flatten: true },
      undefined,
    ],
  ]);
});

it("rejects a stale action before dispatch and drops replies after view destruction", async (testContext) => {
  const fixture = await transportFixture(testContext, {
    beforeSend(method) {
      if (method === "Input.dispatchMouseEvent") throw new Error("stale target");
    },
  });
  const pageSession = (
    await fixture.send("Target.attachToTarget", { targetId: fixture.transport.pageTargetId })
  ).result.sessionId;
  assert.match(
    (await fixture.send("Input.dispatchMouseEvent", {}, pageSession)).error.message,
    /stale target/,
  );
  assert.equal(fixture.commands.length, 0);
  let answer;
  fixture.debuggerApi.sendCommand = () =>
    new Promise((resolve) => {
      answer = resolve;
    });
  let closed = 0;
  fixture.transport.onclose = () => {
    closed += 1;
  };
  await fixture.send("Runtime.evaluate", {}, pageSession);
  fixture.webContents.emit("destroyed");
  const count = fixture.messages.length;
  answer({ late: true });
  await nextTurn();
  assert.equal(fixture.messages.length, count);
  assert.equal(closed, 1);
  assert.deepEqual(fixture.lifecycle, [["attach", "1.3"], ["detach"]]);
});

it("removes only its own document preload registrations in each native session", async (testContext) => {
  const fixture = await transportFixture(testContext, { attached: true });
  const nativeSend = fixture.debuggerApi.sendCommand;
  fixture.debuggerApi.sendCommand = (method, params, sessionId) =>
    method === "Page.addScriptToEvaluateOnNewDocument"
      ? Promise.resolve({ identifier: sessionId ? "frame-script" : "main-script" })
      : nativeSend(method, params, sessionId);
  const pageSession = (
    await fixture.send("Target.attachToTarget", { targetId: fixture.transport.pageTargetId })
  ).result.sessionId;
  await fixture.send("Page.addScriptToEvaluateOnNewDocument", { source: "void 0" }, pageSession);
  fixture.debuggerApi.emit("message", {}, "Target.attachedToTarget", {
    sessionId: "frame-session",
    targetInfo: { targetId: "frame-target", type: "iframe" },
  });
  await fixture.send(
    "Page.addScriptToEvaluateOnNewDocument",
    { source: "void 0" },
    "frame-session",
  );
  await fixture.send(
    "Page.removeScriptToEvaluateOnNewDocument",
    { identifier: "main-script" },
    pageSession,
  );
  fixture.transport.close();
  assert.deepEqual(fixture.commands, [
    ["Page.removeScriptToEvaluateOnNewDocument", { identifier: "main-script" }, undefined],
    ["Page.removeScriptToEvaluateOnNewDocument", { identifier: "frame-script" }, "frame-session"],
  ]);
  assert.deepEqual(fixture.lifecycle, [], "the borrowed debugger stays attached");
});

it("removes a document preload whose registration finishes after disconnect", async (testContext) => {
  const fixture = await transportFixture(testContext, { attached: true });
  let finishRegistration;
  const nativeSend = fixture.debuggerApi.sendCommand;
  fixture.debuggerApi.sendCommand = (method, params, sessionId) =>
    method === "Page.addScriptToEvaluateOnNewDocument"
      ? new Promise((resolve) => {
          finishRegistration = resolve;
        })
      : nativeSend(method, params, sessionId);
  const pageSession = (
    await fixture.send("Target.attachToTarget", { targetId: fixture.transport.pageTargetId })
  ).result.sessionId;
  await fixture.send("Page.addScriptToEvaluateOnNewDocument", { source: "void 0" }, pageSession);
  fixture.transport.close();
  finishRegistration({ identifier: "late-script" });
  await nextTurn();
  assert.deepEqual(fixture.commands, [
    ["Page.removeScriptToEvaluateOnNewDocument", { identifier: "late-script" }, undefined],
  ]);
});

it("closes on cancellation or deadline and refuses an already expired action before attaching", async (testContext) => {
  const controller = new AbortController();
  const cancelled = await transportFixture(testContext, { signal: controller.signal });
  let closes = 0;
  cancelled.transport.onclose = () => {
    closes += 1;
  };
  controller.abort();
  assert.equal(closes, 1);
  assert.throws(() => cancelled.transport.send("{}"), /closed/);
  const expired = await transportFixture(testContext, { deadlineAt: Date.now() + 20 });
  expired.transport.onclose = () => {
    closes += 1;
  };
  await new Promise((resolve) => {
    setTimeout(resolve, 40);
  });
  assert.equal(closes, 2);
  await assert.rejects(
    createBrowserAutomationTransport({ debugger: {} }, { deadlineAt: 0 }),
    /expired/,
  );
});
