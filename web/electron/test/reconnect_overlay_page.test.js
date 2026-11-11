"use strict";

// The overlay page's script, rendered in jsdom. The controller is tested in
// reconnect_overlay.test.ts.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const PAGE = path.join(__dirname, "../reconnect-overlay/index.html");

describe("reconnect overlay page (reconnect-overlay/index.html)", () => {
  function render(hint) {
    const dom = new JSDOM(fs.readFileSync(PAGE, "utf8"), {
      url: `file:///app/reconnect-overlay/index.html?${new URLSearchParams({ hint })}`,
      runScripts: "outside-only",
    });
    const calls = [];
    dom.window.omnigentReconnectOverlay = { cancel: () => calls.push("cancel") };
    dom.window.eval(dom.window.document.querySelector("script").textContent);
    return { document: dom.window.document, window: dom.window, calls };
  }

  it("shows the hint as text and cancels from the button or Escape", () => {
    const { document, window, calls } = render("<b>Check your network connection.</b>");
    assert.equal(document.getElementById("title").textContent, "Reconnecting to Databricks…");
    assert.equal(
      document.getElementById("hint").textContent,
      "<b>Check your network connection.</b>",
    );
    document.getElementById("cancel").click();
    document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape" }));
    assert.deepEqual(calls, ["cancel", "cancel"]);
  });

  it("keeps Cancel clear of the title when there is no hint", () => {
    const { document, window } = render("");
    assert.equal(window.getComputedStyle(document.getElementById("hint")).display, "none");
    assert.equal(window.getComputedStyle(document.querySelector(".actions")).marginTop, "16px");
  });
});
