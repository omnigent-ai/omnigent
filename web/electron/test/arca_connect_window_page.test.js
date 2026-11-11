"use strict";

// The Arca connect console page's script, rendered in jsdom. The flow is tested
// in arca_connect_window.test.ts.

const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

/** Load the console page with a fake bridge; jsdom has no layout, so scrollHeight is stubbed. */
function renderConsole(t, { scrollHeight }) {
  const dom = new JSDOM(
    fs.readFileSync(path.join(__dirname, "../arca-connect/index.html"), "utf8"),
    {
      runScripts: "outside-only",
    },
  );
  t.after(() => dom.window.close());
  const page = { document: dom.window.document, events: {}, sizes: [] };
  // Electron supplies this bridge; no remote process is launched by a renderer test.
  dom.window.arcaConnect = {
    confirm() {},
    cancel() {},
    resizeToContent: (size) => page.sizes.push(size),
  };
  dom.window.Terminal = class Terminal {
    loadAddon() {}
    open() {}
    write() {}
  };
  dom.window.FitAddon = {
    FitAddon: class FitAddon {
      fit() {}
    },
  };
  for (const event of ["Init", "Started", "Output", "Done"]) {
    dom.window.arcaConnect[`on${event}`] = (listener) => {
      page.events[event] = listener;
    };
  }
  dom.window.requestAnimationFrame = (callback) => {
    callback();
    return 1;
  };
  dom.window.cancelAnimationFrame = () => {};
  Object.defineProperty(page.document.documentElement, "scrollHeight", { value: scrollHeight });
  dom.window.eval(fs.readFileSync(path.join(__dirname, "../arca-connect/console.js"), "utf8"));
  return page;
}

for (const copy of [
  {
    name: "ordinary connection",
    reason: null,
    title: "Connect your Arca instance?",
    intro: "This runs the command below over ",
    button: "Connect",
  },
  {
    name: "preview reconnect",
    reason: "preview-reconnect",
    title: "Reconnect Arca for this preview?",
    intro: "To load this preview, run the command below over ",
    button: "Reconnect",
  },
]) {
  it(`renders consent, sign-in, retry, and completion for ${copy.name}`, (t) => {
    const { document, events, sizes } = renderConsole(t, { scrollHeight: 360 });
    const confirm = document.getElementById("confirm");
    const command = document.getElementById("command");
    events.Init({
      serverUrl: "https://account.databricks.com/omnigent?o=123",
      command: "host command",
      reason: copy.reason,
    });
    assert.equal(document.getElementById("title").textContent, copy.title);
    assert.equal(document.getElementById("intro").textContent, copy.intro);
    assert.equal(confirm.textContent, copy.button);
    assert.equal(sizes.at(-1).height, 360);
    assert.equal(sizes.at(-1).phase, "asking");
    confirm.click();
    events.Started({ login: false, command: "host command" });
    assert.equal(confirm.disabled, true);
    // The console can go straight from the connect to sign-in.
    events.Started({ login: true, command: "login command" });
    assert.equal(command.textContent, "login command");
    assert.equal(confirm.textContent, "Signing in…");
    assert.equal(confirm.disabled, true);
    events.Started({ login: false, command: "host command" });
    assert.equal(command.textContent, "host command");
    assert.equal(confirm.textContent, "Connecting…");
    events.Done({ ok: true });
    assert.equal(confirm.hidden, true);
    assert.equal(document.getElementById("cancel").textContent, "Close");
    assert.match(document.getElementById("status").textContent, /Connected/);
  });
}
