// Real desktop shell and `omnigent server` with a fixture arca CLI (no live Arca or SSH).
// Run after building the SPA: node --test web/electron/e2e/desktop_arca_connect_background.e2e.js
"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { setTimeout: sleep } = require("node:timers/promises");
const {
  APP_ROOT,
  desktopDepsAvailable,
  launchDesktop,
  saveRecording,
  spawnServer,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();
const HOST_ID = "fedcba9876543210fedcba9876543210";
const testOptions = {
  timeout: 180_000,
  skip: !deps.ok ? `missing deps: ${deps.missing.join(", ")}` : process.platform === "win32",
};

/* oxlint-disable no-shadow -- Serialized entrypoints run in separate processes. */
async function fixtureArca({ stateDir, hostId }) {
  const fs = require("node:fs");
  const path = require("node:path");
  const { setTimeout: sleep } = require("node:timers/promises");
  const file = (name) => path.join(stateDir, name);
  const event = (name, extra = {}) =>
    fs.appendFileSync(file("events.jsonl"), JSON.stringify({ name, ...extra }) + "\n");
  const args = process.argv.slice(2);
  event("command", { args, pid: process.pid });
  if (args[0] === "status") {
    process.stdout.write(JSON.stringify({ status: "running", instance: { shutdown_time: null } }));
    return;
  }
  event("connect-start", { pid: process.pid });
  process.stdout.write("Checking your Arca instance…\n");
  await sleep(600);
  process.stdout.write("Attempting to start your Classic Arca instance (cold start)\n");
  for (let waited = 1; ; waited += 1) {
    if (fs.existsSync(file("fail"))) {
      process.stderr.write("arca: the instance did not become reachable over SSH\n");
      event("connect-exit", { pid: process.pid });
      process.exitCode = 255;
      return;
    }
    if (fs.existsSync(file("release"))) break;
    process.stdout.write(`Waiting for the instance to accept SSH… ${waited}s\n`);
    // oxlint-disable-next-line no-await-in-loop -- Stream progress until the test releases it.
    await sleep(1000);
  }
  process.stdout.write("Starting the Omnigent host daemon…\nArca host registered.\n");
  process.stdout.write("__OMNIGENT_ARCA_IDENTITY_START__\n");
  process.stdout.write(
    JSON.stringify({
      daemons: [
        {
          mode: "server",
          process: "online",
          host_status: "online",
          error: null,
          target: args.find((arg) => arg.startsWith("'http"))?.slice(1, -1),
          server_url: args.find((arg) => arg.startsWith("'http"))?.slice(1, -1),
          host_id: hostId,
        },
      ],
    }),
  );
  process.stdout.write("\n__OMNIGENT_ARCA_IDENTITY_END__\n");
  event("connect-complete");
  event("connect-exit", { pid: process.pid });
}

/** Treat the local server as Databricks-managed and use the fixture arca, before main loads. */
function fixturePolicy({ appRoot, arcaPath, serverUrl }) {
  const path = require("node:path");
  const src = path.join(appRoot, "src");
  const managed = require(path.join(src, "managed_preferences"));
  managed.getDatabricksInternalFeaturesEnabled = () => true;
  require(path.join(src, "url")).isDatabricksManagedServerUrl = (url) => {
    try {
      return typeof url === "string" && new URL(url).origin === new URL(serverUrl).origin;
    } catch {
      return false;
    }
  };
  const arca = require(path.join(src, "arca"));
  arca.resolveArcaPath = () => arcaPath;
  arca.resolveArcaPathAsync = async () => arcaPath;
  const startConnect = arca.startArcaConnect;
  arca.startArcaConnect = (url, deps = {}) =>
    startConnect(url, { ...deps, resolveArcaPath: () => arcaPath, timeoutMs: 120_000 });
  const readStatus = arca.readArcaStatus;
  arca.readArcaStatus = (deps = {}) =>
    readStatus({ ...deps, resolveArcaPath: () => arcaPath, timeoutMs: 2000 });
}
/* oxlint-enable no-shadow */

async function waitUntil(predicate, message, timeoutMs = 20_000) {
  const deadline = Date.now() + timeoutMs;
  // oxlint-disable-next-line no-await-in-loop -- Each probe waits for the prior result.
  while (!(await predicate())) {
    assert.ok(Date.now() < deadline, message);
    // oxlint-disable-next-line no-await-in-loop -- Poll subprocess or Electron state.
    await sleep(50);
  }
}

function processExists(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    assert.equal(error.code, "ESRCH");
    return false;
  }
}

/** Main-process view of every window: the shell, the consent sheet, the output window. */
function windowStates(electronApp) {
  return electronApp.evaluate(({ BrowserWindow }) =>
    BrowserWindow.getAllWindows()
      .filter((win) => !win.isDestroyed())
      .map((win) => ({
        id: win.id,
        url: win.webContents.getURL(),
        modal: win.isModal(),
        enabled: win.isEnabled(),
        visible: win.isVisible(),
      })),
  );
}

const isConsole = (state) => state.url.includes("/arca-connect/index.html") && state.visible;

/** The newest console page (consent sheet or output window) once it is visible. */
async function consolePage(electronApp, previous = null) {
  await waitUntil(async () => (await windowStates(electronApp)).some(isConsole), "No console");
  let page;
  await waitUntil(() => {
    page = electronApp
      .windows()
      .findLast((candidate) => candidate.url().includes("/arca-connect/index.html"));
    return page && page !== previous && !page.isClosed();
  }, "Console page not attached");
  await page.locator("#title").waitFor();
  return page;
}

async function withDesktop(name, run) {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-arca-background-"));
  const recordDir = process.env.OMNIGENT_DESKTOP_RECORD_DIR
    ? path.join(path.resolve(process.env.OMNIGENT_DESKTOP_RECORD_DIR), name)
    : fs.mkdtempSync(path.join(os.tmpdir(), `omni-arca-background-${name}-`));
  fs.mkdirSync(recordDir, { recursive: true });
  const eventsFile = path.join(stateDir, "events.jsonl");
  const readEvents = () =>
    fs.existsSync(eventsFile)
      ? fs.readFileSync(eventsFile, "utf8").trim().split("\n").map(JSON.parse)
      : [];
  let server;
  let desktop;
  try {
    server = await spawnServer(stateDir);
    const serverUrl = server.serverUrl;
    const cliSource = path.join(stateDir, "arca-fixture.cjs");
    const arcaPath = path.join(stateDir, "arca");
    const policyPath = path.join(stateDir, "policy.cjs");
    const quote = (value) => `'${value.replaceAll("'", "'\\''")}'`;
    fs.writeFileSync(
      cliSource,
      `(${fixtureArca.toString()})(${JSON.stringify({ stateDir, hostId: HOST_ID })}).catch(error => { console.error(error); process.exitCode = 1; });\n`,
    );
    fs.writeFileSync(
      arcaPath,
      `#!/bin/sh\nexec ${quote(process.execPath)} ${quote(cliSource)} "$@"\n`,
      { mode: 0o700 },
    );
    fs.writeFileSync(
      policyPath,
      `(${fixturePolicy.toString()})(${JSON.stringify({ appRoot: APP_ROOT, arcaPath, serverUrl })});\n`,
    );
    desktop = await launchDesktop({
      recordDir,
      serverUrl,
      userDataDir: path.join(stateDir, "desktop-profile"),
      preload: [policyPath],
      env: { OMNIGENT_ARCA_AUTO_CONNECT: "" },
    });
    const page = desktop.window;
    await page.getByTestId("new-chat-landing-host-chip").waitFor({ timeout: 30_000 });
    // Filming setup is done; the journey starts here. Recorded so the clip can
    // be cut at a measured offset rather than a guessed one.
    fs.writeFileSync(path.join(recordDir, "journey-start.txt"), String(Date.now()));
    await run({ ...desktop, page, stateDir, readEvents });
  } finally {
    if (desktop) {
      await desktop.electronApp.close().catch(() => {});
      await desktop.stopDisplayCapture();
      // The display capture's name carries its start time; keep it past the rename.
      const capture = fs.readdirSync(recordDir).find((file) => /^display@\d+\.webm$/.test(file));
      if (capture) {
        fs.writeFileSync(path.join(recordDir, "capture-start.txt"), capture.match(/\d+/)[0]);
      }
      saveRecording(recordDir, name);
    }
    // Only connects that never exited can still be running; other pids may be reused.
    let running = [];
    try {
      const events = readEvents();
      const exited = new Set(events.filter((e) => e.name === "connect-exit").map((e) => e.pid));
      running = events.filter((e) => e.name === "connect-start" && !exited.has(e.pid));
    } catch {
      /* An unreadable events log leaves nothing known to kill. */
    }
    for (const event of running) {
      try {
        process.kill(event.pid, "SIGKILL");
      } catch {
        /* The fixture already exited. */
      }
    }
    if (server) await server.close();
    fs.rmSync(stateDir, { recursive: true, force: true });
    console.log(`Arca background connect evidence: ${recordDir}`);
  }
}

async function startRunOnArca(page) {
  await page.getByTestId("new-chat-landing-host-chip").click();
  await page.getByTestId("new-chat-landing-run-on-arca").click();
}

it("connects Arca in the background after consent and keeps the app usable", testOptions, () =>
  withDesktop("arca-connect-background", async ({ electronApp, page, stateDir, readEvents }) => {
    await sleep(800);
    await startRunOnArca(page);
    const consent = await consolePage(electronApp);
    await consent.getByText("Connect your Arca instance?", { exact: true }).waitFor();
    const asking = (await windowStates(electronApp)).find(isConsole);
    assert.equal(asking.modal, true);
    // Consent first: nothing ran yet.
    assert.equal(
      readEvents().some((event) => event.name === "connect-start"),
      false,
    );
    await sleep(1500);

    await consent.getByRole("button", { name: "Connect", exact: true }).click();
    await waitUntil(
      async () => !(await windowStates(electronApp)).some(isConsole),
      "The consent sheet stayed open after Connect",
    );
    await waitUntil(
      () => readEvents().some((event) => event.name === "connect-start"),
      "The connect did not start",
    );
    const shell = (await windowStates(electronApp)).find((state) => /^https?:/.test(state.url));
    assert.equal(shell.enabled, true, "The app window is blocked while Arca connects");

    const progress = page.getByTestId("new-chat-landing-arca-progress");
    await progress.waitFor();
    assert.match(await progress.innerText(), /Connecting to Arca…/);
    const composer = page.getByTestId("new-chat-landing-composer").locator("textarea");
    await composer.click();
    await composer.pressSequentially("Draft this while Arca starts", { delay: 60 });
    assert.equal(await composer.inputValue(), "Draft this while Arca starts");
    await sleep(2500);

    fs.writeFileSync(path.join(stateDir, "release"), "release");
    await waitUntil(
      () => readEvents().some((event) => event.name === "connect-complete"),
      "The connect did not finish",
    );
    await progress.waitFor({ state: "detached" });
    assert.equal(await composer.inputValue(), "Draft this while Arca starts");
    assert.equal((await windowStates(electronApp)).some(isConsole), false);
    await sleep(2000);
  }),
);

it(
  "shows a running connect's output in a window that can be hidden, then stops it",
  testOptions,
  () =>
    withDesktop("arca-connect-output", async ({ electronApp, page, readEvents }) => {
      await sleep(800);
      await startRunOnArca(page);
      const consent = await consolePage(electronApp);
      await sleep(800);
      await consent.getByRole("button", { name: "Connect", exact: true }).click();
      await page.getByTestId("new-chat-landing-arca-progress").waitFor();
      await sleep(2500);

      await page.getByTestId("new-chat-landing-arca-show-output").click();
      const output = await consolePage(electronApp, consent);
      await output.getByText("Connecting your Arca instance", { exact: true }).waitFor();
      const outputState = (await windowStates(electronApp)).find(isConsole);
      assert.equal(outputState.modal, false);
      const shell = (await windowStates(electronApp)).find((state) => /^https?:/.test(state.url));
      assert.equal(shell.enabled, true);
      // The backlog from before the window opened is replayed, then the stream continues.
      await output.getByText("Attempting to start your Classic Arca instance").waitFor();
      await output
        .getByText(/Waiting for the instance to accept SSH… [3-9]s/)
        .first()
        .waitFor();
      await sleep(2000);

      await output.getByRole("button", { name: "Hide", exact: true }).click();
      await waitUntil(
        async () => !(await windowStates(electronApp)).some(isConsole),
        "Hide left the output window open",
      );
      const connect = readEvents().find((event) => event.name === "connect-start");
      assert.equal(processExists(connect.pid), true, "Hiding the output stopped the connect");
      await page.getByTestId("new-chat-landing-arca-progress").waitFor();
      await sleep(2500);

      await page.getByTestId("new-chat-landing-arca-show-output").click();
      const reopened = await consolePage(electronApp, output);
      await reopened
        .getByText(/Waiting for the instance to accept SSH… ([6-9]|1\d)s/)
        .first()
        .waitFor();
      await sleep(1500);
      await reopened.getByRole("button", { name: "Stop connecting", exact: true }).click();
      await waitUntil(() => !processExists(connect.pid), "Stop connecting left arca running");
      await waitUntil(
        async () => !(await windowStates(electronApp)).some(isConsole),
        "The output window stayed open after Stop connecting",
      );
      await page.getByTestId("new-chat-landing-arca-progress").waitFor({ state: "detached" });
      // A stop is the user's choice, so the page shows no error for it.
      assert.equal(await page.getByTestId("new-chat-landing-arca-error").count(), 0);
      await sleep(2000);
    }),
);

it("opens the output with the error when the background connect fails", testOptions, () =>
  withDesktop("arca-connect-failure", async ({ electronApp, page, stateDir, readEvents }) => {
    await startRunOnArca(page);
    const consent = await consolePage(electronApp);
    await consent.getByRole("button", { name: "Connect", exact: true }).click();
    await waitUntil(
      async () => !(await windowStates(electronApp)).some(isConsole),
      "The consent sheet stayed open after Connect",
    );
    await waitUntil(
      () => readEvents().some((event) => event.name === "connect-start"),
      "The connect did not start",
    );
    fs.writeFileSync(path.join(stateDir, "fail"), "fail");
    const output = await consolePage(electronApp, consent);
    await output
      .locator("#status[data-kind='error']", {
        hasText: "the instance did not become reachable over SSH",
      })
      .waitFor();
    assert.equal(await output.getByRole("button", { name: "Close", exact: true }).count(), 1);
    // The output window reported it, so the page doesn't repeat the error.
    await page.getByTestId("new-chat-landing-arca-progress").waitFor({ state: "detached" });
    assert.equal(await page.getByTestId("new-chat-landing-arca-error").count(), 0);
  }),
);
