"use strict";

// A file picker a page opens a while after the agent's input
// (a timer, a fetch) is still intercepted, not shown over the user's screen.

const assert = require("node:assert/strict");
const { describe, it } = require("node:test");
const { fileChooserGuard, performBrowserInput } = require("../src/browserInput");
const { at, makeEntry, names, sleep } = require("./browserInputFakes");

describe("performBrowserInput: file pickers a page opens later", () => {
  it("stay intercepted for the activation the agent's input gave, until the user acts", async () => {
    fileChooserGuard.ms = 100;
    try {
      const disabled = (commands) =>
        names(commands).filter((n) => n === "Page.setInterceptFileChooserDialog:false").length;
      // A click's activation outlives the action: still intercepted after it.
      const clicked = makeEntry({ points: { "#upload": at(5, 5) } });
      await performBrowserInput(clicked.entry, "click", { selector: "#upload" });
      assert.deepEqual(clicked.lifecycle, ["attach:1.3"], "still attached after the click");
      await sleep(150);
      assert.deepEqual(clicked.lifecycle, ["attach:1.3", "detach"], "let go once it lapses");

      // The next action takes it over rather than attaching again.
      const reused = makeEntry({ points: { "#upload": at(5, 5) } });
      await performBrowserInput(reused.entry, "click", { selector: "#upload" });
      await performBrowserInput(reused.entry, "press_key", { key: "Tab" });
      assert.deepEqual(reused.lifecycle, ["attach:1.3"]);
      // The user's own press is theirs: their upload opens.
      reused.webContents.emit("before-mouse-event", {}, { type: "mouseMove" });
      assert.deepEqual(reused.lifecycle, ["attach:1.3"], "moving the mouse isn't acting");
      reused.webContents.emit("before-mouse-event", {}, { type: "mouseDown" });
      assert.deepEqual(reused.lifecycle, ["attach:1.3", "detach"]);

      // On a debugger someone else attached, so is its keypress, and the
      // file chooser goes back to it.
      const theirs = makeEntry({ attached: true, points: { "#upload": at(5, 5) } });
      await performBrowserInput(theirs.entry, "click", { selector: "#upload" });
      assert.equal(disabled(theirs.commands), 0);
      theirs.webContents.emit("before-input-event", {}, { type: "keyDown" });
      assert.equal(disabled(theirs.commands), 1);
      assert.deepEqual(theirs.lifecycle, []);

      // An action after the click that gives no activation of its own (a
      // hover, or one refused) keeps the click's time left, no more.
      // (Long enough to outlast the hovers' own measuring.)
      fileChooserGuard.ms = 1500;
      for (const attached of [false, true]) {
        const page = makeEntry({ attached, points: { "#upload": at(5, 5) } });
        // oxlint-disable-next-line no-await-in-loop -- One page at a time.
        await performBrowserInput(page.entry, "click", { selector: "#upload" });
        const lapsesAt = Date.now() + fileChooserGuard.ms;
        // oxlint-disable-next-line no-await-in-loop -- One action at a time.
        await performBrowserInput(page.entry, "hover", { selector: "#upload" });
        // oxlint-disable-next-line no-await-in-loop -- One action at a time.
        await assert.rejects(performBrowserInput(page.entry, "hover", { selector: "#gone" }));
        const off = () =>
          attached ? disabled(page.commands) > 0 : page.lifecycle.includes("detach");
        assert.equal(off(), false, `still intercepted after the hover (attached: ${attached})`);
        // oxlint-disable-next-line no-await-in-loop -- Wait out the click's activation.
        await sleep(lapsesAt - Date.now() + 100);
        assert.equal(
          off(),
          true,
          `let go once the click's activation lapses (attached: ${attached})`,
        );
      }

      fileChooserGuard.ms = 100;

      // A hover gives no activation, so nothing stays on.
      const hovered = makeEntry({ points: { "#upload": at(5, 5) } });
      await performBrowserInput(hovered.entry, "hover", { selector: "#upload" });
      assert.deepEqual(hovered.lifecycle, ["attach:1.3", "detach"]);
    } finally {
      fileChooserGuard.ms = 0;
    }
  });
});
