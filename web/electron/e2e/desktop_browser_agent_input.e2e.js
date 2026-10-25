"use strict";

// Real Electron: the agent browser actions that need trusted input or page
// layout, through the renderer bridge -> main IPC -> DevTools-protocol path.
// Run from web/electron after `pnpm --filter web run build`:
//   OMNIGENT_PYTHON="$PWD/../../.venv/bin/python" node --test e2e/desktop_browser_agent_input.e2e.js

const { after, before, describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const {
  desktopDepsAvailable,
  execInBrowserView,
  launchDesktop,
  openBrowserFixture,
  spawnServer,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();
const VIEW = "agent-input";

const FIXTURE_PAGE = `<!doctype html>
<html><head><title>Agent input fixture</title><style>
  body { margin: 0; font: 14px sans-serif; }
  #menu-trigger:hover + #menu, #menu:hover { display: block; }
  #menu { display: none; position: absolute; left: 20px; top: 56px; background: #ffd; padding: 8px; }
  #drop, #lane { width: 160px; height: 60px; border: 2px dashed #888; }
  #scroller { height: 80px; overflow: auto; border: 1px solid #ccc; }
  #scroller div { height: 400px; }
  #spacer { height: 2000px; }
</style></head><body>
  <button id="menu-trigger" style="margin:20px">Menu</button>
  <div id="menu">Hover menu open</div>
  <div id="ctx" style="margin:20px;padding:8px;background:#eef">Right-click me</div>
  <button id="dbl">Double-click me</button>
  <div id="card" draggable="true" style="width:80px;padding:8px;background:#fcc">Card</div>
  <div id="drop">Drop zone</div>
  <div id="knob" style="width:40px;height:30px;background:#cfc;touch-action:none">Knob</div>
  <div id="lane">Pointer lane</div>
  <form id="form"><input id="q" aria-label="Query"></form>
  <select id="fruit"><option value="a">Apple</option><option value="b">Banana</option></select>
  <label><input id="agree" type="checkbox"> Agree</label>
  <div id="scroller"><div>Tall content</div></div>
  <div id="spacer"></div>
  <script>
    window.log = [];
    const note = (entry) => window.log.push(entry);
    ctx.addEventListener("contextmenu", (e) => { e.preventDefault(); note("contextmenu:" + e.isTrusted); });
    dbl.addEventListener("dblclick", (e) => note("dblclick:" + e.isTrusted));
    card.addEventListener("dragstart", (e) => e.dataTransfer.setData("text/plain", "card"));
    drop.addEventListener("dragover", (e) => e.preventDefault());
    drop.addEventListener("drop", (e) => { e.preventDefault(); drop.textContent = "Dropped " + e.dataTransfer.getData("text/plain"); });
    let dragging = false;
    knob.addEventListener("pointerdown", () => { dragging = true; });
    document.addEventListener("pointerup", (e) => {
      if (!dragging) return;
      dragging = false;
      if (document.elementFromPoint(e.clientX, e.clientY) === lane) lane.textContent = "Knob landed";
    });
    form.addEventListener("submit", (e) => { e.preventDefault(); note("submit:" + q.value); });
    q.addEventListener("keydown", (e) => { if (e.key === "Escape") note("escape"); });
  </script>
</body></html>`;

function startFixture() {
  const server = http.createServer((req, res) => {
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(req.url === "/second" ? "<title>Second</title><p>Second page</p>" : FIXTURE_PAGE);
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () =>
      resolve({
        url: `http://127.0.0.1:${server.address().port}/`,
        close: () =>
          new Promise((done) => {
            server.close(done);
          }),
      }),
    );
  });
}

/** The production renderer path the agent relay uses. */
function perform(window, action, params) {
  return window.evaluate(
    ({ id, name, args }) => window.omnigentDesktop.browserPerformAction(id, name, args),
    { id: VIEW, name: action, args: params },
  );
}

async function pageValue(window, js) {
  const r = await execInBrowserView(window, VIEW, js);
  assert.equal(r.ok, true, r.error);
  return r.result;
}

async function waitForPage(window, check) {
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    // oxlint-disable-next-line no-await-in-loop -- Poll until the view loads.
    const r = await execInBrowserView(window, VIEW, check);
    if (r.ok && r.result) return;
    // oxlint-disable-next-line no-await-in-loop -- Wait before the next probe.
    await window.waitForTimeout(200);
  }
  throw new Error(`page never satisfied: ${check}`);
}

describe(
  "desktop shell - agent browser input",
  { concurrency: 1, skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let server;
    let fixture;
    let desktop;

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-agent-input-"));
      [server, fixture] = await Promise.all([spawnServer(tmpDir), startFixture()]);
      desktop = await launchDesktop({
        serverUrl: server.serverUrl,
        recordDir: process.env.OMNIGENT_DESKTOP_RECORD_DIR || path.join(tmpDir, "recordings"),
      });
      const { window } = desktop;
      await window.waitForFunction(() => !!window.omnigentDesktop?.browserPerformAction);
      assert.equal((await openBrowserFixture(window, VIEW, fixture.url)).ok, true);
      await waitForPage(window, "document.title === 'Agent input fixture'");
    });

    after(async () => {
      try {
        await desktop?.electronApp.close();
      } finally {
        await desktop?.stopDisplayCapture?.();
        if (desktop?.userDataDir) fs.rmSync(desktop.userDataDir, { recursive: true, force: true });
        await Promise.all([server?.close(), fixture?.close()]);
        if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
      }
    });

    it(
      "hovers, right-clicks and double-clicks with trusted mouse input",
      { timeout: 60_000 },
      async () => {
        const { window } = desktop;
        assert.deepEqual(await perform(window, "hover", { selector: "#menu-trigger" }), {
          ok: true,
          result: JSON.stringify({ ok: true }),
        });
        assert.equal(
          await pageValue(window, "getComputedStyle(document.querySelector('#menu')).display"),
          "block",
        );
        assert.equal(
          (await perform(window, "click", { selector: "#ctx", button: "right" })).ok,
          true,
        );
        assert.equal(
          (await perform(window, "click", { selector: "#dbl", double_click: true })).ok,
          true,
        );
        assert.deepEqual(await pageValue(window, "window.log"), [
          "contextmenu:true",
          "dblclick:true",
        ]);
      },
    );

    it("drags HTML drag-and-drop and pointer-driven elements", { timeout: 60_000 }, async () => {
      const { window } = desktop;
      const html = await perform(window, "drag", {
        start_selector: "#card",
        end_selector: "#drop",
      });
      assert.deepEqual(JSON.parse(html.result), { ok: true, html_drag: true });
      assert.equal(await pageValue(window, "drop.textContent"), "Dropped card");
      const pointer = await perform(window, "drag", {
        start_selector: "#knob",
        end_selector: "#lane",
      });
      assert.deepEqual(JSON.parse(pointer.result), { ok: true, html_drag: false });
      assert.equal(await pageValue(window, "lane.textContent"), "Knob landed");
    });

    it("presses keys into the focused field", { timeout: 60_000 }, async () => {
      const { window } = desktop;
      await perform(window, "type", { selector: "#q", text: "omnigent" });
      assert.equal(
        (await perform(window, "press_key", { key: "Escape", selector: "#q" })).ok,
        true,
      );
      assert.equal((await perform(window, "press_key", { key: "Enter" })).ok, true);
      const log = await pageValue(window, "window.log");
      assert.deepEqual(log.slice(-2), ["escape", "submit:omnigent"]);
      const bad = await perform(window, "press_key", { key: "Hyper+x" });
      assert.match(bad.error, /unknown modifier/);
    });

    it(
      "selects options, checks boxes and scrolls the page or a container",
      { timeout: 60_000 },
      async () => {
        const { window } = desktop;
        const selected = await perform(window, "select_option", {
          selector: "#fruit",
          values: ["Banana"],
        });
        assert.deepEqual(JSON.parse(selected.result), { selected: ["b"] });
        const checked = await perform(window, "check", { selector: "#agree" });
        assert.deepEqual(JSON.parse(checked.result), { checked: true });
        const inner = JSON.parse(
          (await perform(window, "scroll", { direction: "down", selector: "#scroller div" }))
            .result,
        );
        assert.equal(inner.container, '<div#scroller> "Tall content"');
        assert.ok(inner.scroll_y > 0);
        await pageValue(window, "window.scrollTo(0, 0)");
        const page = JSON.parse(
          (await perform(window, "scroll", { direction: "down", amount: 300 })).result,
        );
        assert.deepEqual(
          { container: page.container, y: page.scroll_y, end: page.at_end },
          { container: "page", y: 300, end: false },
        );
      },
    );

    it(
      "refuses agent history navigation back to a loopback page",
      { timeout: 60_000 },
      async () => {
        const { window } = desktop;
        assert.equal((await openBrowserFixture(window, VIEW, `${fixture.url}second`)).ok, true);
        await waitForPage(window, "document.title === 'Second'");
        const back = await perform(window, "go_back", {});
        assert.equal(back.ok, false);
        assert.match(back.error, /loopback\/internal host/);
        assert.equal(await pageValue(window, "document.title"), "Second");
        const reload = await perform(window, "reload", {});
        assert.match(reload.error, /loopback\/internal host/);
      },
    );
  },
);
