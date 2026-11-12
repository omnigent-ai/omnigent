"use strict";

// Real Electron: the agent browser actions that need trusted input or page
// layout, through the renderer bridge -> main IPC -> DevTools-protocol path.
// Run from web/electron after `pnpm --filter web run build`:
//   OMNIGENT_PYTHON="$PWD/../../.venv/bin/python" node --test e2e/desktop_browser_agent_input.e2e.js

const { after, before, describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  desktopDepsAvailable,
  execInBrowserView,
  launchDesktop,
  openBrowserFixture,
  spawnServer,
} = require("./desktopHarness");
const { localPreviewsPreload, startFixture } = require("./agentInputFixtures");

const deps = desktopDepsAvailable();
const VIEW = "agent-input";

/** The production renderer path the agent relay uses. */
// Typing asks for real text input, as the relay does in sessions started with the tools;
// `standard` sends it as a session without them.
function perform(window, action, params, { standard = false } = {}) {
  const args =
    action === "type" && !standard ? { ...params, provenance: { realInput: true } } : params;
  return window.evaluate(
    ({ id, name, args: sent }) => window.omnigentDesktop.browserPerformAction(id, name, sent),
    { id: VIEW, name: action, args },
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
        preload: [localPreviewsPreload(tmpDir)],
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

    it(
      "drops on the column a dragover moved, not what took its place",
      { timeout: 60_000 },
      async () => {
        const { window } = desktop;
        assert.equal((await openBrowserFixture(window, VIEW, `${fixture.url}sortable`)).ok, true);
        await waitForPage(window, "document.title === 'Sortable'");
        const result = await perform(window, "drag", {
          start_selector: "#item",
          end_selector: "#column",
        });
        assert.equal(result.ok, true, result.error);
        assert.deepEqual(await pageValue(window, "window.drops"), ["column"]);
        assert.equal((await openBrowserFixture(window, VIEW, fixture.url)).ok, true);
        await waitForPage(window, "document.title === 'Agent input fixture' && !!window.tap");
      },
    );

    it(
      "refuses the key when a modifier's handler moves focus or focuses a select",
      { timeout: 60_000 },
      async () => {
        const { window } = desktop;
        // Targeted, the focus check catches the move; untargeted (the key
        // goes wherever focus is), the select check after Alt does.
        for (const [args, error] of [
          [{ key: "Alt+a", selector: "#alt-moves" }, /moved focus to another element/],
          [{ key: "Alt+ArrowDown" }, /on a native <select> would open its popup/],
        ]) {
          // oxlint-disable-next-line no-await-in-loop -- One key at a time.
          await pageValue(
            window,
            "window.keys.length = 0; document.getElementById('alt-picks').focus(); 0",
          );
          // oxlint-disable-next-line no-await-in-loop -- One key at a time.
          const result = await perform(window, "press_key", args);
          assert.equal(result.ok, false, `${args.key} reported success`);
          assert.match(result.error, error, args.key);
          // oxlint-disable-next-line no-await-in-loop -- Checked per key.
          const keys = await pageValue(window, "window.keys");
          assert.deepEqual(
            keys.map((k) => k.split("@")[0]),
            ["down:Alt", "up:Alt"],
            `${args.key}: only Alt went down, and it was released`,
          );
        }
        // Leave the select unfocused.
        await pageValue(window, "document.activeElement.blur(); 0");
      },
    );

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
        assert.deepEqual(JSON.parse(checked.result), { ok: true, checked: true });
        const inner = JSON.parse(
          (await perform(window, "scroll", { direction: "down", selector: "#scroller div" }))
            .result,
        );
        assert.equal(inner.container, '<div#scroller> "Tall content"');
        assert.ok(inner.scroll_y > 0);
        // A container already at its end: a wheel there would scroll the page.
        const beforeScroll = await pageValue(
          window,
          "scroller.scrollTop = scroller.scrollHeight; " +
            "scroller.scrollIntoView({ block: 'center', behavior: 'instant' }); " +
            "[window.scrollY, window.trusted.length]",
        );
        const atEnd = JSON.parse(
          (await perform(window, "scroll", { direction: "down", selector: "#scroller div" }))
            .result,
        );
        assert.equal(atEnd.at_end, true);
        await new Promise((resolve) => {
          setTimeout(resolve, 300);
        });
        assert.deepEqual(
          await pageValue(window, "[window.scrollY, window.trusted.length]"),
          beforeScroll,
          "the page did not move and no wheel was sent",
        );
        await pageValue(window, "window.scrollTo(0, 0)");
        const page = JSON.parse(
          (await perform(window, "scroll", { direction: "down", amount: 300 })).result,
        );
        assert.deepEqual(
          { container: page.container, y: page.scroll_y, end: page.at_end },
          { container: "page", y: 300, end: false },
        );
        const wheels = (await pageValue(window, "window.trusted")).filter((e) =>
          e.startsWith("wheel"),
        );
        assert.ok(wheels.length >= 2 && wheels.every((e) => e.endsWith(":true")), wheels.join());
      },
    );

    it(
      "clicks, types and checks with trusted input, replacing what a field held",
      { timeout: 60_000 },
      async () => {
        const { window } = desktop;
        await pageValue(window, "window.trusted.length = 0; q.value = 'stale'; 0");
        assert.equal((await perform(window, "click", { selector: "#tap" })).ok, true);
        assert.equal((await perform(window, "type", { selector: "#q", text: "fresh" })).ok, true);
        assert.equal(
          (await perform(window, "type", { selector: "#editor", text: "Hello editor" })).ok,
          true,
        );
        assert.equal((await perform(window, "type", { selector: "#qty", text: "12" })).ok, true);
        assert.equal(
          (await perform(window, "type", { selector: "#when", text: "2026-03-04" })).ok,
          true,
        );
        const fancy = await perform(window, "check", { selector: "#fancy" });
        assert.deepEqual(JSON.parse(fancy.result), { ok: true, checked: true });
        // A hidden box whose label has a link at its center: the box changes,
        // the link doesn't open; a label that's all link is refused.
        const terms = await perform(window, "check", { selector: "#terms" });
        assert.equal(terms.ok, true, terms.error);
        assert.equal(await pageValue(window, "document.querySelector('#terms').checked"), true);
        assert.equal(await pageValue(window, "location.hash"), "");
        const linksOnly = await perform(window, "check", { selector: "#links-box" });
        assert.match(linksOnly.error, /label are links or other controls/);
        assert.equal(await pageValue(window, "location.hash"), "");
        assert.equal(
          await pageValue(window, "document.querySelector('#links-box').checked"),
          false,
        );
        assert.deepEqual(
          await pageValue(
            window,
            "[q.value, editor.textContent, qty.value, document.querySelector('#when').value, fancy.checked]",
          ),
          ["fresh", "Hello editor", "12", "2026-03-04", true],
        );
        const trusted = await pageValue(window, "window.trusted");
        assert.deepEqual(
          trusted.filter((e) => !e.startsWith("wheel")),
          [
            "pointerdown#tap:true",
            "mousedown#tap:true",
            "click#tap:true",
            "beforeinput#q:true",
            "input#q:true",
            // Moving on blurs the field, so the browser itself fires change.
            "change#q:true",
            "beforeinput#editor:true",
            "input#editor:true",
            // The hidden input is clicked through its label, as a person would.
            "click#fancy:true",
          ],
        );
      },
    );

    it(
      "types in a standard session with the page's input and change at once",
      { timeout: 60_000 },
      async () => {
        const { window } = desktop;
        await pageValue(window, "window.trusted.length = 0; q.value = 'stale'; 0");
        const typed = await perform(
          window,
          "type",
          { selector: "#q", text: "plain" },
          { standard: true },
        );
        assert.equal(typed.ok, true, typed.error);
        // Read straight after typing, with the field still focused.
        assert.deepEqual(await pageValue(window, "[q.value, window.trusted]"), [
          "plain",
          ["input#q:false", "change#q:false"],
        ]);
        await pageValue(window, "document.activeElement.blur(); 0");
      },
    );

    it(
      "refuses covered elements and native pickers, and cancels file pickers",
      { timeout: 60_000 },
      async () => {
        const { window } = desktop;
        const covered = await perform(window, "click", { selector: "#buried" });
        assert.match(covered.error, /covered by <div#sheet> "sheet"/);
        const select = await perform(window, "click", { selector: "#fruit" });
        assert.match(select.error, /set it with browser_type/);
        // The always-available path for a native dropdown, with the switch off.
        const typed = await perform(window, "type", { selector: "#fruit", text: "Banana" });
        assert.equal(typed.ok, true, typed.error);
        assert.equal(await pageValue(window, "fruit.value"), "b");
        const date = await perform(window, "click", { selector: "#when" });
        assert.match(date.error, /<input type=date> picker — use browser_type/);
        // A wrapper centered on a select, and a label for a color input.
        const wrapped = await perform(window, "click", { selector: "#fruit-wrap" });
        assert.match(wrapped.error, /the click would land on a native <select>/);
        const label = await perform(window, "click", { selector: "#hue-label" });
        assert.match(label.error, /the click would land on an <input type=color> picker/);
        // The keyboard can't open them either.
        const space = await perform(window, "press_key", { key: "Space", selector: "#fruit" });
        assert.match(space.error, /would open its popup/);
        const down = await perform(window, "press_key", { key: "ArrowDown" });
        assert.match(down.error, /would open its popup/);
        // Values Chromium would replace are refused, leaving the field as it was.
        assert.equal(
          (await perform(window, "type", { selector: "#when", text: "2026-05-06" })).ok,
          true,
        );
        const badDate = await perform(window, "type", { selector: "#when", text: "May 6" });
        assert.match(badDate.error, /takes a value like YYYY-MM-DD/);
        assert.equal(
          await pageValue(window, "document.querySelector('#when').value"),
          "2026-05-06",
        );
        const hidden = await perform(window, "type", { selector: "#hidden-select", text: "b" });
        assert.match(hidden.error, /the select is hidden/);
        assert.equal(
          await pageValue(window, "document.querySelector('#hidden-select').value"),
          "a",
        );
        const file = await perform(window, "click", { selector: "#file" });
        assert.match(file.error, /opened a file picker, which was cancelled/);
        // Nothing is left holding the debugger: the next action still runs.
        assert.equal((await perform(window, "hover", { selector: "#tap" })).ok, true);
      },
    );

    it(
      "keeps a file picker a page opens a second after the click off the screen",
      { timeout: 60_000 },
      async () => {
        const { window, electronApp } = desktop;
        // Main hears of a file chooser only while choosers are intercepted:
        // one that isn't would be the OS dialog instead.
        await electronApp.evaluate(({ webContents }, url) => {
          const wc = webContents.getAllWebContents().find((c) => c.getURL() === url);
          globalThis.interceptedChoosers = [];
          wc.debugger.on("message", (_event, method) => {
            if (method === "Page.fileChooserOpened") globalThis.interceptedChoosers.push(method);
          });
        }, fixture.url);
        const setDevTools = (open) =>
          electronApp.evaluate(
            async ({ webContents }, { url, open: want }) => {
              const wc = webContents.getAllWebContents().find((c) => c.getURL() === url);
              if (want === wc.isDevToolsOpened()) return;
              await new Promise((resolve) => {
                wc.once(want ? "devtools-opened" : "devtools-closed", resolve);
                if (want) wc.openDevTools({ mode: "detach" });
                else wc.closeDevTools();
              });
            },
            { url: fixture.url, open },
          );
        try {
          for (const devtools of [false, true]) {
            // oxlint-disable-next-line no-await-in-loop -- One setup at a time.
            await setDevTools(devtools);
            // oxlint-disable-next-line no-await-in-loop -- One setup at a time.
            await pageValue(window, "window.lateUploads.length = 0; 0");
            for (const ms of [500, 1000]) {
              // oxlint-disable-next-line no-await-in-loop -- One click at a time.
              const clicked = await perform(window, "click", { selector: `#late-upload-${ms}` });
              assert.equal(clicked.ok, true, clicked.error);
            }
            // A hover before the timers fire gives no activation of its own,
            // and keeps the clicks' interception on.
            // oxlint-disable-next-line no-await-in-loop -- Before the timers fire.
            const hovered = await perform(window, "hover", { selector: "#tap" });
            assert.equal(hovered.ok, true, hovered.error);
            // oxlint-disable-next-line no-await-in-loop -- Wait for both timers.
            await waitForPage(window, "window.lateUploads.length === 2");
            // oxlint-disable-next-line no-await-in-loop -- Let the choosers open.
            await new Promise((resolve) => {
              setTimeout(resolve, 300);
            });
            assert.deepEqual(
              // oxlint-disable-next-line no-await-in-loop -- Checked per setup.
              await pageValue(window, "window.lateUploads"),
              ["500:true", "1000:true"],
              "each picker opened on the agent click's activation",
            );
            assert.equal(
              // oxlint-disable-next-line no-await-in-loop -- Checked per setup.
              await electronApp.evaluate(() => globalThis.interceptedChoosers.splice(0).length),
              2,
              `both pickers intercepted (DevTools ${devtools ? "open" : "closed"})`,
            );
          }
        } finally {
          await setDevTools(false);
        }
      },
    );

    it("works with the user's DevTools open on the page", { timeout: 60_000 }, async () => {
      const { window, electronApp } = desktop;
      const devtools = (wantOpen) =>
        electronApp.evaluate(
          async ({ webContents }, { url, wantOpen: open }) => {
            const wc = webContents.getAllWebContents().find((c) => c.getURL() === url);
            if (!wc) throw new Error(`no view at ${url}`);
            if (open === wc.isDevToolsOpened()) return open;
            await new Promise((resolve) => {
              wc.once(open ? "devtools-opened" : "devtools-closed", resolve);
              if (open) wc.openDevTools({ mode: "detach" });
              else wc.closeDevTools();
            });
            return wc.isDevToolsOpened();
          },
          { url: fixture.url, wantOpen },
        );
      assert.equal(await devtools(true), true);
      try {
        await pageValue(window, "window.trusted.length = 0; 0");
        assert.equal((await perform(window, "click", { selector: "#tap" })).ok, true);
        assert.equal(
          (await perform(window, "type", { selector: "#q", text: "devtools" })).ok,
          true,
        );
        assert.equal((await perform(window, "press_key", { key: "Enter" })).ok, true);
        const trusted = await pageValue(window, "window.trusted");
        assert.ok(
          trusted.includes("click#tap:true") && trusted.includes("input#q:true"),
          trusted.join(),
        );
        assert.equal((await pageValue(window, "window.log")).at(-1), "submit:devtools");
        assert.equal(
          await electronApp.evaluate(
            ({ webContents }, url) =>
              webContents
                .getAllWebContents()
                .find((c) => c.getURL() === url)
                .isDevToolsOpened(),
            fixture.url,
          ),
          true,
          "the user's DevTools stay open",
        );
      } finally {
        await devtools(false);
      }
    });

    it(
      "checks and types into off-screen controls on smooth-scrolling pages",
      { timeout: 60_000 },
      async () => {
        const { window } = desktop;
        assert.equal((await openBrowserFixture(window, VIEW, `${fixture.url}smooth`)).ok, true);
        await waitForPage(window, "document.title === 'Smooth'");
        for (const selector of ["#far-check", "#inner-check"]) {
          // oxlint-disable-next-line no-await-in-loop -- One action at a time.
          const checked = await perform(window, "check", { selector });
          assert.equal(checked.ok, true, `${selector}: ${checked.error}`);
        }
        assert.equal(
          await pageValue(
            window,
            "[window['far-check'].checked, window['inner-check'].checked].join()",
          ),
          "true,true",
        );
        const typed = await perform(window, "type", { selector: "#inner-field", text: "hello" });
        assert.equal(typed.ok, true, typed.error);
        assert.equal(await pageValue(window, "window['inner-field'].value"), "hello");
      },
    );

    it(
      "types nothing into a page that replaced the one it found the field in",
      { timeout: 60_000 },
      async () => {
        const { window } = desktop;
        assert.equal((await openBrowserFixture(window, VIEW, `${fixture.url}handoff`)).ok, true);
        await waitForPage(window, "document.title === 'Handoff'");
        const typed = await perform(window, "type", { selector: "#q", text: "secret" });
        assert.equal(typed.ok, false, "reported as typed");
        assert.match(typed.error, /the page changed \(it navigated\)/);
        // The agent's input set off the navigation, so it gets the agent URL
        // policy, which refuses this loopback page; either way, nothing typed.
        await window.waitForTimeout(500);
        assert.equal(await pageValue(window, "(window.q || window.landing).value"), "");
      },
    );

    it(
      "types and presses nothing into a field the page moved focus to",
      { timeout: 60_000 },
      async () => {
        const { window } = desktop;
        assert.equal(
          (await openBrowserFixture(window, VIEW, `${fixture.url}focus-thief`)).ok,
          true,
        );
        await waitForPage(window, "document.title === 'Focus thief'");
        const typed = await perform(window, "type", { selector: "#wanted", text: "secret" });
        assert.equal(typed.ok, false, "reported as typed");
        assert.match(typed.error, /moved focus to another element before typing/);
        assert.equal(await pageValue(window, "window.other.value"), "");
        assert.equal(await pageValue(window, "window.wanted.value"), "");
        for (const key of ["x", "Enter"]) {
          // oxlint-disable-next-line no-await-in-loop -- One key at a time.
          const pressed = await perform(window, "press_key", { selector: "#wanted", key });
          assert.equal(pressed.ok, false, `${key} reported as pressed`);
          assert.match(pressed.error, /moved focus to another element before the key press/);
        }
        assert.equal(await pageValue(window, "window.other.value"), "");
      },
    );

    it(
      "replaces a rich-text editor's whole content even when it moves the caret",
      { timeout: 60_000 },
      async () => {
        const { window } = desktop;
        assert.equal(
          (await openBrowserFixture(window, VIEW, `${fixture.url}caret-editor`)).ok,
          true,
        );
        await waitForPage(window, "document.title === 'Caret editor'");
        const typed = await perform(window, "type", { selector: "#ed", text: "new" });
        assert.equal(typed.ok, true, typed.error);
        assert.equal(await pageValue(window, "window.ed.textContent"), "new");
        const cleared = await perform(window, "type", { selector: "#ed", text: "" });
        assert.equal(cleared.ok, true, cleared.error);
        assert.equal(await pageValue(window, "window.ed.textContent"), "");
      },
    );

    it("doesn't click what a hover effect puts over the target", { timeout: 60_000 }, async () => {
      const { window } = desktop;
      assert.equal((await openBrowserFixture(window, VIEW, `${fixture.url}hover-cover`)).ok, true);
      await waitForPage(window, "document.title === 'Hover cover'");
      const clicked = await perform(window, "click", { selector: "#target" });
      assert.equal(clicked.ok, false, "reported as clicked");
      assert.match(clicked.error, /covered by/);
      assert.deepEqual(await pageValue(window, "window.clicks"), []);
    });

    it(
      "acts only on the element it found, not one that replaced it by the same selector",
      { timeout: 60_000 },
      async () => {
        const { window } = desktop;
        const url = `${fixture.url}replaced-on-hover`;
        assert.equal((await openBrowserFixture(window, VIEW, url)).ok, true);
        await waitForPage(window, "document.title === 'Replaced on hover'");
        const clicked = await perform(window, "click", { selector: "#buy" });
        assert.equal(clicked.ok, false, "reported as clicked");
        assert.match(clicked.error, /removed from the page or replaced/);
        assert.deepEqual(
          await pageValue(window, "window.clicks"),
          [],
          "neither button was clicked",
        );
      },
    );

    it(
      "refuses a drag between endpoints farther apart than the viewport",
      { timeout: 60_000 },
      async () => {
        const { window } = desktop;
        assert.equal((await openBrowserFixture(window, VIEW, `${fixture.url}board`)).ok, true);
        await waitForPage(window, "document.title === 'Long board'");
        const result = await perform(window, "drag", {
          start_selector: "#far-card",
          end_selector: "#far-column",
        });
        assert.equal(result.ok, false);
        assert.match(result.error, /don't fit on screen together/);
        assert.deepEqual(
          await pageValue(window, "window.events"),
          [],
          "nothing was pressed or dragged",
        );
      },
    );

    it(
      "doesn't let agent input stand in for the user's gesture in design mode",
      { timeout: 60_000 },
      async () => {
        const { window, electronApp } = desktop;
        assert.equal((await openBrowserFixture(window, VIEW, fixture.url)).ok, true);
        await waitForPage(window, "document.title === 'Agent input fixture' && !!window.tap");
        await window.evaluate((id) => {
          window.agentInputSubmits = [];
          window.omnigentDesktop.onBrowserElementPromptSubmit((p) =>
            window.agentInputSubmits.push(p.prompt),
          );
          return window.omnigentDesktop.browserEnableDesignMode(id);
        }, VIEW);
        const submits = () => window.evaluate(() => window.agentInputSubmits);
        const pick = async (prompt) => {
          assert.equal((await perform(window, "click", { selector: "#tap" })).ok, true);
          const typed = await perform(window, "type", {
            selector: "#__omni-popup-input",
            text: prompt,
          });
          assert.equal(typed.ok, true, typed.error);
        };
        try {
          // The agent selects an element, writes a prompt and sends it.
          await pick("agent prompt");
          assert.equal(
            (await perform(window, "click", { selector: "#__omni-popup-send" })).ok,
            true,
          );
          await window.waitForTimeout(300);
          assert.deepEqual(await submits(), [], "the agent's click didn't authorize the submit");
          assert.equal((await perform(window, "press_key", { key: "Escape" })).ok, true);

          // The same prompt sent by the user's own click goes through.
          await pick("user prompt");
          await window.waitForTimeout(700);
          const send = JSON.parse(
            await pageValue(
              window,
              "JSON.stringify((() => { const r = document.getElementById('__omni-popup-send').getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; })())",
            ),
          );
          await electronApp.evaluate(
            (_electron, { id, point }) => {
              const entry = global.omnigentTestBrowserRegistries
                .map((registry) => registry.get(id))
                .find(Boolean);
              const wc = entry.view.webContents;
              const at = { x: Math.round(point.x), y: Math.round(point.y) };
              wc.sendInputEvent({ type: "mouseDown", ...at, button: "left", clickCount: 1 });
              wc.sendInputEvent({ type: "mouseUp", ...at, button: "left", clickCount: 1 });
            },
            { id: VIEW, point: send },
          );
          await window.waitForFunction(() => window.agentInputSubmits.length > 0, null, {
            timeout: 5000,
          });
          assert.deepEqual(await submits(), ["user prompt"]);
        } finally {
          await window.evaluate((id) => window.omnigentDesktop.browserDisableDesignMode(id), VIEW);
        }
      },
    );

    it(
      "refuses a loopback link an agent clicks on a page the user opened",
      { timeout: 60_000 },
      async () => {
        const { window } = desktop;
        // Opened like the address bar does: the user's own navigation may reach loopback.
        assert.equal((await openBrowserFixture(window, VIEW, `${fixture.url}linker`)).ok, true);
        await waitForPage(window, "document.title === 'Linker'");
        const clicked = await perform(window, "click", { selector: "#to-second" });
        assert.equal(clicked.ok, true, clicked.error);
        await window.waitForTimeout(1_000);
        assert.equal(await pageValue(window, "document.title"), "Linker", "the link was refused");
      },
    );

    it(
      "reports back and reload a page's leave-page warning kept from happening",
      { timeout: 60_000 },
      async () => {
        const { window, electronApp } = desktop;
        const previews = (on) =>
          electronApp.evaluate((_electron, value) => {
            globalThis.omnigentTestLocalPreviews = value;
          }, on);
        const historyIndex = () =>
          electronApp.evaluate(({ webContents }, url) => {
            const wc = webContents.getAllWebContents().find((c) => c.getURL() === url);
            return wc.navigationHistory.getActiveIndex();
          }, `${fixture.url}guarded`);
        await previews(true);
        try {
          assert.equal((await openBrowserFixture(window, VIEW, `${fixture.url}second`)).ok, true);
          await waitForPage(window, "document.title === 'Second'");
          assert.equal((await openBrowserFixture(window, VIEW, `${fixture.url}guarded`)).ok, true);
          await waitForPage(window, "document.title === 'Guarded'");
          // Playwright, attached to the view, would dismiss the warning's
          // dialog itself after Electron already kept the page (and fail).
          for (const page of electronApp.context().pages()) {
            if (page.url().endsWith("/guarded")) {
              page.on("dialog", (dialog) => dialog.dismiss().catch(() => {}));
            }
          }
          // Unsaved edits: the page asks to stay.
          const typed = await perform(window, "type", { selector: "#notes", text: "draft" });
          assert.equal(typed.ok, true, typed.error);
          const index = await historyIndex();
          const loads = await pageValue(window, "sessionStorage.loads");
          for (const action of ["go_back", "reload"]) {
            // oxlint-disable-next-line no-await-in-loop -- One step at a time.
            const result = await perform(window, action, {});
            assert.equal(result.ok, false, `${action} reported success`);
            assert.match(result.error, /the page asked to stay/, action);
            // oxlint-disable-next-line no-await-in-loop -- Checked per step.
            assert.equal(await historyIndex(), index, `${action}: same history entry`);
            // oxlint-disable-next-line no-await-in-loop -- Checked per step.
            assert.equal(await pageValue(window, "sessionStorage.loads"), loads, action);
          }
          await pageValue(window, "notes.value = ''; 0");
        } finally {
          await previews(false);
        }
      },
    );

    it(
      "never sets a form control on the page that replaced the one it was sent to",
      { timeout: 60_000 },
      async () => {
        const { window, electronApp } = desktop;
        // The agent's input puts the tab under its URL policy, which then
        // covers the navigation under way: allow it as a localhost preview.
        await electronApp.evaluate(() => {
          globalThis.omnigentTestLocalPreviews = true;
        });
        for (const [action, args] of [
          ["select_option", { selector: "#pick", values: ["y"] }],
          ["type", { selector: "#pick", text: "Y" }],
        ]) {
          // oxlint-disable-next-line no-await-in-loop -- One page at a time.
          assert.equal((await openBrowserFixture(window, VIEW, `${fixture.url}form`)).ok, true);
          // oxlint-disable-next-line no-await-in-loop -- One page at a time.
          await waitForPage(window, "document.title === 'Form'");
          const release = fixture.holdForm();
          // The tab starts loading the next page, which has the same select.
          // oxlint-disable-next-line no-await-in-loop -- One page at a time.
          const loading = await openBrowserFixture(window, VIEW, `${fixture.url}held-form`);
          assert.equal(loading.ok, true);
          const acting = perform(window, action, args);
          // oxlint-disable-next-line no-await-in-loop -- Let the action reach main.
          await window.waitForTimeout(300);
          release();
          // oxlint-disable-next-line no-await-in-loop -- One action at a time.
          const result = await acting;
          // oxlint-disable-next-line no-await-in-loop -- One page at a time.
          await waitForPage(window, "document.title === 'Held form'");
          assert.equal(result.ok, false, `${action} reported success`);
          assert.match(result.error, /the page changed \(it navigated\)/, action);
          // oxlint-disable-next-line no-await-in-loop -- Checked per action.
          assert.equal(await pageValue(window, "pick.value"), "x", `${action} left it alone`);
        }
        await electronApp.evaluate(() => {
          globalThis.omnigentTestLocalPreviews = false;
        });
        assert.equal((await openBrowserFixture(window, VIEW, fixture.url)).ok, true);
        await waitForPage(window, "document.title === 'Agent input fixture' && !!window.tap");
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
