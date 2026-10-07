const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { loadPreload: loadPreloadWithIpc } = require("../test-support/preload_harness");

function loadPreload() {
  let updateStatus = { state: "idle" };
  const h = loadPreloadWithIpc((channel) =>
    channel === "omnigent:get-update-status" ? updateStatus : null,
  );
  return {
    ...h,
    setStatus: (status) => {
      updateStatus = status;
    },
  };
}

describe("server-page update bridge", () => {
  it("hides every shell-owned update prompt state, including download progress", async () => {
    const h = loadPreload();
    async function expectHidden(state, lastError) {
      h.setStatus({ state, lastError });
      const status = await h.desktop.updates.getStatus();
      assert.equal(status.state, "idle");
      assert.equal(status.progress, undefined);
      assert.equal(status.info, undefined);
    }

    await expectHidden("available");
    await expectHidden("downloading");
    await expectHidden("downloaded");
    await expectHidden("error-security", "signature failed");
  });

  it("forwards embedded Browser recent-session input and unsubscribes", () => {
    const h = loadPreload();
    const received = [];
    const unsubscribe = h.desktop.onBrowserRecentSessionInput((input) => received.push(input));

    h.emit("browser-recent-session-input", { type: "keydown", key: "Tab", ctrlKey: true });
    assert.deepEqual(received, [{ type: "keydown", key: "Tab", ctrlKey: true }]);

    unsubscribe();
    assert.equal(h.hasListener("browser-recent-session-input"), false);
  });

  it("asks the main process to cancel a declined recent-session switch", async () => {
    const h = loadPreload();

    await h.desktop.browserCancelRecentSessionSwitch();

    assert.ok(
      h.invokes.some(({ channel }) => channel === "omnigent:browser-cancel-recent-session-switch"),
    );
  });

  it("advertises recent-session switch support to the main process", async () => {
    const h = loadPreload();

    await h.desktop.browserSetRecentSessionSwitchSupported(true);
    await h.desktop.browserSetRecentSessionSwitchSupported(false);

    assert.deepEqual(
      h.invokes
        .filter(({ channel }) => channel === "omnigent:browser-set-recent-session-switch-supported")
        .map(({ args }) => args.supported),
      [true, false],
    );
  });
});
