// Desktop e2e: a Databricks workspace session across a VPN drop. The workspace
// is a local stand-in (fixtures/fakeDatabricksWorkspace.js) whose "off VPN"
// mode answers like a workspace IP access list.
"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { desktopDepsAvailable, launchDesktop, saveRecording } = require("./desktopHarness");
const { startDesignBackend } = require("./desktopDesignPromptHarness");
const { startFakeDatabricksWorkspace } = require("./fixtures/fakeDatabricksWorkspace");

const deps = desktopDepsAvailable();
const HOST = "accounts.cloud.databricks.com";
const WORKSPACE_URL = `https://${HOST}/omnigent`;
const PRELOADS = [
  path.join(__dirname, "fixtures", "fakeSystemBrowser.cjs"),
  path.join(__dirname, "fixtures", "databricksStandInNetwork.cjs"),
];
const OUT_DIR =
  process.env.OMNIGENT_E2E_VPN_OUT_DIR || path.join(__dirname, "recordings", "databricks-vpn");
const MESSAGE = "Hello from the office";
const REPLY = "Hi! Working on it from the office.";
const SCENARIOS = (process.env.OMNIGENT_E2E_VPN_SCENARIOS || "token-valid")
  .split(",")
  .map((name) => name.trim())
  .filter(Boolean);

const sleep = (ms) =>
  new Promise((resolve) => {
    setTimeout(resolve, ms);
  });

async function waitFor(check, timeoutMs, intervalMs = 500) {
  const deadline = Date.now() + timeoutMs;
  // oxlint-disable no-await-in-loop -- polling.
  while (Date.now() < deadline) {
    const value = await check();
    if (value) return value;
    await sleep(intervalMs);
  }
  // oxlint-enable no-await-in-loop
  return null;
}

/** What the desktop window shows: the page URL, the overlay, and setup-page text. */
async function screenState(app) {
  const window = await app.electronApp.browserWindow(app.window);
  const shell = await window.evaluate((win) => {
    if (!win) return null;
    const overlay = win.contentView.children
      .map((view) => view.webContents?.getURL?.() ?? "")
      .find((url) => url.includes("reconnect-overlay/index.html"));
    return { url: win.webContents.getURL(), overlayUrl: overlay ?? null };
  });
  await window.dispose();
  const state = {
    at: new Date().toISOString(),
    url: shell?.url ?? null,
    overlay: null,
    setup: null,
  };
  if (shell?.overlayUrl) {
    const page = app.electronApp.windows().find((p) => p.url() === shell.overlayUrl);
    state.overlay = page
      ? await page
          .evaluate(() => document.body.innerText.replace(/\s+/g, " ").trim())
          .catch(() => "?")
      : "shown";
  }
  if (state.url && /\/setup\/|server-selector/.test(state.url)) {
    state.setup = await app.window
      .evaluate(() => document.body.innerText.replace(/\s+/g, " ").trim().slice(0, 400))
      .catch(() => null);
  }
  return state;
}

function timeline(scenario) {
  const file = path.join(OUT_DIR, `${scenario}-screen.jsonl`);
  let last = "";
  return {
    note(event) {
      fs.appendFileSync(file, `${JSON.stringify({ at: new Date().toISOString(), event })}\n`);
    },
    async sample(app) {
      const state = await screenState(app).catch((error) => ({ error: String(error) }));
      const key = JSON.stringify({ ...state, at: undefined });
      if (key !== last) {
        last = key;
        fs.appendFileSync(file, `${JSON.stringify(state)}\n`);
      }
      return state;
    },
  };
}

async function watch(app, log, ms) {
  const deadline = Date.now() + ms;
  let state;
  // oxlint-disable no-await-in-loop -- sampling the screen over time.
  while (Date.now() < deadline) {
    state = await log.sample(app);
    await sleep(1000);
  }
  // oxlint-enable no-await-in-loop
  return state;
}

async function signInAndOpenConversation(app, workspace, log, sessionId) {
  const urlField = app.window.locator("#url");
  await urlField.waitFor({ state: "visible", timeout: 20_000 });
  await sleep(1500);
  await urlField.fill("");
  await urlField.pressSequentially(WORKSPACE_URL, { delay: 25 });
  assert.equal(await urlField.inputValue(), WORKSPACE_URL);
  log.note("typed workspace URL");
  await app.window.locator("#connect").click();
  log.note("clicked Connect");
  await app.window
    .getByText("What should we build?")
    .waitFor({ state: "visible", timeout: 60_000 });
  log.note("signed in: home visible");
  await app.window.locator(`[data-sidebar-session-id="${sessionId}"]`).first().click();
  await app.window.waitForURL(`${WORKSPACE_URL}/c/${sessionId}`, { timeout: 30_000 });
  const conversationUrl = app.window.url();
  log.note({ conversation: conversationUrl });
  const composer = app.window.getByLabel("Message the agent");
  await composer.click();
  await composer.pressSequentially(MESSAGE, { delay: 30 });
  await app.window.keyboard.press("Enter");
  log.note("sent first message");
  await app.window.getByText(REPLY).first().waitFor({ state: "visible", timeout: 60_000 });
  log.note("agent replied");
  await sleep(4000);
  assert.equal(workspace.counts.authorize, 1);
  return conversationUrl;
}

async function onConversation(app, conversationUrl) {
  const state = await screenState(app);
  const replyVisible = await app.window
    .getByText(REPLY)
    .first()
    .isVisible()
    .catch(() => false);
  return state.url === conversationUrl && !state.overlay && replyVisible ? state : null;
}

const SCENARIO_CONFIG = {
  smoke: { accessTokenTtlSeconds: 3600, cookieMaxAgeSeconds: 600 },
  "token-valid": { accessTokenTtlSeconds: 3600, cookieMaxAgeSeconds: 60 },
  "refresh-blocked": {
    accessTokenTtlSeconds: 300,
    cookieMaxAgeSeconds: 60,
    tokenEndpointBlockedOffVpn: true,
  },
  "refresh-allowed": { accessTokenTtlSeconds: 300, cookieMaxAgeSeconds: 60 },
  reload: { accessTokenTtlSeconds: 3600, cookieMaxAgeSeconds: 600 },
};

async function runScenario(scenario, server) {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), `omni-dbx-vpn-${scenario}-`));
  const home = path.join(tmp, "home");
  fs.mkdirSync(home, { recursive: true });
  const log = timeline(scenario);
  const workspace = await startFakeDatabricksWorkspace({
    host: HOST,
    upstreamUrl: server.serverUrl,
    logPath: path.join(OUT_DIR, `${scenario}-workspace.jsonl`),
    ...SCENARIO_CONFIG[scenario],
  });
  const recordDir = path.join(OUT_DIR, `raw-${scenario}`);
  const mainLog = fs.createWriteStream(path.join(OUT_DIR, `${scenario}-main.log`));
  const result = { scenario, config: SCENARIO_CONFIG[scenario] };
  let app;
  try {
    const sessionId = await server.createSession();
    app = await launchDesktop({
      recordDir,
      userDataDir: path.join(tmp, "user-data"),
      preload: PRELOADS,
      env: {
        HOME: home,
        OMNIGENT_DESKTOP_VERSION_OVERRIDE: "999.0.0",
        OMNIGENT_E2E_DATABRICKS_HOST: HOST,
        OMNIGENT_E2E_DATABRICKS_PLAIN_URL: workspace.plainUrl,
      },
    });
    const child = app.electronApp.process();
    for (const stream of [child.stdout, child.stderr]) {
      stream?.on("data", (chunk) => {
        const stamp = new Date().toISOString();
        for (const line of String(chunk).split("\n")) {
          if (line.trim() && !/dbus|Fontconfig|libva/i.test(line))
            mainLog.write(`${stamp} ${line}\n`);
        }
      });
    }
    const conversationUrl = await signInAndOpenConversation(app, workspace, log, sessionId);
    result.conversationUrl = conversationUrl;
    await log.sample(app);
    if (scenario !== "smoke") {
      Object.assign(result, await SCENARIO_STEPS[scenario](app, workspace, log, conversationUrl));
    }
    result.browserSignIns = workspace.counts.authorize;
    result.browserOpened = (
      await app.electronApp.evaluate(() => globalThis.fakeBrowserOpened)
    ).length;
    result.counts = { ...workspace.counts };
  } catch (error) {
    result.error = String(error?.stack ?? error);
    throw error;
  } finally {
    fs.writeFileSync(
      path.join(OUT_DIR, `${scenario}-result.json`),
      JSON.stringify(result, null, 2),
    );
    if (app) {
      // Stop filming before the window closes so the clip ends on the journey's outcome.
      await app.stopDisplayCapture();
      await app.electronApp.close().catch(() => {});
      result.recordings = saveRecording(recordDir, scenario);
      fs.writeFileSync(
        path.join(OUT_DIR, `${scenario}-result.json`),
        JSON.stringify(result, null, 2),
      );
    }
    mainLog.end();
    await workspace.close();
    fs.rmSync(tmp, { recursive: true, force: true });
  }
  return result;
}

const SCENARIO_STEPS = {
  async "token-valid"(app, workspace, log, conversationUrl) {
    workspace.setVpn(false);
    log.note("left VPN");
    const renewed = await waitFor(() => workspace.counts.sessionCreate >= 2, 90_000);
    const offVpn = await watch(app, log, 25_000);
    workspace.setVpn(true);
    log.note("rejoined VPN");
    const back = await waitFor(
      async () => (await log.sample(app), onConversation(app, conversationUrl)),
      60_000,
      1000,
    );
    if (back) await watch(app, log, 4000);
    return {
      renewalAttemptedOffVpn: Boolean(renewed),
      offVpn,
      recoveredWithoutClick: Boolean(back),
    };
  },
  async "refresh-allowed"(app, workspace, log, conversationUrl) {
    return SCENARIO_STEPS["token-valid"](app, workspace, log, conversationUrl);
  },
  async "refresh-blocked"(app, workspace, log, conversationUrl) {
    workspace.setVpn(false);
    log.note("left VPN");
    const refused = await waitFor(
      () =>
        workspace.requests.some(
          (request) =>
            !request.vpn &&
            request.path === "/oidc/v1/token" &&
            request.detail === "refresh_token" &&
            request.status === 403,
        ),
      90_000,
    );
    assert.ok(refused, "the stand-in never refused a token refresh off the network");
    const offVpn = await watch(app, log, 20_000);
    const connect = app.window.locator("#connect");
    let clickedOffVpn = false;
    if (await connect.isVisible().catch(() => false)) {
      await connect.click();
      clickedOffVpn = true;
      log.note("clicked Connect off VPN");
      await watch(app, log, 15_000);
    }
    workspace.setVpn(true);
    log.note("rejoined VPN");
    const back = await waitFor(
      async () => (await log.sample(app), onConversation(app, conversationUrl)),
      60_000,
      1000,
    );
    const afterWait = await screenState(app);
    let afterClick = null;
    if (!back && (await connect.isVisible().catch(() => false))) {
      await connect.click();
      log.note("clicked Connect after VPN returned");
      afterClick = await watch(app, log, 20_000);
    } else if (back) {
      await watch(app, log, 4000);
    }
    return {
      offVpn,
      clickedOffVpn,
      recoveredWithoutClick: Boolean(back),
      afterWait,
      afterClick,
      returnedToConversation: (afterClick?.url ?? afterWait.url) === conversationUrl,
    };
  },
  async reload(app, workspace, log, conversationUrl) {
    workspace.setVpn(false);
    log.note("left VPN");
    await watch(app, log, 5000);
    const window = await app.electronApp.browserWindow(app.window);
    await window.evaluate((win) => win.webContents.reload());
    await window.dispose();
    log.note("reloaded the conversation window");
    const offVpn = await watch(app, log, 20_000);
    assert.ok(
      workspace.requests.some(
        (request) => !request.vpn && request.path.startsWith("/omnigent") && request.status === 403,
      ),
      "the stand-in never refused the conversation reload",
    );
    workspace.setVpn(true);
    log.note("rejoined VPN");
    const back = await waitFor(
      async () => (await log.sample(app), onConversation(app, conversationUrl)),
      60_000,
      1000,
    );
    const afterWait = await screenState(app);
    let afterClick = null;
    const connect = app.window.locator("#connect");
    if (!back && (await connect.isVisible().catch(() => false))) {
      await connect.click();
      log.note("clicked Connect after VPN returned");
      afterClick = await watch(app, log, 20_000);
    } else if (back) {
      await watch(app, log, 4000);
    }
    return {
      offVpn,
      recoveredWithoutClick: Boolean(back),
      afterWait,
      afterClick,
      returnedToConversation: (afterClick?.url ?? afterWait.url) === conversationUrl,
    };
  },
};

describe(
  "desktop shell — Databricks workspace session across a VPN drop",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let server;

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-dbx-vpn-server-"));
      server = await startDesignBackend(tmpDir, {
        serverEnv: { OMNIGENT_WEB_BASE_PATH: "/omnigent" },
      });
      await fetch(`${server.mockUrl}/mock/set_fallback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: "gpt-4o-mini", text: REPLY, stream: true }),
      });
    });

    after(async () => {
      if (server) await server.close();
      if (tmpDir) {
        fs.mkdirSync(OUT_DIR, { recursive: true });
        for (const name of ["server.log", "runner.log", "mock.log"]) {
          const source = path.join(tmpDir, name);
          if (fs.existsSync(source)) fs.copyFileSync(source, path.join(OUT_DIR, `backend-${name}`));
        }
        fs.rmSync(tmpDir, { recursive: true, force: true });
      }
    });

    for (const scenario of SCENARIOS) {
      it(
        `${scenario}: keeps the conversation without a new sign-in`,
        { timeout: 420_000 },
        async () => {
          const result = await runScenario(scenario, server);
          assert.equal(result.browserSignIns, 1, "the app asked for another browser sign-in");
          if (scenario !== "smoke") {
            assert.ok(
              result.recoveredWithoutClick,
              `the conversation did not come back on its own after the VPN returned: ${JSON.stringify(result.afterWait ?? result.offVpn)}`,
            );
          }
        },
      );
    }
  },
);
