"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const {
  registerBrowserPermissions,
  createBrowserPermissionStore,
} = require("../src/browserPermissions");

const ORIGIN = "https://login.example.com";
const NETWORK_PERMISSIONS = ["local-network-access", "local-network", "loopback-network"];
const tick = () =>
  new Promise((resolve) => {
    setImmediate(resolve);
  });

function storage(initial = {}) {
  let settings = structuredClone(initial);
  const writes = [];
  const deps = {
    loadSettings: () => structuredClone(settings),
    saveSettings: (value) => {
      settings = structuredClone(value);
      writes.push(value);
    },
  };
  return {
    store: createBrowserPermissionStore(deps),
    writes,
    reopen: () => createBrowserPermissionStore(deps),
  };
}

function harness({ url = `${ORIGIN}/signin`, visible = true, saved = storage() } = {}) {
  let currentUrl = url;
  let isVisible = visible;
  let requestHandler;
  let checkHandler;
  let destroyed = false;
  const dialogs = [];
  const responses = [];
  const wc = new EventEmitter();
  wc.isDestroyed = () => destroyed;
  wc.getURL = () => currentUrl;
  wc.reloads = 0;
  wc.reload = () => {
    wc.reloads++;
    wc.emit("did-start-navigation", {}, currentUrl, false, true);
  };
  const session = {
    setPermissionRequestHandler: (handler) => (requestHandler = handler),
    setPermissionCheckHandler: (handler) => (checkHandler = handler),
  };
  const policy = registerBrowserPermissions(session, {
    canPrompt: () => isVisible,
    store: saved.store,
    showPrompt: (options) => {
      dialogs.push(options);
      return new Promise((resolve, reject) => {
        options.signal.addEventListener("abort", () => resolve("dismiss"), { once: true });
        responses.push({ resolve, reject });
      });
    },
  });
  policy.attach(wc);
  return {
    wc,
    policy,
    dialogs,
    saved,
    respond: (choice) => responses.shift().resolve(choice),
    fail: () => responses.shift().reject(new Error("Window closed")),
    check: (permission = "loopback-network", origin = ORIGIN, details = {}, sender = wc) =>
      checkHandler(sender, permission, origin, { isMainFrame: true, ...details }),
    request: (permission = "loopback-network", details = {}, sender = wc) =>
      new Promise((resolve) => {
        requestHandler(sender, permission, resolve, {
          requestingUrl: currentUrl,
          isMainFrame: true,
          ...details,
        });
      }),
    setVisible: (value) => (isVisible = value),
    navigate(next = currentUrl) {
      wc.emit("did-start-navigation", {}, next, false, true);
      currentUrl = next;
      wc.emit("did-navigate", {}, next);
    },
    crash() {
      wc.emit("render-process-gone", {}, { reason: "crashed" });
    },
    destroy() {
      destroyed = true;
      wc.emit("destroyed");
    },
  };
}

describe("browser local network permissions", () => {
  for (const permission of NETWORK_PERMISSIONS) {
    it(`requires native approval for ${permission}`, async () => {
      const h = harness();
      const result = h.request(permission);
      await tick();
      assert.equal(h.dialogs.length, 1);
      const dialog = h.dialogs[0];
      assert.equal(dialog.origin, ORIGIN);
      assert.equal(dialog.reload, false);
      assert.equal(dialog.signal.aborted, false);
      h.respond("allow-once");
      assert.equal(await result, true);
      assert.equal(h.wc.reloads, 0);
      for (const name of NETWORK_PERMISSIONS) assert.equal(h.check(name), true);
      assert.equal(h.dialogs.length, 1);
    });
  }

  it("does not grant on a check before consent, and reloads after allowing once", async () => {
    const h = harness();
    for (const name of NETWORK_PERMISSIONS) assert.equal(h.check(name), false);
    await tick();
    assert.equal(h.dialogs.length, 1, "coalesce query aliases into one dialog");
    assert.equal(h.dialogs[0].reload, true);
    assert.equal(h.wc.reloads, 0);
    h.respond("allow-once");
    await tick();
    assert.equal(h.wc.reloads, 1);
    assert.equal(h.check(), true);
    await tick();
    assert.equal(h.dialogs.length, 1);
  });

  it("coalesces concurrent requests", async () => {
    const h = harness();
    const results = NETWORK_PERMISSIONS.map((permission) => h.request(permission));
    await tick();
    assert.equal(h.dialogs.length, 1);
    h.respond("allow-once");
    assert.deepEqual(await Promise.all(results), [true, true, true]);
  });

  for (const response of ["deny", "dismiss", "invalid"]) {
    it(`treats response ${response} as denial and does not reprompt`, async () => {
      const h = harness();
      assert.equal(h.check(), false);
      await tick();
      h.respond(response);
      await tick();
      assert.equal(h.check(), false);
      assert.equal(await h.request(), false);
      assert.equal(h.dialogs.length, 1);
      assert.equal(h.wc.reloads, 0);
    });
  }

  it("keeps every unrelated permission denied even after network approval", async () => {
    const h = harness();
    const result = h.request();
    await tick();
    h.respond("allow-once");
    await result;
    const permissions = ["media", "geolocation", "notifications", "clipboard-read", "unknown"];
    const results = await Promise.all(permissions.map((permission) => h.request(permission)));
    assert.ok(results.every((granted) => granted === false));
    for (const permission of permissions) assert.equal(h.check(permission), false);
    assert.equal(h.dialogs.length, 1);
  });

  it("isolates choices by site and by conversation browser", async () => {
    const a = harness();
    const b = harness();
    const result = a.request();
    await tick();
    a.respond("allow-once");
    await result;
    a.navigate("https://another.example/signin");
    assert.equal(a.check("loopback-network", ORIGIN), false);
    assert.equal(a.check("loopback-network", "https://another.example"), false);
    assert.equal(b.check(), false);
    await tick();
    assert.equal(a.dialogs.length, 2);
    assert.equal(b.dialogs.length, 1);
    a.respond("deny");
    b.respond("deny");
    await tick();
    a.navigate(`${ORIGIN}/signin`);
    a.setVisible(false);
    assert.equal(a.check(), false, "allow once expires on leaving the site");
  });

  it("rejects subframes, mismatched origins, foreign contents, and unattributed requests", async () => {
    const h = harness();
    assert.equal(h.check("loopback-network", ORIGIN, { isMainFrame: false }), false);
    assert.equal(h.check("loopback-network", "https://other.example"), false);
    assert.equal(
      h.check("loopback-network", ORIGIN, { embeddingOrigin: "https://other.example" }),
      false,
    );
    assert.equal(h.check("loopback-network", ORIGIN, {}, new EventEmitter()), false);
    assert.equal(h.check("loopback-network", ORIGIN, {}, null), false);
    assert.equal(await h.request("loopback-network", { isMainFrame: false }), false);
    assert.equal(
      await h.request("loopback-network", { requestingUrl: "https://other.example" }),
      false,
    );
    assert.equal(await h.request("loopback-network", {}, null), false);
    await tick();
    assert.equal(h.dialogs.length, 0);
  });

  it("handles context-free checks only when requesting, embedding, and current origins match", async () => {
    const h = harness();
    const details = { embeddingOrigin: ORIGIN, isMainFrame: false };
    assert.equal(h.check("loopback-network", ORIGIN, details, null), false);
    await tick();
    assert.equal(h.dialogs.length, 1);
    h.respond("allow-once");
    await tick();
    assert.equal(h.check("loopback-network", ORIGIN, details, null), true);
    assert.equal(
      h.check("loopback-network", ORIGIN, { embeddingOrigin: "https://other.example" }, null),
      false,
    );
  });

  for (const url of [
    "http://insecure.example",
    "file:///tmp/page.html",
    "data:text/html,hi",
    "about:blank",
  ]) {
    it(`does not prompt for ${url}`, async () => {
      const h = harness({ url });
      assert.equal(h.check("loopback-network", url), false);
      assert.equal(await h.request(), false);
      await tick();
      assert.equal(h.dialogs.length, 0);
    });
  }

  it("allows prompting from trustworthy HTTP loopback origins", async () => {
    const h = harness({ url: "http://localhost:8000/signin" });
    const result = h.request();
    await tick();
    h.respond("allow-once");
    assert.equal(await result, true);
  });

  for (const invalidate of [
    (h) => h.navigate(),
    (h) => {
      h.navigate("https://other.example");
      h.navigate(`${ORIGIN}/signin`);
    },
    (h) => h.destroy(),
    (h) => h.setVisible(false),
  ]) {
    it("discards approval after navigation, teardown, or hiding the requesting pane", async () => {
      const h = harness();
      const result = h.request();
      await tick();
      invalidate(h);
      h.respond("always-allow");
      assert.equal(await result, false);
      assert.equal(h.wc.reloads, 0);
      h.setVisible(false);
      assert.equal(h.check(), false, "stale approval was not saved");
    });
  }

  for (const choice of ["allow-once", "always-allow"]) {
    it(`honors an existing ${choice} grant while its pane is hidden`, async () => {
      const h = harness();
      const result = h.request();
      await tick();
      h.respond(choice);
      assert.equal(await result, true);
      assert.equal(h.check(), true);

      h.setVisible(false);
      assert.equal(h.check(), true);
      assert.equal(await h.request(), true);
      assert.equal(h.dialogs.length, 1, "an existing grant must not open another prompt");
    });
  }

  it("does not prompt for hidden panes, but lets the visible pane retry", async () => {
    const h = harness({ visible: false });
    assert.equal(h.check(), false);
    assert.equal(await h.request(), false);
    await tick();
    assert.equal(h.dialogs.length, 0);
    h.setVisible(true);
    assert.equal(h.check(), false);
    await tick();
    assert.equal(h.dialogs.length, 1);
    h.respond("deny");
    await tick();
  });

  it("fails closed if the native dialog fails", async () => {
    const h = harness();
    const result = h.request();
    await tick();
    h.fail();
    assert.equal(await result, false);
    assert.equal(h.wc.reloads, 0);
  });

  it("remembers Always allow across conversations and app restarts, but not other sites", async () => {
    const saved = storage({ server_url: "https://server.example" });
    const a = harness({ saved });
    const b = harness({ saved });
    const result = a.request();
    await tick();
    assert.equal(saved.writes.length, 0, "no grant persisted before consent");
    a.respond("always-allow");
    assert.equal(await result, true);
    assert.equal(b.check(), true);
    a.destroy();
    const reopened = harness({ saved: { store: saved.reopen() } });
    assert.equal(reopened.check(), true);
    assert.equal(saved.writes[0].server_url, "https://server.example");
    assert.deepEqual(saved.writes[0].browser_local_network_permissions, { [ORIGIN]: true });
    b.navigate("https://different.example");
    b.setVisible(false);
    assert.equal(b.check("local-network", "https://different.example"), false);
  });

  it("Allow once survives a reload but not leaving the site, closing, or another conversation", async () => {
    const saved = storage();
    const a = harness({ saved });
    const result = a.request();
    await tick();
    a.respond("allow-once");
    assert.equal(await result, true);
    a.navigate(`${ORIGIN}/next`);
    assert.equal(a.check(), true);
    a.wc.reload();
    assert.equal(a.check(), true);
    assert.equal(saved.writes.length, 0);
    a.navigate("https://different.example");
    a.navigate(`${ORIGIN}/signin`);
    a.setVisible(false);
    assert.equal(a.check(), false);
    const b = harness({ saved, visible: false });
    assert.equal(b.check(), false);
  });

  for (const savedChoice of ["deny", "always-allow"]) {
    it(`does not let a stale Allow once prompt erase a newer ${savedChoice} decision`, async () => {
      const saved = storage();
      const stale = harness({ saved });
      const current = harness({ saved });

      const staleResult = stale.request();
      await tick();
      const currentResult = current.request();
      await tick();
      current.respond(savedChoice);
      assert.equal(await currentResult, savedChoice === "always-allow");

      stale.respond("allow-once");
      assert.equal(await staleResult, false, "the stale prompt must be invalidated");
      assert.equal(saved.writes.length, 1, "Allow once must not rewrite persistent settings");
      assert.deepEqual(saved.writes[0].browser_local_network_permissions, {
        [ORIGIN]: savedChoice === "always-allow",
      });

      const fresh = harness({ saved: { store: saved.reopen() } });
      assert.equal(fresh.check(), savedChoice === "always-allow");
    });
  }

  it("Deny revokes one-visit grants in other browsers and persists across restarts", async () => {
    const saved = storage();
    const a = harness({ saved });
    const b = harness({ saved });
    const result = a.request();
    await tick();
    a.respond("allow-once");
    await result;
    const blocked = b.request();
    await tick();
    b.respond("deny");
    assert.equal(await blocked, false);
    assert.equal(a.check(), false);
    assert.equal(harness({ saved: { store: saved.reopen() } }).check(), false);
    saved.store.set(ORIGIN, undefined);
    const allowed = b.request();
    await tick();
    b.respond("allow-once");
    assert.equal(await allowed, true);
    assert.equal(b.check(), true);
    a.setVisible(false);
    assert.equal(a.check(), false, "clearing Deny must not revive an older one-visit grant");
  });

  it("dismissal neither saves a denial nor reloads, and can be retried after navigation", async () => {
    const h = harness();
    const result = h.request();
    await tick();
    h.respond("dismiss");
    assert.equal(await result, false);
    assert.equal(h.saved.writes.length, 0);
    assert.equal(h.wc.reloads, 0);
    assert.equal(h.check(), false);
    await tick();
    assert.equal(h.dialogs.length, 1, "do not repeatedly prompt a dismissed document");
    h.navigate();
    assert.equal(h.check(), false);
    await tick();
    h.respond("allow-once");
    await tick();
    assert.equal(h.check(), true);
  });

  it("does not grant if saving an Always allow choice fails", async () => {
    const h = harness({
      saved: {
        store: createBrowserPermissionStore({
          loadSettings: () => ({}),
          saveSettings: () => {
            throw new Error("Read-only settings");
          },
        }),
      },
    });
    const result = h.request();
    await tick();
    h.respond("always-allow");
    assert.equal(await result, false);
    assert.equal(h.check(), false);
    assert.equal(h.wc.reloads, 0);
  });

  it("aborts pending consent when the requesting renderer crashes", async () => {
    const h = harness();
    const result = h.request();
    await tick();

    h.crash();
    assert.equal(h.wc.isDestroyed(), false, "Electron can retain WebContents after a crash");
    assert.equal(h.dialogs[0].signal.aborted, true);
    h.respond("always-allow");

    assert.equal(await result, false);
    assert.equal(h.saved.writes.length, 0);
    h.setVisible(false);
    assert.equal(h.check(), false, "the crashed visit must not retain a grant");
  });

  it("aborts the prompt as soon as its document navigates or is destroyed", async () => {
    await Promise.all(
      [(h) => h.navigate(), (h) => h.destroy()].map(async (invalidate) => {
        const h = harness();
        const result = h.request();
        await tick();
        invalidate(h);
        assert.equal(h.dialogs[0].signal.aborted, true);
        assert.equal(await result, false);
        assert.equal(h.saved.writes.length, 0);
      }),
    );
  });
});
