// New-session host picker, local-machine path: "Use this machine" must connect
// and select this machine, and a host daemon already running must be picked up,
// also when the saved host pick still carries the legacy `host_` prefix.

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");

const {
  REPO_ROOT,
  desktopDepsAvailable,
  spawnServer,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();
// The omnigent CLI shim below is a bash script, so the lane is POSIX-only.
const SKIP_REASON = deps.ok
  ? process.platform === "win32" && "the omnigent CLI shim is a bash script"
  : `missing deps: ${deps.missing.join(", ")}`;
const RECORD_ROOT =
  process.env.OMNIGENT_DESKTOP_RECORD_DIR ||
  path.join(__dirname, "recordings", "run-on-this-machine");

const PYTHON = process.env.OMNIGENT_PYTHON || "python3";
const CONNECTED_MARKER = "✓ Connected";
const SELECT_TIMEOUT_MS = 45_000;
const LAST_HOST_CHOICE_KEY = "omnigent:last-host-choice";
const CHIP = '[data-testid="new-chat-landing-host-chip"]';
const HOST_MENU = '[data-testid="new-chat-landing-host-menu"]';
const RUN_ON_THIS_MACHINE = '[data-testid="new-chat-landing-run-on-this-machine"]';
const CONNECT_ERROR = '[data-testid="new-chat-landing-connect-error"]';
// "This machine" on Linux/Windows, "This Mac" on macOS (localMachineLabel).
const THIS_MACHINE = /This (machine|Mac)\b/;
// Every journey derives its environment from this snapshot and restores it.
const BASE_ENV = { ...process.env };

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function shellQuote(value) {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

function writeCliShim(dir) {
  const shim = path.join(dir, "omnigent");
  const pythonPath = [
    REPO_ROOT,
    path.join(REPO_ROOT, "sdks", "python-client"),
    path.join(REPO_ROOT, "sdks", "ui"),
  ].join(path.delimiter);
  fs.writeFileSync(
    shim,
    "#!/usr/bin/env bash\n" +
      `export PYTHONPATH=${shellQuote(pythonPath)}\${PYTHONPATH:+:$PYTHONPATH}\n` +
      `exec ${shellQuote(PYTHON)} -c "from omnigent.cli import main; main()" "$@"\n`,
    { mode: 0o755 },
  );
  return shim;
}

function prepareProfile(label, serverUrl, cliShim) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), `omni-local-host-home-${label}-`));
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), `omni-local-host-data-${label}-`));
  const settings = {
    server_url: serverUrl,
    omnigent_path: cliShim,
    // Native enrollment is preapproved because Playwright cannot click its dialog.
    allowed_hosting_origins: [new URL(serverUrl).origin],
  };
  // Dev builds derive userData from appData (see launchDesktop); seed both.
  for (const profile of [userDataDir, path.join(userDataDir, "Omnigent Dev")]) {
    fs.mkdirSync(profile, { recursive: true });
    fs.writeFileSync(path.join(profile, "settings.json"), JSON.stringify(settings, null, 2));
  }
  return { home, userDataDir };
}

function isolateEnv(home) {
  // Electron and the daemon must read the same host identity under HOME, and
  // ambient runner/host identities would override the daemon's file-based one.
  const cleanEnv = Object.fromEntries(
    Object.entries(BASE_ENV).filter(
      ([key]) => !key.startsWith("OMNIGENT_HOST_") && !key.startsWith("OMNIGENT_RUNNER_"),
    ),
  );
  delete cleanEnv.OMNIGENT_CONFIG_HOME;
  delete cleanEnv.OMNIGENT_DATA_DIR;
  cleanEnv.HOME = home;
  const noProxy = "127.0.0.1,localhost";
  for (const key of ["NO_PROXY", "no_proxy"]) {
    cleanEnv[key] = cleanEnv[key] ? `${cleanEnv[key]},${noProxy}` : noProxy;
  }
  process.env = cleanEnv;
}

function servedWindow(launched) {
  // launchDesktop already waits for the shell's loaded page; the journeys need
  // the served SPA, not the setup or server-selector page.
  const url = launched.window.url();
  assert.match(url, /^https?:/, `desktop shell did not load the served SPA: ${url}`);
  return launched.window;
}

function startHostDaemon(cliShim, serverUrl, logPath) {
  const child = spawn(cliShim, ["host", "--server", serverUrl, "--non-interactive"], {
    env: { ...process.env },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const closed = new Promise((resolve) => {
    child.once("close", resolve);
  });
  let log = "";
  const out = fs.createWriteStream(logPath);
  const closeLog = () => {
    if (!out.writableEnded) out.end();
  };
  return new Promise((resolve) => {
    let done = false;
    const finish = (connected) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve({ child, closed, connected, log });
    };
    const timer = setTimeout(() => finish(false), 60_000);
    const onData = (buf) => {
      const text = buf.toString();
      log += text;
      if (!out.writableEnded) out.write(text);
      if (log.includes(CONNECTED_MARKER)) finish(true);
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    child.on("error", (err) => {
      log += `spawn error: ${err.message}\n`;
      finish(false);
    });
    // "close" follows "exit"/"error" once the stdio pipes have drained.
    child.on("close", () => {
      closeLog();
      finish(false);
    });
  });
}

async function fetchHosts(serverUrl) {
  const res = await fetch(`${serverUrl}/v1/hosts`);
  if (!res.ok) throw new Error(`GET /v1/hosts failed: ${res.status} ${await res.text()}`);
  const body = await res.json();
  return body.hosts ?? [];
}

function stopDaemon({ child, closed }) {
  // SIGTERM first, SIGKILL after 5 s; resolve only once "close" has fired so
  // nothing is written under HOME after it is removed.
  if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
  const escalate = setTimeout(() => child.kill("SIGKILL"), 5_000);
  return closed.finally(() => clearTimeout(escalate));
}

async function waitForOnlineHost(serverUrl, timeoutMs = 20_000) {
  const deadline = Date.now() + timeoutMs;
  let hosts = [];
  /* oxlint-disable no-await-in-loop */
  while (Date.now() < deadline) {
    hosts = await fetchHosts(serverUrl);
    const online = hosts.find((h) => h.status === "online");
    if (online) return online;
    await sleep(500);
  }
  /* oxlint-enable no-await-in-loop */
  throw new Error(`no online host after connect: ${JSON.stringify(hosts)}`);
}

async function chipLabel(window) {
  // The chip renders only an icon; its label is the accessible name.
  return (await window.locator(CHIP).getAttribute("aria-label")) ?? "";
}

async function waitForChipOutcome(window) {
  const errorBox = window.locator(CONNECT_ERROR);
  const deadline = Date.now() + SELECT_TIMEOUT_MS;
  let label = "";
  /* oxlint-disable no-await-in-loop */
  while (Date.now() < deadline) {
    if ((await errorBox.count()) > 0) {
      return { label, error: (await errorBox.textContent()) ?? "(empty error)", timedOut: false };
    }
    label = await chipLabel(window);
    if (THIS_MACHINE.test(label)) return { label, error: null, timedOut: false };
    await sleep(500);
  }
  /* oxlint-enable no-await-in-loop */
  return { label, error: null, timedOut: true };
}

async function settleAndSnapshot(window, recordDir, name, extra) {
  // Open the host menu so the clip shows which row is selected, hold it, then
  // keep a still + facts.
  let menuText = null;
  try {
    await window.locator(CHIP).click();
    const menu = window.locator(HOST_MENU);
    await menu.waitFor({ state: "visible", timeout: 5_000 });
    menuText = await menu.innerText();
  } catch {
    // A chip whose menu will not open is still filmed and snapshotted.
  }
  await sleep(3000);
  await window.screenshot({ path: path.join(recordDir, `${name}.png`) });
  fs.writeFileSync(
    path.join(recordDir, `${name}.json`),
    JSON.stringify({ ...extra, menuText }, null, 2),
  );
}

async function closeJourney(journey) {
  // Stop filming before the window goes away so the clip ends on the observed
  // state, then run every step even if one throws: a crashed app must not leak
  // the daemon or the server into the next journey. Failures are returned.
  let saved = [];
  const failures = [];
  const steps = [
    () => journey.stopDisplayCapture?.(),
    () => journey.electronApp?.close(),
    () => {
      saved = saveRecording(journey.recordDir, journey.name);
    },
    () => journey.daemon && stopDaemon(journey.daemon),
    () => journey.server?.close(),
    () => journey.userDataDir && fs.rmSync(journey.userDataDir, { recursive: true, force: true }),
    () => journey.home && fs.rmSync(journey.home, { recursive: true, force: true }),
    () => {
      process.env = { ...BASE_ENV };
    },
  ];
  /* oxlint-disable no-await-in-loop */
  for (const step of steps) {
    try {
      await step();
    } catch (err) {
      failures.push(err);
    }
  }
  /* oxlint-enable no-await-in-loop */
  return { saved, failures };
}

describe("desktop shell — 'Use this machine' selects the local host", { skip: SKIP_REASON }, () => {
  let tmpDir;
  let cliShim;

  before(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-local-host-e2e-"));
    cliShim = writeCliShim(tmpDir);
  });

  after(() => {
    process.env = { ...BASE_ENV };
    if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it("connects and selects this machine when 'Use this machine' is clicked", async () => {
    const recordDir = path.join(RECORD_ROOT, "run-on-this-machine");
    fs.mkdirSync(recordDir, { recursive: true });
    const journey = { recordDir, name: "run-on-this-machine" };
    let closed;
    try {
      // Each journey gets its own host registry.
      journey.server = await spawnServer(fs.mkdtempSync(path.join(tmpDir, "srv-connect-")));
      Object.assign(journey, prepareProfile("connect", journey.server.serverUrl, cliShim));
      isolateEnv(journey.home);
      const launched = await launchDesktop({ recordDir, userDataDir: journey.userDataDir });
      journey.electronApp = launched.electronApp;
      journey.stopDisplayCapture = launched.stopDisplayCapture;
      const window = servedWindow(launched);
      const chip = window.locator(CHIP);
      await chip.waitFor({ state: "visible", timeout: 30_000 });
      await window
        .locator(`${CHIP}[aria-label*="No host"]`)
        .waitFor({ state: "visible", timeout: 30_000 });

      await chip.click();
      const runItem = window.locator(RUN_ON_THIS_MACHINE);
      await runItem.waitFor({ state: "visible", timeout: 15_000 });
      await sleep(1000);
      await runItem.click();

      const outcome = await waitForChipOutcome(window);
      const hosts = await fetchHosts(journey.server.serverUrl);
      await settleAndSnapshot(window, recordDir, "outcome", { ...outcome, hosts });
      assert.equal(
        outcome.error,
        null,
        `"Use this machine" surfaced a connect error: ${outcome.error}`,
      );
      assert.ok(
        !outcome.timedOut,
        `host chip never settled within ${SELECT_TIMEOUT_MS} ms — it reads ${JSON.stringify(outcome.label)}`,
      );
      assert.match(
        outcome.label,
        THIS_MACHINE,
        `host chip never selected this machine — it reads ${JSON.stringify(outcome.label)}`,
      );
      const online = hosts.filter((h) => h.status === "online");
      assert.equal(
        online.length,
        1,
        `expected exactly one online host after the connect, got: ${JSON.stringify(hosts)}`,
      );
    } finally {
      closed = await closeJourney(journey);
    }
    assert.equal(closed.failures.length, 0, `teardown failed: ${closed.failures.join("; ")}`);
    assert.ok(closed.saved.length > 0, "no desktop recording was produced");
  });

  it("auto-selects a local host daemon the user already started in a terminal", async () => {
    const recordDir = path.join(RECORD_ROOT, "manual-host");
    fs.mkdirSync(recordDir, { recursive: true });
    const journey = { recordDir, name: "manual-host" };
    let closed;
    try {
      journey.server = await spawnServer(fs.mkdtempSync(path.join(tmpDir, "srv-manual-")));
      Object.assign(journey, prepareProfile("manual", journey.server.serverUrl, cliShim));
      isolateEnv(journey.home);
      const daemon = await startHostDaemon(
        cliShim,
        journey.server.serverUrl,
        path.join(recordDir, "daemon.log"),
      );
      journey.daemon = daemon;
      assert.ok(daemon.connected, `omnigent host did not connect:\n${daemon.log.slice(-2000)}`);
      await waitForOnlineHost(journey.server.serverUrl);

      const launched = await launchDesktop({ recordDir, userDataDir: journey.userDataDir });
      journey.electronApp = launched.electronApp;
      journey.stopDisplayCapture = launched.stopDisplayCapture;
      const window = servedWindow(launched);
      await window.locator(CHIP).waitFor({ state: "visible", timeout: 30_000 });

      const outcome = await waitForChipOutcome(window);
      const hosts = await fetchHosts(journey.server.serverUrl);
      await settleAndSnapshot(window, recordDir, "outcome", { ...outcome, hosts });
      assert.ok(
        !outcome.timedOut,
        `host chip never settled within ${SELECT_TIMEOUT_MS} ms — it reads ${JSON.stringify(outcome.label)}`,
      );
      assert.match(
        outcome.label,
        THIS_MACHINE,
        `host chip never picked the running local host — it reads ${JSON.stringify(outcome.label)}`,
      );
    } finally {
      closed = await closeJourney(journey);
    }
    assert.equal(closed.failures.length, 0, `teardown failed: ${closed.failures.join("; ")}`);
    assert.ok(closed.saved.length > 0, "no desktop recording was produced");
  });

  it("recovers when the persisted last-host choice carries the legacy host_ prefix", async () => {
    const recordDir = path.join(RECORD_ROOT, "legacy-host-choice");
    fs.mkdirSync(recordDir, { recursive: true });
    const journey = { recordDir, name: "legacy-host-choice" };
    let closed;
    try {
      journey.server = await spawnServer(fs.mkdtempSync(path.join(tmpDir, "srv-legacy-")));
      Object.assign(journey, prepareProfile("legacy", journey.server.serverUrl, cliShim));
      isolateEnv(journey.home);
      const daemon = await startHostDaemon(
        cliShim,
        journey.server.serverUrl,
        path.join(recordDir, "daemon.log"),
      );
      journey.daemon = daemon;
      assert.ok(daemon.connected, `omnigent host did not connect:\n${daemon.log.slice(-2000)}`);
      const { host_id: hostId } = await waitForOnlineHost(journey.server.serverUrl);

      const launched = await launchDesktop({ recordDir, userDataDir: journey.userDataDir });
      journey.electronApp = launched.electronApp;
      journey.stopDisplayCapture = launched.stopDisplayCapture;
      const window = servedWindow(launched);
      const chip = window.locator(CHIP);
      await chip.waitFor({ state: "visible", timeout: 30_000 });
      const beforeSeed = await waitForChipOutcome(window);
      assert.ok(
        !beforeSeed.timedOut && beforeSeed.error === null,
        `chip did not settle before seeding the legacy choice: ${JSON.stringify(beforeSeed)}`,
      );

      // Seed the legacy `host_<id>` spelling an older build persisted.
      await window.evaluate(
        ([key, id]) => localStorage.setItem(key, `host_${id}`),
        [LAST_HOST_CHOICE_KEY, hostId],
      );
      await window.reload();
      await chip.waitFor({ state: "visible", timeout: 30_000 });

      const outcome = await waitForChipOutcome(window);
      const hosts = await fetchHosts(journey.server.serverUrl);
      const storedChoice = await window.evaluate(
        (key) => localStorage.getItem(key),
        LAST_HOST_CHOICE_KEY,
      );
      await settleAndSnapshot(window, recordDir, "outcome", {
        beforeSeed,
        ...outcome,
        storedChoice,
        hosts,
      });
      assert.ok(
        !outcome.timedOut,
        `host chip never settled within ${SELECT_TIMEOUT_MS} ms — it reads ${JSON.stringify(outcome.label)}`,
      );
      assert.match(
        outcome.label,
        THIS_MACHINE,
        `host chip never recovered from the legacy-prefixed stored choice — it reads ` +
          `${JSON.stringify(outcome.label)}`,
      );
    } finally {
      closed = await closeJourney(journey);
    }
    assert.equal(closed.failures.length, 0, `teardown failed: ${closed.failures.join("; ")}`);
    assert.ok(closed.saved.length > 0, "no desktop recording was produced");
  });
});
