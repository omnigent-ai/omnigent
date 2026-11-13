// Main stops an agent call at the deadline the relay sends with it.
const { it } = require("node:test");
const assert = require("node:assert/strict");

const { registerBrowserIpc } = require("../src/browserIpc");

it("never navigates once an agent navigation's caller has given up", async () => {
  const handlers = new Map();
  let answer;
  const opened = [];
  registerBrowserIpc({
    ipcMain: { handle: (channel, fn) => handlers.set(channel, fn) },
    isPinnedOriginSender: () => true,
    getRegistryForEvent: () => ({
      get: () => undefined,
      beginAgentNavigation: () => () => true,
      // The URL check (DNS) answers only when the test says so.
      checkAgentNavigation: () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
      openOrNavigate: (id, url) => {
        opened.push(`${id} ${url}`);
        return { ok: true, created: true };
      },
    }),
  });
  const navigate = (deadlineAt) =>
    handlers.get("omnigent:browser-open-or-navigate")(
      { sender: {} },
      {
        conversationId: "browser-tab:conv:new",
        url: "https://example.com/",
        opts: { agent: true, deadlineAt },
      },
    );
  const late = navigate(Date.now() + 20);
  await new Promise((resolve) => {
    setTimeout(resolve, 40);
  });
  answer({ ok: true });
  assert.equal((await late).ok, false);
  assert.deepEqual(opened, [], "nothing navigated after the deadline");
  // In time, and without a deadline, it navigates as before.
  const inTime = navigate(Date.now() + 5_000);
  answer({ ok: true });
  assert.equal((await inTime).ok, true);
  const unbounded = navigate(undefined);
  answer({ ok: true });
  assert.equal((await unbounded).ok, true);
  assert.equal(opened.length, 2);
});

const { EXPIRED, makeEntry, stall } = require("./browserInputFakes");

/** browserIpc with one view, `entry`, in the sender's window. */
function ipcWith(entry, registryExtras = {}) {
  const handlers = new Map();
  registerBrowserIpc({
    ipcMain: { handle: (channel, fn) => handlers.set(channel, fn) },
    isPinnedOriginSender: () => true,
    getRegistryForEvent: () => ({ get: () => entry, ...registryExtras }),
  });
  return (channel, args) =>
    handlers.get(channel)({ sender: {} }, { conversationId: "conv", ...args });
}

/** The call's answer and how long it took, in ms. */
async function timed(call) {
  const started = Date.now();
  const result = await call;
  return { result, ms: Date.now() - started };
}

it("stops each kind of action at the deadline its caller sent", async () => {
  const deadlineAt = () => Date.now() + 80;
  // A page paused in DevTools: no page script or protocol command answers.
  const paused = stall();
  const { entry } = makeEntry({ answer: () => paused.promise, reply: () => paused.promise });
  entry.view.webContents.capturePage = () => paused.promise;
  const call = ipcWith(entry);
  for (const [channel, args] of [
    // The standard snapshot script, trusted input, and a page-script action.
    [
      "omnigent:browser-perform-action",
      { action: "snapshot", params: { deadlineAt: deadlineAt() } },
    ],
    [
      "omnigent:browser-perform-action",
      { action: "hover", params: { selector: "#a", deadlineAt: deadlineAt() } },
    ],
    [
      "omnigent:browser-perform-action",
      {
        action: "select_option",
        params: { selector: "#s", values: ["a"], deadlineAt: deadlineAt() },
      },
    ],
    ["omnigent:browser-screenshot", { deadlineAt: deadlineAt() }],
  ]) {
    // oxlint-disable-next-line no-await-in-loop -- One action at a time, as the relay sends them.
    const { result, ms } = await timed(call(channel, args));
    assert.equal(result.ok, false, `${channel} ${args.action}`);
    assert.match(result.error, EXPIRED, `${channel} ${args.action}`);
    assert.ok(ms < 2_000, `${args.action ?? channel} took ${ms} ms`);
  }
  paused.resume("{}");
});

it("gives the standard scripts only their own arguments, and history the caller's deadline", async () => {
  const { entry, steps } = makeEntry({ answer: () => JSON.stringify({ tree: "page" }) });
  const history = [];
  const call = ipcWith(entry, {
    agentNavigateHistory: async (_id, action, opts) => {
      history.push([action, opts.deadlineAt]);
      return { ok: true, url: "https://a.example/", title: "A" };
    },
  });
  const deadlineAt = Date.now() + 5_000;
  const snapshot = await call("omnigent:browser-perform-action", {
    action: "snapshot",
    params: { deadlineAt },
  });
  assert.deepEqual(snapshot, { ok: true, result: JSON.stringify({ tree: "page" }) });
  assert.equal(steps.length, 1);
  await call("omnigent:browser-perform-action", { action: "go_back", params: { deadlineAt } });
  assert.deepEqual(history, [["go_back", deadlineAt]]);
});

it("calls back at the deadline by the clock, when its timer fires early", async (t) => {
  const { atAgentDeadline } = require("../src/agentDeadline");
  const { sleep } = require("./browserInputFakes");
  let clock = 0;
  t.mock.method(Date, "now", () => clock);
  let fired = 0;
  atAgentDeadline(20, () => {
    fired += 1;
  });
  // The timer fires at 20 ms, but the clock says the deadline hasn't come.
  await sleep(30);
  assert.equal(fired, 0);
  clock = 20;
  await sleep(40);
  assert.equal(fired, 1);
});

it("doesn't give up on work before its deadline by the clock, when its timer fires early", async (t) => {
  const { beforeAgentDeadline } = require("../src/agentDeadline");
  const { sleep } = require("./browserInputFakes");
  let clock = 0;
  t.mock.method(Date, "now", () => clock);
  let settled = "pending";
  beforeAgentDeadline(new Promise(() => {}), 20).catch(() => {
    settled = "timed out";
  });
  await sleep(30);
  assert.equal(settled, "pending");
  clock = 20;
  await sleep(40);
  assert.equal(settled, "timed out");
});
