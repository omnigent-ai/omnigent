// Live Edit on main's side (src/designTools.js): its scopes, their order and their end, and
// the page scripts it builds (src/designToolsPage.mjs), run in a real JSDOM page.

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { afterEach, beforeEach, describe, it } = require("node:test");
const { JSDOM } = require("jsdom");

const { runInPage } = require("./browserInputFakes");
const { createDesignTools } = require("../src/designTools");
const {
  liveEditForgetScript,
  liveEditPickScript,
  liveEditPreviewScript,
  liveEditRestoreScript,
  liveEditSettleScript,
} = require("../src/designToolsPage.mjs");

const tick = () =>
  new Promise((resolve) => {
    setImmediate(resolve);
  });
const wait = (ms) =>
  new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
/** Wait until `done()` holds, failing after `ms`. */
async function eventually(done, ms = 1000) {
  const until = Date.now() + ms;
  while (!done()) {
    if (Date.now() > until) throw new Error("timed out waiting");
    // oxlint-disable-next-line no-await-in-loop -- look again, a few ms on
    await wait(5);
  }
}

// Main's own timers don't keep the process alive: this does, while a test awaits one.
let alive;
beforeEach(() => {
  alive = setInterval(() => {}, 1000);
});
afterEach(() => clearInterval(alive));

const ENDED = { ok: false, error: "that Live Edit session has ended" };
const INVALID = { ok: false, error: "invalid design tools request" };

/** A tool open on a page whose heading is what every point hits; Live Edit begun on it. */
async function liveEdit(options = {}) {
  const { window } = new JSDOM("<body><h1 id=heading>Welcome back</h1></body>", {
    runScripts: "outside-only",
  });
  window.document.elementFromPoint = () => window.document.getElementById("heading");
  const held = [];
  const webContents = Object.assign(new EventEmitter(), {
    getZoomFactor: () => 1,
    isDestroyed: () => false,
    /** Hold the next script until the returned function is called. */
    holdNext: () => {
      let release;
      held.push(
        new Promise((resolve) => {
          release = resolve;
        }),
      );
      return () => release();
    },
    executeJavaScriptInIsolatedWorld: async (_world, [{ code }]) => {
      const hold = held.shift();
      if (hold) await hold;
      return runInPage(window, code);
    },
  });
  const view = Object.assign(new EventEmitter(), {
    webContents,
    getBounds: () => ({ x: 0, y: 0, width: 800, height: 600 }),
    getVisible: () => true,
  });
  const entry = { view };
  const owner = Object.assign(new EventEmitter(), { isDestroyed: () => false });
  const tools = createDesignTools(options);
  const t = {
    window,
    webContents,
    entry,
    owner,
    tools,
    session: null,
    start: () => {
      t.session = tools.start("view-1", entry, {}, { send: () => {}, owner }).session;
    },
    ask: (op, args = {}) => tools.handle(entry, { op, session: t.session, ...args }),
    end: (scope, args) => tools.handle(entry, { op: "liveEditEnd", scope, ...args }),
    heading: () => window.document.getElementById("heading").textContent,
  };
  t.start();
  t.scope = (await t.ask("liveEditBegin")).scope;
  return t;
}

describe("Live Edit: main's scopes", () => {
  it("runs a scope's operations in order, and nothing for it after its end", async () => {
    const t = await liveEdit();
    const picked = JSON.parse((await t.ask("liveEditPick", { scope: t.scope, x: 1, y: 1 })).result);
    assert.deepEqual([picked.handle, picked.before], [1, "Welcome back"]);
    await t.ask("liveEditPreview", { scope: t.scope, handle: 1, text: "Hello" });
    assert.equal(t.heading(), "Hello");
    assert.equal((await t.end(t.scope, { texts: [[1, "Hi"]] })).ok, true);
    assert.equal(t.heading(), "Hi", "the end shows the latest text");
    assert.deepEqual(
      await t.ask("liveEditPreview", { scope: t.scope, handle: 1, text: "Late" }),
      ENDED,
    );
    assert.deepEqual(await t.end(t.scope, { texts: null }), ENDED);
    assert.equal(t.heading(), "Hi");
    assert.equal(t.window.omnigentLiveEdit[t.scope], undefined, "the page forgot it");
  });

  it("skips what was still waiting when the end was asked for", async () => {
    const t = await liveEdit();
    await t.ask("liveEditPick", { scope: t.scope, x: 1, y: 1 });
    const release = t.webContents.holdNext();
    const running = t.ask("liveEditPreview", { scope: t.scope, handle: 1, text: "One" });
    await tick();
    const waiting = t.ask("liveEditPreview", { scope: t.scope, handle: 1, text: "Two" });
    const end = t.end(t.scope, { texts: [[1, "Three"]] });
    release();
    assert.equal((await running).ok, true);
    assert.deepEqual(await waiting, ENDED);
    assert.equal((await end).ok, true);
    assert.equal(t.heading(), "Three");
  });

  it("forgets, without settling, after Discard; puts the page back for a Discard cut short", async () => {
    const t = await liveEdit();
    await t.ask("liveEditPick", { scope: t.scope, x: 1, y: 1 });
    await t.ask("liveEditPreview", { scope: t.scope, handle: 1, text: "Hello" });
    assert.equal((await t.ask("liveEditRestore", { scope: t.scope })).result, "1");
    await t.end(t.scope, { texts: null });
    assert.equal(t.heading(), "Welcome back");
    assert.equal(t.window.omnigentLiveEdit[t.scope], undefined);

    const next = (await t.ask("liveEditBegin")).scope;
    await t.ask("liveEditPick", { scope: next, x: 1, y: 1 });
    await t.ask("liveEditPreview", { scope: next, handle: 1, text: "Edited" });
    t.tools.stop(t.entry, t.session);
    assert.equal((await t.end(next, { texts: null, restore: true })).ok, true);
    assert.equal(t.heading(), "Welcome back");
    assert.equal(t.window.omnigentLiveEdit[next], undefined);
  });

  it("takes no more work for a closed opening's scope, but still its end", async () => {
    const t = await liveEdit();
    await t.ask("liveEditPick", { scope: t.scope, x: 1, y: 1 });
    t.webContents.emit("did-navigate-in-page", {}, "https://example.test/#b", true);
    assert.deepEqual(await t.ask("liveEditPick", { scope: t.scope, x: 1, y: 1 }), {
      ok: false,
      error: "the design tool isn't open on this page",
    });
    t.start();
    assert.deepEqual(await t.ask("liveEditPick", { scope: t.scope, x: 1, y: 1 }), ENDED);
    assert.equal((await t.end(t.scope, { texts: [[1, "Typed last"]] })).ok, true);
    assert.equal(t.heading(), "Typed last");
    assert.equal(t.window.omnigentLiveEdit[t.scope], undefined);
  });

  it("runs a newer scope's work only once older scopes have ended", async () => {
    const t = await liveEdit();
    await t.ask("liveEditPick", { scope: t.scope, x: 1, y: 1 });
    await t.ask("liveEditPreview", { scope: t.scope, handle: 1, text: "Old preview" });
    t.tools.stop(t.entry, t.session);
    t.start();
    const next = (await t.ask("liveEditBegin")).scope;
    const picking = t.ask("liveEditPick", { scope: next, x: 1, y: 1 });
    await tick();
    assert.equal(t.window.omnigentLiveEdit[next], undefined, "it waits for the old end");
    assert.equal((await t.end(t.scope, { texts: [[1, "Old final"]] })).ok, true);
    const picked = JSON.parse((await picking).result);
    assert.equal(picked.before, "Old final", "it starts from the page as the old end left it");
  });

  it("forgets a closed opening's scope the app never ends, once it's been idle a while", async () => {
    const t = await liveEdit({ endGraceMs: 300 });
    await t.ask("liveEditPick", { scope: t.scope, x: 1, y: 1 });
    const release = t.webContents.holdNext();
    const running = t.ask("liveEditPreview", { scope: t.scope, handle: 1, text: "Slow" });
    await tick();
    t.tools.stop(t.entry, t.session);
    await wait(50);
    assert.ok(t.window.omnigentLiveEdit[t.scope], "kept while its work runs");
    release();
    await running;
    await wait(40);
    assert.ok(t.window.omnigentLiveEdit[t.scope], "kept a while after its last operation");
    assert.equal((await t.end(t.scope, { texts: [[1, "Last"]] })).ok, true, "so its end lands");
    assert.equal(t.heading(), "Last");

    const u = await liveEdit({ endGraceMs: 5 });
    await u.ask("liveEditPick", { scope: u.scope, x: 1, y: 1 });
    u.tools.stop(u.entry, u.session);
    await wait(100);
    assert.equal(u.window.omnigentLiveEdit[u.scope], undefined);
  });

  it("forgets the scopes of an app that went, at once", async () => {
    const t = await liveEdit();
    await t.ask("liveEditPick", { scope: t.scope, x: 1, y: 1 });
    await t.ask("liveEditPreview", { scope: t.scope, handle: 1, text: "Kept" });
    t.owner.emit("destroyed");
    await eventually(() => t.window.omnigentLiveEdit[t.scope] === undefined);
    assert.equal(t.heading(), "Kept");
  });

  it("settles the edits it can, the latest of each, and refuses an end naming too many", async () => {
    const t = await liveEdit();
    await t.ask("liveEditPick", { scope: t.scope, x: 1, y: 1 });
    await t.ask("liveEditPreview", { scope: t.scope, handle: 1, text: "Preview" });
    const texts = Array.from({ length: 1001 }, (_, index) => [index + 1, "Final"]);
    assert.deepEqual(await t.end(t.scope, { texts }), INVALID);
    assert.equal(t.heading(), "Preview", "nothing settled");
    const answer = await t.end(t.scope, {
      texts: [
        [1, "x".repeat(100_001)],
        ["2", "not a handle"],
        [1, "Kept"],
      ],
    });
    assert.equal(answer.ok, true);
    assert.equal(t.heading(), "Kept");
  });

  it("keeps a scope whose end the page didn't take, for the app to ask again", async () => {
    const t = await liveEdit({ opTimeoutMs: 20 });
    await t.ask("liveEditPick", { scope: t.scope, x: 1, y: 1 });
    t.webContents.holdNext();
    await assert.rejects(t.end(t.scope, { texts: [[1, "First try"]] }), /the page didn't answer/);
    // A newer scope's work waits for it, still.
    const next = (await t.ask("liveEditBegin")).scope;
    let picked = false;
    const pick = t.ask("liveEditPick", { scope: next, x: 1, y: 1 }).then((result) => {
      picked = true;
      return result;
    });
    await wait(5);
    assert.equal(picked, false);
    assert.equal((await t.end(t.scope, { texts: [[1, "Second try"]] })).ok, true);
    assert.equal(t.heading(), "Second try");
    assert.equal((await pick).ok, true);
  });

  it("puts the page back, once it's idle, for a Discard whose end the page didn't take", async () => {
    const t = await liveEdit({ opTimeoutMs: 20, endGraceMs: 30 });
    await t.ask("liveEditPick", { scope: t.scope, x: 1, y: 1 });
    await t.ask("liveEditPreview", { scope: t.scope, handle: 1, text: "Previewed" });
    t.tools.stop(t.entry, t.session);
    t.webContents.holdNext();
    await assert.rejects(t.end(t.scope, { texts: null, restore: true }), /the page didn't answer/);
    assert.equal(t.heading(), "Previewed");
    await eventually(() => t.window.omnigentLiveEdit[t.scope] === undefined);
    assert.equal(t.heading(), "Welcome back");
  });

  it("refuses malformed requests, and a scope it didn't begin", async () => {
    const t = await liveEdit();
    for (const args of [
      { op: "liveEditPick", scope: t.scope, x: "1); window.taken = 1; (0", y: 1 },
      { op: "liveEditPreview", scope: t.scope, handle: "1", text: "x" },
      { op: "liveEditPreview", scope: t.scope, handle: 1, text: 7 },
      { op: "liveEditEnd", scope: t.scope, texts: "x" },
    ]) {
      // oxlint-disable-next-line no-await-in-loop -- one request at a time
      assert.deepEqual(await t.tools.handle(t.entry, { session: t.session, ...args }), INVALID);
    }
    assert.equal(t.window.taken, undefined);
    assert.deepEqual(await t.ask("liveEditPick", { scope: "s_unknown", x: 1, y: 1 }), ENDED);
    assert.deepEqual(await t.end("s_unknown", { texts: null }), ENDED);
  });
});

describe("Live Edit: page scripts", () => {
  const SCOPE = "s1_test";

  /** A page with plain text, formatted text, text in two nodes, and a text area. */
  function page() {
    const { window } = new JSDOM(
      '<body><p id="title">Old title</p><p id="rich">Hello <strong id="bold">world</strong></p>' +
        '<p id="split"></p><textarea id="area">Draft</textarea></body>',
      { runScripts: "outside-only" },
    );
    const { document } = window;
    document.getElementById("split").append("Hello, ", "Ada");
    const byId = (id) => document.getElementById(id);
    const run = (script) => runInPage(window, script);
    const pick = (el, scope = SCOPE) => {
      document.elementFromPoint = () => el;
      return JSON.parse(run(liveEditPickScript(scope, 1, 1)));
    };
    const preview = (handle, text, scope = SCOPE) =>
      run(liveEditPreviewScript(scope, handle, text));
    return { window, document, byId, run, pick, preview };
  }

  it("picks the nearest element with text of its own, and describes it", () => {
    const p = page();
    assert.deepEqual(
      (({ before, text, element }) => ({ before, text, tag: element.tag }))(p.pick(p.byId("bold"))),
      { before: "world", text: "world", tag: "strong" },
    );
    p.document.body.insertAdjacentHTML("beforeend", '<section id="empty"><div></div></section>');
    assert.equal(p.pick(p.byId("empty").firstElementChild), null);
  });

  it("says where in the text the click landed, for the editor's cursor", () => {
    const p = page();
    const split = p.byId("split");
    const [, ada] = [...split.childNodes];
    p.document.caretPositionFromPoint = () => ({ offsetNode: ada, offset: 1 });
    assert.equal(p.pick(split).caret, 8);
    // Off the element's own text: no caret.
    p.document.caretPositionFromPoint = () => ({ offsetNode: p.byId("title").firstChild });
    assert.equal(p.pick(split).caret, -1);
    // An engine with only the older API.
    delete p.document.caretPositionFromPoint;
    p.document.caretRangeFromPoint = () => ({ startContainer: ada, startOffset: 3 });
    assert.equal(p.pick(split).caret, 10);
  });

  it("refuses text with elements inside it, and text that isn't shown as text", () => {
    const p = page();
    const media = p.document.createElement("video");
    p.byId("title").append(media);
    assert.deepEqual(p.pick(p.byId("title")), { nested: true });
    assert.deepEqual(p.pick(p.byId("rich")), { nested: true });
    assert.deepEqual(p.pick(p.byId("area")), { nested: true });
    assert.equal(p.byId("title").contains(media), true, "nothing was taken out of the page");
  });

  it("changes only the text of an element's own nodes, and puts each one's back", () => {
    const p = page();
    const split = p.byId("split");
    const [first, second] = [...split.childNodes];
    const { handle, before } = p.pick(split);
    assert.equal(before, "Hello, Ada");
    assert.equal(p.preview(handle, "Hi there"), "ok");
    assert.equal(split.textContent, "Hi there");
    assert.deepEqual([...split.childNodes], [first, second]);
    p.preview(handle, "Bye");
    assert.equal(p.run(liveEditRestoreScript(SCOPE)), "1");
    assert.deepEqual([first.data, second.data], ["Hello, ", "Ada"]);
  });

  it("leaves text the page changed itself after the pick alone", () => {
    const p = page();
    const title = p.byId("title");
    const { handle } = p.pick(title);
    p.preview(handle, "Mine");
    title.firstChild.data = "Page's own";
    assert.equal(p.preview(handle, "Mine again"), "gone");
    assert.equal(p.run(liveEditRestoreScript(SCOPE)), "1");
    assert.equal(title.textContent, "Page's own");
    const bold = p.pick(p.byId("bold"));
    p.byId("bold").replaceChildren("Fresh");
    p.run(liveEditSettleScript(SCOPE, [[bold.handle, "Late"]]));
    assert.equal(p.byId("bold").textContent, "Fresh");
  });

  it("picks the same edit again, and again after its text was cleared", () => {
    const p = page();
    const title = p.byId("title");
    const { handle } = p.pick(title);
    assert.equal(p.pick(title).handle, handle);
    p.preview(handle, "");
    const again = p.pick(title);
    assert.deepEqual([again.handle, again.before, again.text], [handle, "Old title", ""]);
  });

  it("picks text inside an open shadow root", () => {
    const p = page();
    const host = p.document.createElement("x-label");
    p.document.body.append(host);
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = '<span id="label">Shadow text</span>';
    const label = root.getElementById("label");
    root.elementFromPoint = () => label;
    const picked = p.pick(host);
    assert.equal(picked.before, "Shadow text");
    p.preview(picked.handle, "New");
    assert.equal(label.textContent, "New");
  });

  it("keeps the new text when it forgets, and says when an element is gone", () => {
    const p = page();
    const title = p.byId("title");
    p.preview(p.pick(title).handle, "Kept");
    p.run(liveEditForgetScript(SCOPE));
    assert.equal(title.textContent, "Kept");
    assert.equal(p.run(liveEditRestoreScript(SCOPE)), "none");
    const again = p.pick(title);
    title.remove();
    assert.equal(p.preview(again.handle, "Lost"), "gone");
  });

  it("refuses an element an older session's state still holds, until that session ends", () => {
    const p = page();
    const old = p.pick(p.byId("bold"));
    p.preview(old.handle, "old bold");
    assert.deepEqual(p.pick(p.byId("bold"), "s2_test"), { busy: true });
    assert.equal(p.pick(p.byId("title"), "s2_test").before, "Old title");
    p.run(liveEditSettleScript(SCOPE, [[old.handle, "latest bold"]]));
    assert.equal(p.byId("bold").textContent, "latest bold");
    assert.equal(p.pick(p.byId("bold"), "s2_test").text, "latest bold");
  });

  it("refuses text too long to edit, and more elements than a session's end settles", () => {
    const p = page();
    p.byId("title").firstChild.data = "x".repeat(100_001);
    assert.deepEqual(p.pick(p.byId("title")), { tooLong: true });
    p.document.body.innerHTML = Array.from(
      { length: 1001 },
      (_, i) => `<p id="p${i}">Line ${i}</p>`,
    ).join("");
    for (let i = 0; i < 1000; i += 1) assert.ok(p.pick(p.byId(`p${i}`)).handle);
    assert.deepEqual(p.pick(p.byId("p1000")), { tooMany: true });
    assert.equal(p.pick(p.byId("p0")).handle, 1, "one already picked can still be edited");
  });

  it("never makes the page editable", () => {
    const p = page();
    p.preview(p.pick(p.byId("title")).handle, "New");
    assert.equal(p.document.querySelector("[contenteditable]"), null);
  });
});
