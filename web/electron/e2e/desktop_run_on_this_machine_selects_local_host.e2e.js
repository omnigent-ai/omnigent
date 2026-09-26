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
const RECORD_ROOT = path.join(__dirname, "recordings", "run-on-this-machine");

const PYTHON = process.env.OMNIGENT_PYTHON || "python3";
const CONNECTED_MARKER = "✓ Connected";
const SELECT_TIMEOUT_MS = 45_000;
const LAST_HOST_CHOICE_KEY = "omnigent:last-host-choice";

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
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
      `export PYTHONPATH="${pythonPath}\${PYTHONPATH:+:$PYTHONPATH}"\n` +
      `exec "${PYTHON}" -c "from omnigent.cli import main; main()" "$@"\n`,
    { mode: 0o755 },
  );
  return shim;
}

function prepareProfile(label, serverUrl, cliShim) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), `omni-local-host-home-${label}-`));
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), `omni-local-host-data-${label}-`));
  fs.writeFileSync(
    path.join(userDataDir, "settings.json"),
    JSON.stringify(
      {
        server_url: serverUrl,
        omnigent_path: cliShim,
        // Native enrollment is preapproved because Playwright cannot click its dialog.
        allowed_hosting_origins: [new URL(serverUrl).origin],
      },
      null,
      2,
    ),
  );
  return { home, userDataDir };
}

function isolateEnv(home) {
  process.env.HOME = home;
  // Electron and the daemon must read the same host identity under HOME.
  delete process.env.OMNIGENT_CONFIG_HOME;
  delete process.env.OMNIGENT_DATA_DIR;
  // Ambient identities would override the daemon's file-based identity.
  const cleanEnv = Object.fromEntries(
    Object.entries(process.env).filter(
      ([key]) => !key.startsWith("OMNIGENT_HOST_") && !key.startsWith("OMNIGENT_RUNNER_"),
    ),
  );
  process.env = cleanEnv;
  const noProxy = ["127.0.0.1", "localhost"];
  for (const key of ["NO_PROXY", "no_proxy"]) {
    const existing = process.env[key];
    process.env[key] = existing ? `${existing},${noProxy.join(",")}` : noProxy.join(",");
  }
}

async function waitForSpaWindow(electronApp, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  // The update overlay can open first; select the served SPA window.
  /* oxlint-disable no-await-in-loop */
  while (Date.now() < deadline) {
    const spa = electronApp.windows().find((w) => w.url().startsWith("http"));
    if (spa) return spa;
    await sleep(500);
  }
  /* oxlint-enable no-await-in-loop */
  throw new Error("no SPA window (http…) appeared within the deadline");
}

function startHostDaemon(cliShim, serverUrl) {
  const child = spawn(cliShim, ["host", "--server", serverUrl, "--non-interactive"], {
    env: { ...process.env },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let log = "";
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve({ child, connected: false, log }), 60_000);
    const onData = (buf) => {
      log += buf.toString();
      if (log.includes(CONNECTED_MARKER)) {
        clearTimeout(timer);
        resolve({ child, connected: true, log });
      }
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    child.on("exit", () => {
      clearTimeout(timer);
      resolve({ child, connected: false, log });
    });
  });
}

async function waitForChipOutcome(window) {
  const chip = window.locator('[data-testid="new-chat-landing-host-chip"]');
  const errorBox = window.locator('[data-testid="new-chat-landing-connect-error"]');
  const deadline = Date.now() + SELECT_TIMEOUT_MS;
  let label = "";
  /* oxlint-disable no-await-in-loop */
  while (Date.now() < deadline) {
    if ((await errorBox.count()) > 0) {
      return { label, error: (await errorBox.textContent()) ?? "(empty error)" };
    }
    label = (await chip.textContent()) ?? "";
    if (label.includes("This machine")) return { label, error: null };
    await sleep(500);
  }
  /* oxlint-enable no-await-in-loop */
  return { label, error: null };
}

describe(
  "desktop shell — 'Run on this machine' selects the local host",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let cliShim;

    before(() => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-local-host-e2e-"));
      cliShim = writeCliShim(tmpDir);
    });

    after(() => {
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it("connects and selects this machine when 'Run on this machine' is clicked", async () => {
      // Each journey gets its own host registry.
      const server = await spawnServer(fs.mkdtempSync(path.join(tmpDir, "srv-connect-")));
      const { home, userDataDir } = prepareProfile("connect", server.serverUrl, cliShim);
      isolateEnv(home);
      const recordDir = path.join(RECORD_ROOT, "run-on-this-machine");
      const { electronApp } = await launchDesktop({ recordDir, userDataDir });
      let saved;
      try {
        const window = await waitForSpaWindow(electronApp);
        const chip = window.locator('[data-testid="new-chat-landing-host-chip"]');
        await chip.waitFor({ state: "visible", timeout: 30_000 });
        await window
          .locator('[data-testid="new-chat-landing-host-chip"]:has-text("No hosts")')
          .waitFor({ state: "visible", timeout: 30_000 });

        await chip.click();
        const runItem = window.locator('[data-testid="new-chat-landing-run-on-this-machine"]');
        await runItem.waitFor({ state: "visible", timeout: 15_000 });
        await runItem.click();

        const outcome = await waitForChipOutcome(window);
        assert.equal(
          outcome.error,
          null,
          `"Run on this machine" surfaced a connect error: ${outcome.error}`,
        );
        assert.ok(
          outcome.label.includes("This machine"),
          `host chip never selected this machine — it reads ${JSON.stringify(outcome.label)} ` +
            '(the reported symptom is it falling back to "Choose host")',
        );

        const res = await fetch(`${server.serverUrl}/v1/hosts`);
        const body = await res.json();
        const online = (body.hosts ?? []).filter((h) => h.status === "online");
        assert.equal(
          online.length,
          1,
          `expected exactly one online host after the connect, got: ${JSON.stringify(body)}`,
        );
      } finally {
        await electronApp.close();
        saved = saveRecording(recordDir, "run-on-this-machine");
        await server.close();
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
      assert.ok(saved && saved.length > 0, "no desktop recording was produced");
    });

    it("auto-selects a local host daemon the user already started in a terminal", async () => {
      const server = await spawnServer(fs.mkdtempSync(path.join(tmpDir, "srv-manual-")));
      const { home, userDataDir } = prepareProfile("manual", server.serverUrl, cliShim);
      isolateEnv(home);

      const { child, connected, log } = await startHostDaemon(cliShim, server.serverUrl);

      const recordDir = path.join(RECORD_ROOT, "manual-host");
      let electronApp;
      let saved;
      try {
        assert.ok(connected, `omnigent host did not connect:\n${log.slice(-2000)}`);

        const launched = await launchDesktop({ recordDir, userDataDir });
        electronApp = launched.electronApp;
        const window = await waitForSpaWindow(electronApp);

        const chip = window.locator('[data-testid="new-chat-landing-host-chip"]');
        await chip.waitFor({ state: "visible", timeout: 30_000 });

        const outcome = await waitForChipOutcome(window);
        assert.ok(
          outcome.label.includes("This machine"),
          `host chip never picked the running local host — it reads ` +
            `${JSON.stringify(outcome.label)} ` +
            '(the reported symptom is it staying on "Choose host")',
        );
      } finally {
        if (electronApp) await electronApp.close();
        saved = saveRecording(recordDir, "manual-host");
        child.kill("SIGTERM");
        await server.close();
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
      assert.ok(saved && saved.length > 0, "no desktop recording was produced");
    });

    it("recovers when the persisted last-host choice carries the legacy host_ prefix", async () => {
      const server = await spawnServer(fs.mkdtempSync(path.join(tmpDir, "srv-legacy-")));
      const { home, userDataDir } = prepareProfile("legacy", server.serverUrl, cliShim);
      isolateEnv(home);

      const { child, connected, log } = await startHostDaemon(cliShim, server.serverUrl);

      const recordDir = path.join(RECORD_ROOT, "legacy-host-choice");
      let electronApp;
      let saved;
      try {
        assert.ok(connected, `omnigent host did not connect:\n${log.slice(-2000)}`);
        let hostId = null;
        let lastBody = null;
        const hostRowDeadline = Date.now() + 20_000;
        /* oxlint-disable no-await-in-loop */
        while (!hostId && Date.now() < hostRowDeadline) {
          const res = await fetch(`${server.serverUrl}/v1/hosts`);
          lastBody = await res.json();
          hostId = lastBody.hosts?.[0]?.host_id ?? null;
          if (!hostId) await sleep(500);
        }
        /* oxlint-enable no-await-in-loop */
        assert.ok(hostId, `no host row after connect: ${JSON.stringify(lastBody)}`);

        const launched = await launchDesktop({ recordDir, userDataDir });
        electronApp = launched.electronApp;
        const window = await waitForSpaWindow(electronApp);
        const chip = window.locator('[data-testid="new-chat-landing-host-chip"]');
        await chip.waitFor({ state: "visible", timeout: 30_000 });

        // Recreate a choice saved by an older desktop build.
        await window.evaluate(
          ([key, id]) => localStorage.setItem(key, `host_${id}`),
          [LAST_HOST_CHOICE_KEY, hostId],
        );
        await window.reload();
        await chip.waitFor({ state: "visible", timeout: 30_000 });

        const outcome = await waitForChipOutcome(window);
        assert.ok(
          outcome.label.includes("This machine"),
          `host chip never recovered from the legacy-prefixed stored choice — it reads ` +
            `${JSON.stringify(outcome.label)} ` +
            '(the reported symptom is it staying on "Choose host" indefinitely)',
        );
      } finally {
        if (electronApp) await electronApp.close();
        saved = saveRecording(recordDir, "legacy-host-choice");
        child.kill("SIGTERM");
        await server.close();
        fs.rmSync(userDataDir, { recursive: true, force: true });
      }
      assert.ok(saved && saved.length > 0, "no desktop recording was produced");
    });
  },
);
