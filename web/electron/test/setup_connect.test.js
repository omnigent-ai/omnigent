"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const SERVER = "https://workspace.cloud.databricks.com/omnigent?o=123";
const tick = () =>
  new Promise((resolve) => {
    setImmediate(resolve);
  });

async function harness(t, { cancel = async () => true, search = "" } = {}) {
  const dom = new JSDOM(fs.readFileSync(path.join(__dirname, "../setup/index.html"), "utf8"), {
    url: `https://setup.example/${search}`,
    runScripts: "outside-only",
  });
  t.after(() => dom.window.close());
  const requests = [];
  const cancellations = [];
  let progress;
  dom.window.omnigentSetup = {
    getServerUrl: async () => SERVER,
    getManagedServers: async () => [],
    getRecentServers: async () => [SERVER],
    onConnectionProgress: (listener) => {
      progress = listener;
      return () => {
        progress = null;
      };
    },
    setServerUrl: (url, opts) =>
      new Promise((resolve, reject) => {
        requests.push({ url, ...opts, resolve, reject });
      }),
    cancelServerConnection: async (id) => {
      cancellations.push(id);
      return cancel(id);
    },
  };
  dom.window.eval(fs.readFileSync(path.join(__dirname, "../src/url.js"), "utf8"));
  dom.window.eval(dom.window.document.querySelector("script:not([src])").textContent);
  await tick();
  const document = dom.window.document;
  return {
    requests,
    cancellations,
    window: dom.window,
    document,
    input: document.getElementById("url"),
    connect: document.getElementById("connect"),
    cancel: document.getElementById("cancel-connect"),
    label: document.getElementById("connect-label"),
    spinner: document.getElementById("connect-spinner"),
    error: document.getElementById("err"),
    progress: (requestId, phase) => progress?.({ requestId, phase }),
  };
}

it("shows a concise expiry message without repeating the prefilled server URL", async (t) => {
  const message = "Session expired. Connect to sign in again.";
  const params = new URLSearchParams({ error: message, url: SERVER });
  const h = await harness(t, { search: `?${params}` });
  assert.equal(h.error.textContent, message);
  assert.equal(h.input.value, SERVER);
  assert.equal(h.connect.disabled, false);
});

it("shows a spinner and an attached cancel action while the backend authenticates", async (t) => {
  const h = await harness(t);
  assert.equal(h.cancel.hidden, true);
  h.connect.click();
  assert.equal(h.label.textContent, "Connecting…");
  assert.equal(h.spinner.hidden, false);
  assert.equal(h.connect.disabled, true);
  assert.equal(h.input.disabled, true);
  assert.equal(h.connect.getAttribute("aria-busy"), "true");
  h.progress(h.requests[0].requestId, "authenticating");
  assert.equal(h.label.textContent, "Authenticating…");
  assert.equal(h.cancel.hidden, false);
  assert.equal(h.cancel.disabled, false);
  assert.equal(h.cancel.getAttribute("aria-label"), "Cancel sign-in");
  assert.equal(h.document.getElementById("connect-group").dataset.cancellable, "true");
  assert.equal(h.requests[0].url, SERVER);
});

it("prevents duplicate submissions from click, Enter, and recent-server actions", async (t) => {
  const h = await harness(t);
  h.connect.click();
  h.connect.click();
  h.input.dispatchEvent(new h.window.KeyboardEvent("keydown", { key: "Enter" }));
  h.document.querySelector(".recent-btn").click();
  assert.equal(h.requests.length, 1);
});

it("cancels the matching attempt and restores the same URL for retry", async (t) => {
  const h = await harness(t);
  h.connect.click();
  const request = h.requests[0];
  h.progress(request.requestId, "authenticating");
  h.cancel.click();
  assert.equal(h.label.textContent, "Cancelling…");
  assert.equal(h.cancel.disabled, true);
  await tick();
  assert.deepEqual(h.cancellations, [request.requestId]);
  assert.equal(h.connect.disabled, false);
  assert.equal(h.label.textContent, "Connect");
  assert.equal(h.spinner.hidden, true);
  assert.equal(h.cancel.hidden, true);
  assert.equal(h.input.disabled, false);
  assert.equal(h.input.value, SERVER);
  assert.equal(h.error.textContent, "");
});

it("ignores late progress and completion from a cancelled attempt after retry", async (t) => {
  const h = await harness(t);
  h.connect.click();
  const previous = h.requests[0];
  h.cancel.click();
  await tick();
  h.connect.click();
  const next = h.requests[1];
  assert.notEqual(previous.requestId, next.requestId);
  h.progress(next.requestId, "authenticating");
  h.progress(previous.requestId, "connecting");
  previous.reject(new Error("late failure"));
  await tick();
  assert.equal(h.label.textContent, "Authenticating…");
  assert.equal(h.connect.disabled, true);
  assert.equal(h.error.textContent, "");
  next.resolve({ cancelled: true });
  await tick();
  assert.equal(h.label.textContent, "Connect");
});

it("restores the action and shows an error when connecting fails", async (t) => {
  const h = await harness(t);
  h.connect.click();
  h.requests[0].reject(new Error("OAuth unavailable"));
  await tick();
  assert.equal(h.label.textContent, "Connect");
  assert.equal(h.connect.disabled, false);
  assert.equal(h.spinner.hidden, true);
  assert.equal(h.cancel.hidden, true);
  assert.equal(h.input.value, SERVER);
  assert.match(h.error.textContent, /OAuth unavailable/);
});

it("does not pretend login stopped if cancellation itself fails", async (t) => {
  const h = await harness(t, {
    cancel: async () => {
      throw new Error("IPC unavailable");
    },
  });
  h.connect.click();
  h.progress(h.requests[0].requestId, "authenticating");
  h.cancel.click();
  await tick();
  assert.equal(h.connect.disabled, true);
  assert.equal(h.label.textContent, "Authenticating…");
  assert.equal(h.cancel.disabled, false);
  assert.match(h.error.textContent, /Could not cancel sign-in/);
});
