const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  loadServerAfterAuth,
  loadInitialDestination,
  isSetupIdle,
  withServerLoad,
} = require("../src/server_load");

describe("transactional server loading", () => {
  it("does not mutate settings, manifest, or page when authentication is cancelled", async () => {
    const events = [];

    const loaded = await loadServerAfterAuth({
      authenticate: async () => {
        events.push("authenticate");
        return false;
      },
      beforeLoad: () => events.push("commit-settings-and-manifest"),
      load: async () => events.push("load-new-server"),
    });

    assert.equal(loaded, false);
    assert.deepEqual(events, ["authenticate"]);
  });

  it("commits server state only after authentication and before navigation", async () => {
    const events = [];

    const loaded = await loadServerAfterAuth({
      authenticate: async () => {
        events.push("authenticate");
        return true;
      },
      beforeLoad: () => events.push("commit-settings-and-manifest"),
      load: async () => events.push("load-new-server"),
    });

    assert.equal(loaded, true);
    assert.deepEqual(events, ["authenticate", "commit-settings-and-manifest", "load-new-server"]);
  });

  it("loads setup after a cancelled cold-start login", async () => {
    const events = [];

    const loaded = await loadInitialDestination({
      loadServer: async () => {
        events.push("authenticate-saved-server");
        return false;
      },
      loadSetup: async () => events.push("load-setup"),
    });

    assert.equal(loaded, false);
    assert.deepEqual(events, ["authenticate-saved-server", "load-setup"]);
  });

  it("does not reuse setup while another server load is authenticating", async () => {
    const state = { serverUrl: null };
    const authentication = Promise.withResolvers();
    const connecting = withServerLoad(state, () => authentication.promise);

    assert.equal(isSetupIdle(state), false);
    authentication.resolve();
    await connecting;
    assert.equal(isSetupIdle(state), true);
  });

  it("rejects a competing expiry load while navigation is in flight", async () => {
    const state = { pendingServerLoads: 0 };
    const navigation = Promise.withResolvers();
    const events = [];
    const first = withServerLoad(state, async () => {
      events.push("navigation");
      await navigation.promise;
    });

    const expiry = await withServerLoad(state, async () => events.push("expiry"));

    assert.equal(expiry, false);
    assert.deepEqual(events, ["navigation"]);
    navigation.resolve();
    await first;
    assert.equal(state.pendingServerLoads, 0);
  });

  it("tracks and clears a rejected pending load with the server-load gate", async () => {
    const state = { pendingServerLoads: 0 };
    const loadStarted = Promise.withResolvers();
    const finishLoad = Promise.withResolvers();
    const loadError = new Error("load failed");
    const loading = withServerLoad(state, async () => {
      loadStarted.resolve();
      await finishLoad.promise;
      throw loadError;
    });

    await loadStarted.promise;
    assert.equal(state.pendingServerLoads, 1);
    assert.equal(state.pendingLoad, loading);

    finishLoad.resolve();
    await assert.rejects(loading, loadError);
    assert.equal(state.pendingServerLoads, 0);
    assert.equal(state.pendingLoad, null);
  });
});
