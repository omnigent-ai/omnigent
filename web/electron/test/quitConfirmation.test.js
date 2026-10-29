const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  CONFIRM_BEFORE_QUIT_SETTING,
  confirmBeforeQuitEnabled,
  createQuitConfirmation,
  quitPromptDetail,
} = require("../src/quitConfirmation");

function harness({
  settings = {},
  responses = [],
  running = { hosts: 0, localServer: false },
  parent = { id: "focused-window" },
  isMac = true,
} = {}) {
  const calls = { prompts: [], quits: 0, saved: [] };
  let stored = { ...settings };
  let releasePrompt = null;
  const confirmation = createQuitConfirmation({
    appName: () => "Omnigent",
    isMac,
    loadSettings: () => ({ ...stored }),
    saveSettings: (next) => {
      stored = { ...next };
      calls.saved.push({ ...next });
    },
    runningWork: () => running,
    parentWindow: () => parent,
    showMessageBox: (win, options) => {
      calls.prompts.push({ win, options });
      const next = responses.shift();
      if (next === "hold") {
        return new Promise((resolve) => {
          releasePrompt = resolve;
        });
      }
      return Promise.resolve(next ?? { response: 1, checkboxChecked: false });
    },
    quit: () => {
      calls.quits += 1;
    },
  });
  return {
    calls,
    confirmation,
    settings: () => stored,
    releasePrompt: (result) => releasePrompt(result),
  };
}

describe("confirmBeforeQuitEnabled", () => {
  it("defaults on and only turns off for an explicit false", () => {
    assert.equal(confirmBeforeQuitEnabled({}), true);
    assert.equal(confirmBeforeQuitEnabled(undefined), true);
    assert.equal(confirmBeforeQuitEnabled({ [CONFIRM_BEFORE_QUIT_SETTING]: true }), true);
    assert.equal(confirmBeforeQuitEnabled({ [CONFIRM_BEFORE_QUIT_SETTING]: false }), false);
  });
});

describe("quitPromptDetail", () => {
  it("names the hosts and the local server that quitting stops", () => {
    assert.equal(quitPromptDetail({ hosts: 0, localServer: false }), undefined);
    assert.equal(
      quitPromptDetail({ hosts: 2, localServer: false }),
      "Sessions running on this computer will go offline.",
    );
    assert.equal(
      quitPromptDetail({ hosts: 0, localServer: true }),
      "The local Omnigent server will stop.",
    );
    assert.equal(
      quitPromptDetail({ hosts: 1, localServer: true }),
      "Sessions running on this computer will go offline, and the local Omnigent server will stop.",
    );
  });
});

describe("requestQuit", () => {
  it("asks on the focused window and stays open on Cancel", async () => {
    const h = harness({ responses: [{ response: 1, checkboxChecked: false }] });
    await h.confirmation.requestQuit();

    assert.equal(h.calls.quits, 0);
    assert.equal(h.calls.prompts.length, 1);
    const { win, options } = h.calls.prompts[0];
    assert.deepEqual(win, { id: "focused-window" });
    assert.equal(options.message, "Quit Omnigent?");
    assert.deepEqual(options.buttons, ["Quit", "Cancel"]);
    assert.equal(options.defaultId, 0);
    assert.equal(options.cancelId, 1);
    assert.equal(options.checkboxLabel, "Don't ask again");
    assert.equal("detail" in options, false);
  });

  it("quits on Quit and explains what stops", async () => {
    const h = harness({
      responses: [{ response: 0, checkboxChecked: false }],
      running: { hosts: 1, localServer: false },
    });
    await h.confirmation.requestQuit();

    assert.equal(h.calls.quits, 1);
    assert.equal(
      h.calls.prompts[0].options.detail,
      "Sessions running on this computer will go offline.",
    );
    assert.deepEqual(h.calls.saved, []);
  });

  it("remembers Don't ask again and then quits without asking", async () => {
    const h = harness({ responses: [{ response: 0, checkboxChecked: true }] });
    await h.confirmation.requestQuit();
    assert.equal(h.calls.quits, 1);
    assert.equal(h.settings()[CONFIRM_BEFORE_QUIT_SETTING], false);

    await h.confirmation.requestQuit();
    assert.equal(h.calls.quits, 2);
    assert.equal(h.calls.prompts.length, 1);
  });

  it("ignores Don't ask again when the user cancels", async () => {
    const h = harness({ responses: [{ response: 1, checkboxChecked: true }] });
    await h.confirmation.requestQuit();
    assert.equal(h.calls.quits, 0);
    assert.deepEqual(h.calls.saved, []);
  });

  it("does not stack a second prompt while one is open", async () => {
    const h = harness({ responses: ["hold"] });
    const first = h.confirmation.requestQuit();
    await h.confirmation.requestQuit();
    assert.equal(h.calls.prompts.length, 1);

    h.releasePrompt({ response: 0, checkboxChecked: false });
    await first;
    assert.equal(h.calls.quits, 1);

    // The prompt is closed again, so the next ⌘Q asks again.
    await h.confirmation.requestQuit();
    assert.equal(h.calls.prompts.length, 2);
  });

  it("asks without a parent when no window is open", async () => {
    const h = harness({ parent: null, responses: [{ response: 0, checkboxChecked: false }] });
    await h.confirmation.requestQuit();
    assert.equal(h.calls.prompts[0].win, null);
    assert.equal(h.calls.quits, 1);
  });
});

describe("quit menu items", () => {
  it("offers a persisted warn toggle and a confirming Quit item", async () => {
    const h = harness({ responses: [{ response: 0, checkboxChecked: false }] });
    const [warn, quit] = h.confirmation.menuItems();

    assert.equal(warn.id, "confirm_before_quit");
    assert.equal(warn.type, "checkbox");
    assert.equal(warn.label, "Warn Before Quitting (⌘Q)");
    assert.equal(warn.checked, true);
    warn.click({ checked: false });
    assert.equal(h.settings()[CONFIRM_BEFORE_QUIT_SETTING], false);
    assert.equal(h.confirmation.menuItems()[0].checked, false);
    warn.click({ checked: true });
    assert.equal(h.settings()[CONFIRM_BEFORE_QUIT_SETTING], true);

    assert.equal(quit.id, "quit");
    assert.equal(quit.label, "Quit Omnigent");
    assert.equal(quit.accelerator, "CmdOrCtrl+Q");
    quit.click();
    await new Promise((resolve) => {
      setImmediate(resolve);
    });
    assert.equal(h.calls.prompts.length, 1);
    assert.equal(h.calls.quits, 1);
  });

  it("names Ctrl+Q off macOS", () => {
    const h = harness({ isMac: false });
    assert.equal(h.confirmation.menuItems()[0].label, "Warn Before Quitting (Ctrl+Q)");
  });
});
