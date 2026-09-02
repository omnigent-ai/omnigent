// Regression test: the desktop shell's "Run on this machine" must actually
// connect a local host — not leave the picker stuck on "Choose host".
//
// The reported journey (Omnigent desktop app):
//   1. Open the desktop app connected to a server with no host selected.
//   2. Open the host picker and click "Run on this machine".
//   3. BUG: nothing runs locally — the chip just shows "Choose host".
//   4. Workaround attempt: run `omnigent host <server-url>` in a terminal;
//      the picker STILL shows "Choose host" instead of using the local host.
//
// Cases 1 and 2 cover the fresh-profile journeys: the click and the
// manual-daemon workaround must both select "This machine" (guarded by the
// localHostId() legacy-prefix normalization in the Electron shell). Case 3
// covers the returning-user journey: a profile whose persisted last-host
// choice carries the legacy "host_"-prefixed spelling — what pre-migration
// desktop builds persisted when the user picked this machine — never matches
// the bare-hex ids /v1/hosts reports, so without normalization on read the
// picker waits for that host forever, stuck on "Choose host" even while the
// local host is online (guarded by hostPreferences.ts normalizing the stored
// choice).
//
// This lane drives the REAL packaged Electron shell (the `controlHost` IPC,
// the CLI resolution, the spawned `omnigent host` daemon and the identity
// file) against the same mock-LLM + `omnigent server` pair the Python suite
// uses — a browser-tab stand-in cannot exercise any of that. Run from
// web/electron AFTER building the SPA:
//
//   node --test e2e/desktop_run_on_this_machine_selects_local_host.e2e.js
//
// Headless CI: wrap in `xvfb-run -a` and set OMNIGENT_PW_NO_SANDBOX=1; point
// OMNIGENT_PYTHON at a Python that can import the repo's `omnigent`.

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

/** The Python interpreter the harness spawns the server with. */
const PYTHON = process.env.OMNIGENT_PYTHON || "python3";

/** The line `omnigent host` prints once the websocket tunnel is up. */
const CONNECTED_MARKER = "✓ Connected";

/** How long the picker gets to reach its settled label after the trigger. */
const SELECT_TIMEOUT_MS = 45_000;

/** The localStorage key hostPreferences.ts persists the last host pick under. */
const LAST_HOST_CHOICE_KEY = "omnigent:last-host-choice";

/** Sleep `ms` milliseconds. Block body so the Promise executor returns nothing. */
function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

/**
 * Write an executable `omnigent` CLI shim that runs the repo's CLI under the
 * harness Python. The desktop shell resolves the CLI from its settings
 * (`omnigent_path`) and spawns it for host control, so the shim is what makes
 * `getHostIdentity().cliInstalled` true and "Run on this machine" appear.
 *
 * @param {string} dir Directory to write the shim into.
 * @returns {string} Absolute path of the shim.
 */
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

/**
 * Prepare an isolated $HOME plus a userData dir whose settings.json is
 * pre-connected to `serverUrl`, points the CLI at the shim, and carries the
 * "Always Allow" hosting grant for the server's origin — the steady state of
 * a user who approved the native enrollment dialog once. (The dialog is a
 * main-process native surface Playwright cannot click, and an approved origin
 * skips it by design.)
 *
 * @param {string} label Distinguishes the per-test temp dirs.
 * @param {string} serverUrl The harness server, e.g. "http://127.0.0.1:5xxxx".
 * @param {string} cliShim Path of the CLI shim from {@link writeCliShim}.
 * @returns {{ home: string, userDataDir: string }}
 */
function prepareProfile(label, serverUrl, cliShim) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), `omni-local-host-home-${label}-`));
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), `omni-local-host-data-${label}-`));
  fs.writeFileSync(
    path.join(userDataDir, "settings.json"),
    JSON.stringify(
      {
        server_url: serverUrl,
        omnigent_path: cliShim,
        allowed_hosting_origins: [new URL(serverUrl).origin],
      },
      null,
      2,
    ),
  );
  return { home, userDataDir };
}

/**
 * Point every process this test spawns (Electron, its `omnigent host` child,
 * and any CLI subprocess) at the isolated `home`: the host identity file
 * (`~/.omnigent/config.yaml`), the daemon registry, and the auth-token store
 * all live under $HOME, and the desktop shell must read the SAME identity the
 * spawned daemon writes for "this machine" matching to be exercised for real.
 *
 * @param {string} home The isolated home directory.
 */
function isolateEnv(home) {
  process.env.HOME = home;
  // A config-home override would split the Electron shell's identity read
  // from the daemon's hardcoded ~/.omnigent write — drop it.
  delete process.env.OMNIGENT_CONFIG_HOME;
  delete process.env.OMNIGENT_DATA_DIR;
  // Ambient managed-host / runner identity would override the daemon's
  // file-based identity; never let it leak into the spawned host. Rebuild by
  // filtering (rather than deleting keys) to keep the object shape static.
  const cleanEnv = Object.fromEntries(
    Object.entries(process.env).filter(
      ([key]) => !key.startsWith("OMNIGENT_HOST_") && !key.startsWith("OMNIGENT_RUNNER_"),
    ),
  );
  process.env = cleanEnv;
  // Loopback traffic must not be routed through an ambient corporate proxy.
  const noProxy = ["127.0.0.1", "localhost"];
  for (const key of ["NO_PROXY", "no_proxy"]) {
    const existing = process.env[key];
    process.env[key] = existing ? `${existing},${noProxy.join(",")}` : noProxy.join(",");
  }
}

/**
 * The SPA window of the launched shell. `launchDesktop` returns Playwright's
 * firstWindow(), but the desktop shell also opens the update-overlay window
 * (file://…/update-overlay.html) and either may enumerate first — so resolve
 * the window whose URL is the served SPA (http…) before locating anything.
 *
 * @param {import("playwright").ElectronApplication} electronApp
 * @param {number} [timeoutMs]
 * @returns {Promise<import("playwright").Page>}
 */
async function waitForSpaWindow(electronApp, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  // Sequential polling is the point — window enumeration is a live snapshot.
  /* oxlint-disable no-await-in-loop */
  while (Date.now() < deadline) {
    const spa = electronApp.windows().find((w) => w.url().startsWith("http"));
    if (spa) return spa;
    await sleep(500);
  }
  /* oxlint-enable no-await-in-loop */
  throw new Error("no SPA window (http…) appeared within the deadline");
}

/**
 * Start `omnigent host --server <url> --non-interactive` — the exact command
 * the reporter ran as the workaround — and wait for its connected marker.
 *
 * @param {string} cliShim Path of the CLI shim from {@link writeCliShim}.
 * @param {string} serverUrl The harness server URL.
 * @returns {Promise<{ child: import("node:child_process").ChildProcess,
 *   connected: boolean, log: string }>}
 */
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

/**
 * Poll the host chip until it names this machine, a connect error surfaces,
 * or the deadline passes. Returns what settled so the assertion can name the
 * exact symptom ("Choose host" being the reported one).
 *
 * @param {import("playwright").Page} window
 * @returns {Promise<{ label: string, error: string | null }>}
 */
async function waitForChipOutcome(window) {
  const chip = window.locator('[data-testid="new-chat-landing-host-chip"]');
  const errorBox = window.locator('[data-testid="new-chat-landing-connect-error"]');
  const deadline = Date.now() + SELECT_TIMEOUT_MS;
  let label = "";
  // Sequential polling is the point — each probe reflects the current UI.
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
      // A per-test server: the click enrolls a host, and sharing a server
      // across cases would bleed that host into the other journeys.
      const server = await spawnServer(fs.mkdtempSync(path.join(tmpDir, "srv-connect-")));
      const { home, userDataDir } = prepareProfile("connect", server.serverUrl, cliShim);
      isolateEnv(home);
      const recordDir = path.join(RECORD_ROOT, "run-on-this-machine");
      const { electronApp } = await launchDesktop({ recordDir, userDataDir });
      let saved;
      try {
        const window = await waitForSpaWindow(electronApp);

        // 1. The landing composer renders; no host is connected yet.
        const chip = window.locator('[data-testid="new-chat-landing-host-chip"]');
        await chip.waitFor({ state: "visible", timeout: 30_000 });
        await window
          .locator('[data-testid="new-chat-landing-host-chip"]:has-text("No hosts")')
          .waitFor({ state: "visible", timeout: 30_000 });

        // 2. Open the host picker: the desktop shell (CLI installed, machine
        //    not in the list) offers the one-click "Run on this machine".
        await chip.click();
        const runItem = window.locator('[data-testid="new-chat-landing-run-on-this-machine"]');
        await runItem.waitFor({ state: "visible", timeout: 15_000 });

        // 3. Click it. The menu closes, the shell spawns `omnigent host` for
        //    this server (enrollment pre-approved), and on success selects
        //    this machine in the picker.
        await runItem.click();

        // 4. THE BUG: nothing ran locally and the chip settled on
        //    "Choose host". Fixed behavior: the chip names this machine.
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

        // 5. The click genuinely enrolled a local host server-side — the label
        //    isn't cosmetic.
        const res = await fetch(`${server.serverUrl}/v1/hosts`);
        const body = await res.json();
        const online = (body.hosts ?? []).filter((h) => h.status === "online");
        assert.equal(
          online.length,
          1,
          `expected exactly one online host after the connect, got: ${JSON.stringify(body)}`,
        );
      } finally {
        // Close first — the video is flushed on close — so a FAILING run (the
        // primary repro use) still yields the before-fix footage.
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

      // The reporter's workaround: run `omnigent host <server-url>` in a
      // terminal first, and only then look at the desktop picker.
      const { child, connected, log } = await startHostDaemon(cliShim, server.serverUrl);

      const recordDir = path.join(RECORD_ROOT, "manual-host");
      let electronApp;
      let saved;
      try {
        assert.ok(connected, `omnigent host did not connect:\n${log.slice(-2000)}`);

        // Open the desktop app AFTER the daemon is up: the picker should
        // detect the online local host and auto-select it as this machine.
        const launched = await launchDesktop({ recordDir, userDataDir });
        electronApp = launched.electronApp;
        const window = await waitForSpaWindow(electronApp);

        const chip = window.locator('[data-testid="new-chat-landing-host-chip"]');
        await chip.waitFor({ state: "visible", timeout: 30_000 });

        // THE BUG (workaround facet): even with the daemon
        // running, the chip stayed on "Choose host". Fixed behavior: the
        // online local host is auto-picked and labeled as this machine.
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

      // The local host is genuinely online (the reporter ran the workaround).
      const { child, connected, log } = await startHostDaemon(cliShim, server.serverUrl);

      const recordDir = path.join(RECORD_ROOT, "legacy-host-choice");
      let electronApp;
      let saved;
      try {
        assert.ok(connected, `omnigent host did not connect:\n${log.slice(-2000)}`);
        // The daemon prints its connected marker when the tunnel is up; the
        // server commits the host row moments later — poll rather than race it.
        let hostId = null;
        let lastBody = null;
        const hostRowDeadline = Date.now() + 20_000;
        // Sequential polling is the point — each probe reflects current state.
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

        // Seed the persisted pick with the legacy "host_"-prefixed spelling —
        // what desktop builds predating the localHostId() normalization wrote
        // when the user picked this machine — then reload: the returning-user
        // journey on an upgraded app.
        await window.evaluate(
          ([key, id]) => localStorage.setItem(key, `host_${id}`),
          [LAST_HOST_CHOICE_KEY, hostId],
        );
        await window.reload();
        await chip.waitFor({ state: "visible", timeout: 30_000 });

        // THE BUG (still live at authoring time): the stored "host_<hex>" never
        // matches the bare-hex /v1/hosts ids, and hostPreferences waits for
        // the stored host to "reappear" — which it never can — so the picker
        // stays on "Choose host" forever even though this machine is online.
        // Fixed behavior: the picker recovers and selects this machine.
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
