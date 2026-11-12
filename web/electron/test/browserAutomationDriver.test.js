"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { it } = require("node:test");
const { connect } = require("../src/browserAutomationDriver");

const PAGE_URL = "https://example.test/form";

// CDP initialization replies for one page, with real Puppeteer handling them.
function driverFixture() {
  const commands = [];
  const documentScripts = new Set();
  let nextScript = 0;
  const debuggerApi = Object.assign(new EventEmitter(), {
    isAttached: () => true,
    detach() {
      throw new Error("the driver must not detach its borrowed debugger");
    },
    async sendCommand(method, params, sessionId) {
      commands.push({ method, params, sessionId });
      if (method === "Page.getFrameTree") {
        return {
          frameTree: {
            frame: {
              id: "main-frame",
              loaderId: "main-loader",
              url: PAGE_URL,
              securityOrigin: "https://example.test",
              mimeType: "text/html",
            },
          },
        };
      }
      if (method === "Runtime.enable") {
        debuggerApi.emit("message", {}, "Runtime.executionContextCreated", {
          context: {
            id: 1,
            uniqueId: "main-world",
            origin: "https://example.test",
            name: "",
            auxData: { isDefault: true, frameId: "main-frame", type: "default" },
          },
        });
      }
      if (method === "Page.createIsolatedWorld") {
        debuggerApi.emit("message", {}, "Runtime.executionContextCreated", {
          context: {
            id: 2,
            uniqueId: "utility-world",
            origin: "https://example.test",
            name: params.worldName,
            auxData: { isDefault: false, frameId: "main-frame", type: "isolated" },
          },
        });
        return { executionContextId: 2 };
      }
      if (method === "Page.addScriptToEvaluateOnNewDocument") {
        const identifier = `script-${++nextScript}`;
        documentScripts.add(identifier);
        return { identifier };
      }
      if (method === "Page.removeScriptToEvaluateOnNewDocument") {
        documentScripts.delete(params.identifier);
      }
      if (method === "Runtime.callFunctionOn" || method === "Runtime.evaluate") {
        return { result: { type: "number", value: 42 } };
      }
      return {};
    },
  });
  const webContents = Object.assign(new EventEmitter(), {
    debugger: debuggerApi,
    getTitle: () => "Example form",
    getURL: () => PAGE_URL,
  });
  return { webContents, debuggerApi, commands, documentScripts };
}

it(
  "initializes a real Puppeteer Page and evaluates in its main execution context",
  { timeout: 2_000 },
  async (testContext) => {
    const fixture = driverFixture();
    const connection = await connect(fixture.webContents, fixture.debuggerApi, {
      deadlineAt: Date.now() + 1_000,
    });
    testContext.after(() => connection.disconnect());
    assert.equal(connection.page.url(), PAGE_URL);
    assert.equal(connection.page.mainFrame().url(), PAGE_URL);
    assert.equal(connection.page.frames().length, 1);
    assert.equal(await connection.page.evaluate(() => 6 * 7), 42);
    const evaluated = fixture.commands.find(
      (command) => command.method === "Runtime.callFunctionOn",
    );
    assert.equal(evaluated.params.executionContextId, 1);
    assert.equal(evaluated.sessionId, undefined);
    assert.ok(fixture.commands.some((command) => command.method === "Page.createIsolatedWorld"));
    assert.ok(fixture.documentScripts.size > 0);
    connection.disconnect();
    assert.equal(
      fixture.documentScripts.size,
      0,
      "the utility world's preload registration is released",
    );
    assert.equal(fixture.debuggerApi.listenerCount("message"), 0);
  },
);

it(
  "reuses a borrowed debugger without accumulating utility preload registrations",
  { timeout: 2_000 },
  async () => {
    const fixture = driverFixture();
    for (let action = 0; action < 2; action += 1) {
      // oxlint-disable-next-line no-await-in-loop -- These are successive actions on the same native view.
      const connection = await connect(fixture.webContents, fixture.debuggerApi, {
        deadlineAt: Date.now() + 1_000,
      });
      assert.equal(fixture.documentScripts.size, 1);
      connection.disconnect();
      assert.equal(fixture.documentScripts.size, 0);
    }
    assert.equal(
      fixture.commands.filter(
        (command) => command.method === "Page.removeScriptToEvaluateOnNewDocument",
      ).length,
      2,
    );
  },
);

it(
  "cleans up the connection when native page initialization fails",
  { timeout: 2_000 },
  async () => {
    const fixture = driverFixture();
    const sendCommand = fixture.debuggerApi.sendCommand;
    fixture.debuggerApi.sendCommand = (method, ...args) => {
      if (method === "Page.getFrameTree")
        return Promise.reject(new Error("frame tree unavailable"));
      return sendCommand(method, ...args);
    };
    await assert.rejects(
      connect(fixture.webContents, fixture.debuggerApi, { deadlineAt: Date.now() + 1_000 }),
      /frame tree unavailable/,
    );
    assert.equal(fixture.debuggerApi.listenerCount("message"), 0);
    assert.equal(fixture.documentScripts.size, 0);
  },
);
