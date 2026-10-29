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
  running = { hosts: 0, localServer: false },
  pageCanAsk = true,
  isMac = true,
} = {}) {
  const calls = { asked: [], quits: 0, saved: [] };
  let stored = { ...settings };
  const confirmation = createQuitConfirmation({
    isMac,
    appName: () => "Omnigent",
    loadSettings: () => ({ ...stored }),
    saveSettings: (next) => {
      stored = { ...next };
      calls.saved.push({ ...next });
    },
    runningWork: () => running,
    ask: (detail) => {
      calls.asked.push(detail);
      return pageCanAsk;
    },
    quit: () => {
      calls.quits += 1;
    },
  });
  return { calls, confirmation, settings: () => stored };
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
    assert.equal(quitPromptDetail({ hosts: 0, localServer: false }), null);
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
  it("asks in a page, naming what stops, and waits for its answer", () => {
    const h = harness({ running: { hosts: 1, localServer: false } });
    h.confirmation.requestQuit();
    assert.deepEqual(h.calls.asked, ["Sessions running on this computer will go offline."]);
    assert.equal(h.calls.quits, 0);
  });

  it("quits at once when no page can ask, or the prompt is off", () => {
    const noPage = harness({ pageCanAsk: false });
    noPage.confirmation.requestQuit();
    assert.equal(noPage.calls.quits, 1);

    const off = harness({ settings: { [CONFIRM_BEFORE_QUIT_SETTING]: false } });
    off.confirmation.requestQuit();
    assert.deepEqual(off.calls.asked, []);
    assert.equal(off.calls.quits, 1);
  });
});

describe("confirmQuit", () => {
  it("quits, and remembers Don't ask again for the next Quit", () => {
    const h = harness();
    h.confirmation.confirmQuit({ dontAskAgain: false });
    assert.equal(h.calls.quits, 1);
    assert.deepEqual(h.calls.saved, []);

    h.confirmation.confirmQuit({ dontAskAgain: true });
    assert.equal(h.calls.quits, 2);
    assert.equal(h.settings()[CONFIRM_BEFORE_QUIT_SETTING], false);
    h.confirmation.requestQuit();
    assert.deepEqual(h.calls.asked, []);
    assert.equal(h.calls.quits, 3);
  });
});

describe("quit menu items", () => {
  it("offers a persisted warn toggle and a confirming Quit item", () => {
    const h = harness();
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
    assert.equal(h.calls.asked.length, 1);
    assert.equal(h.calls.quits, 0);
  });

  it("names Ctrl+Q off macOS", () => {
    const h = harness({ isMac: false });
    assert.equal(h.confirmation.menuItems()[0].label, "Warn Before Quitting (Ctrl+Q)");
  });
});
