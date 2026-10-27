// Real Electron console/preload, connect flow, and subprocess runner. Arca,
// SSH, Companion, and both browser sign-ins are a file-gated subprocess fixture;
// this does not validate live SSO, remote TTY allocation, or callback forwarding.
// Run: node --test web/electron/e2e/desktop_arca_sign_in.e2e.js
"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { setTimeout: sleep } = require("node:timers/promises");
const { APP_ROOT, REPO_ROOT, desktopDepsAvailable, saveRecording } = require("./desktopHarness");

const deps = desktopDepsAvailable();
const SERVER_URL = "https://example.cloud.databricks.com/omnigent?o=123";
const HOST_ID = "abcdef0123456789abcdef0123456789";
const SECRET = "fixture-private-oauth-ticket";

/* oxlint-disable no-shadow -- Serialized entrypoints run in separate processes. */
/** Executed by a real child process in place of the external Arca CLI. */
async function fixtureArca({ stateDir, serverUrl, hostId, secret }) {
  const assert = require("node:assert/strict");
  const fs = require("node:fs");
  const path = require("node:path");
  const { spawn } = require("node:child_process");
  const { setTimeout: sleep } = require("node:timers/promises");
  const args = process.argv.slice(2);
  const event = (name, extra = {}) =>
    fs.appendFileSync(
      path.join(stateDir, "events.jsonl"),
      JSON.stringify({ name, ...extra }) + "\n",
    );
  const waitFor = async (name) => {
    while (!fs.existsSync(path.join(stateDir, name))) {
      // oxlint-disable-next-line no-await-in-loop -- The fixture awaits a browser completion gate.
      await sleep(25);
    }
  };
  assert.equal(args[0], "ssh");
  assert.ok(args.includes("ClearAllForwardings=yes"));
  assert.ok(args.includes(`'${serverUrl}'`));
  assert.ok(args.includes("--background"));

  if (args.includes("-tt")) {
    assert.ok(!args.includes("--non-interactive"));
    assert.ok(args.includes('BROWSER="$HOME/.arca-client/tools/www-browser"'));
    const signaled = path.join(stateDir, `worker-${process.pid}-signaled`);
    const ready = path.join(stateDir, `worker-${process.pid}-ready`);
    const worker = spawn(
      process.execPath,
      [
        "-e",
        'const fs = require("node:fs"); process.on("SIGTERM", () => { fs.writeFileSync(process.argv[1], "signaled"); if (process.argv[3] !== "ignore") process.exit(0); }); fs.writeFileSync(process.argv[2], "ready"); setInterval(() => {}, 1000);',
        signaled,
        ready,
        fs.existsSync(path.join(stateDir, "ignore-worker-sigterm")) ? "ignore" : "exit",
      ],
      { stdio: "ignore" },
    );
    fs.writeFileSync(
      path.join(stateDir, "process.json"),
      JSON.stringify({ pid: process.pid, worker: worker.pid }),
    );
    await waitFor(path.basename(ready));
    event("setup-start", { pid: process.pid, worker: worker.pid, signaled });
    process.stdout.write(`Provider OAuth URL: https://sso.example/authorize?state=${secret}\n`);
    event("provider-browser-opened");
    await waitFor("provider-approved");
    process.stderr.write(`Omnigent OAuth URL: https://sso.example/authorize?code=${secret}\n`);
    event("app-browser-opened");
    await waitFor("app-approved");
    event("setup-complete");
    const exited = new Promise((resolve) => {
      worker.once("exit", resolve);
    });
    worker.kill("SIGTERM");
    await exited;
    return;
  }

  assert.ok(args.includes("--non-interactive"));
  assert.ok(fs.existsSync(path.join(stateDir, "provider-approved")));
  assert.ok(fs.existsSync(path.join(stateDir, "app-approved")));
  event("connect-start");
  const target = new URL(serverUrl);
  target.search = "";
  target.pathname = "/api/2.0/omnigent";
  process.stdout.write("Arca host registered.\n__OMNIGENT_ARCA_IDENTITY_START__\n");
  process.stdout.write(
    JSON.stringify({
      daemons: [
        {
          mode: "server",
          process: "online",
          host_status: "online",
          error: null,
          target: target.href,
          server_url: target.href,
          host_id: hostId,
        },
      ],
    }),
  );
  process.stdout.write("\n__OMNIGENT_ARCA_IDENTITY_END__\n");
}

/** Scratch entrypoint: only external CLI resolution is replaced. */
function fixtureMain({ appRoot, stateDir, arcaPath, serverUrl }) {
  const { app, BrowserWindow, ipcMain } = require("electron");
  const path = require("node:path");
  const arca = require(path.join(appRoot, "src/arca.js"));
  const { createArcaConnectFlow } = require(path.join(appRoot, "src/arca_connect_window.js"));
  app.setPath("userData", path.join(stateDir, "profile"));
  app.on("window-all-closed", () => {});
  void app.whenReady().then(() => {
    const processDeps = { resolveArcaPath: () => arcaPath, timeoutMs: 30_000, killGraceMs: 250 };
    const flow = createArcaConnectFlow({
      BrowserWindow,
      ipcMain,
      pagePath: path.join(appRoot, "arca-connect/index.html"),
      preloadPath: path.join(appRoot, "src/arca_connect_preload.js"),
      startSetup: (url) => arca.startArcaSetup(url, processDeps),
      setupCommandLine: (url) => `arca ${arca.buildSetupArgs(url).join(" ")}`,
      startConnect: (url, onOutput) => arca.startArcaConnect(url, { ...processDeps, onOutput }),
      commandLine: (url) => `arca ${arca.buildConnectArgs(url).join(" ")}`,
    });
    global.startFixtureFlow = () => {
      global.fixtureResult = null;
      void flow.run(null, serverUrl).then((result) => {
        global.fixtureResult = result;
      });
    };
    global.startFixtureFlow();
  });
}
/* oxlint-enable no-shadow */

async function waitUntil(predicate, message) {
  const deadline = Date.now() + 10_000;
  // oxlint-disable-next-line no-await-in-loop -- Each probe waits for the prior result.
  while (!(await predicate())) {
    assert.ok(Date.now() < deadline, message);
    // oxlint-disable-next-line no-await-in-loop -- Poll subprocess or Electron completion.
    await sleep(25);
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

it(
  "Arca console prepares both sign-ins after consent, protects OAuth output, and cancels descendants",
  {
    timeout: 90_000,
    skip: !deps.ok ? `missing deps: ${deps.missing.join(", ")}` : process.platform === "win32",
  },
  async () => {
    const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-arca-fixture-"));
    const recordDir = process.env.OMNIGENT_DESKTOP_RECORD_DIR
      ? path.join(path.resolve(process.env.OMNIGENT_DESKTOP_RECORD_DIR), "arca-sign-in")
      : fs.mkdtempSync(path.join(os.tmpdir(), "omni-arca-sign-in-evidence-"));
    assert.ok(!recordDir.startsWith(REPO_ROOT + path.sep), "Keep recordings outside the checkout");
    fs.mkdirSync(recordDir, { recursive: true });
    const cliSource = path.join(stateDir, "arca-fixture.cjs");
    const arcaPath = path.join(stateDir, "arca");
    const mainPath = path.join(stateDir, "main.cjs");
    const quote = (value) => `'${value.replaceAll("'", "'\\''")}'`;
    fs.writeFileSync(
      cliSource,
      `(${fixtureArca.toString()})(${JSON.stringify({ stateDir, serverUrl: SERVER_URL, hostId: HOST_ID, secret: SECRET })}).catch(error => { console.error(error); process.exitCode = 1; });\n`,
    );
    fs.writeFileSync(
      arcaPath,
      `#!/bin/sh\nexec ${quote(process.execPath)} ${quote(cliSource)} "$@"\n`,
      { mode: 0o700 },
    );
    fs.writeFileSync(
      mainPath,
      `(${fixtureMain.toString()})(${JSON.stringify({ appRoot: APP_ROOT, stateDir, arcaPath, serverUrl: SERVER_URL })});\n`,
    );
    const readEvents = () =>
      fs.existsSync(path.join(stateDir, "events.jsonl"))
        ? fs
            .readFileSync(path.join(stateDir, "events.jsonl"), "utf8")
            .trim()
            .split("\n")
            .map(JSON.parse)
        : [];
    const seen = (name) => readEvents().some((event) => event.name === name);
    const { _electron } = require("playwright");
    const args = [mainPath];
    if (process.env.OMNIGENT_PW_NO_SANDBOX) args.push("--no-sandbox", "--disable-dev-shm-usage");
    let electronApp;
    try {
      electronApp = await _electron.launch({ args, recordVideo: { dir: recordDir } });
      let page = await electronApp.firstWindow();
      await page.locator("#confirm").waitFor();
      assert.match(await page.locator("#command").innerText(), /-tt/);
      assert.deepEqual(readEvents(), [], "Opening consent must not launch Arca");
      await page.evaluate(() => {
        window.fixtureOutput = [];
        window.arcaConnect.onOutput((text) => window.fixtureOutput.push(text));
      });
      await page.locator("#confirm").click();
      await waitUntil(() => seen("provider-browser-opened"), "Provider sign-in did not start");
      assert.match(await page.locator("#confirm").innerText(), /Signing in/);
      assert.match(await page.locator("#status").innerText(), /browser opens on your laptop/);
      assert.equal(seen("connect-start"), false);
      await page.screenshot({ path: path.join(recordDir, "waiting-for-sign-in.png") });
      fs.writeFileSync(path.join(stateDir, "provider-approved"), "approved");
      await waitUntil(() => seen("app-browser-opened"), "Omnigent sign-in did not start");
      assert.equal(seen("connect-start"), false, "One completed sign-in must not start connect");
      assert.equal(await page.evaluate(() => window.fixtureOutput.join("")), "");
      assert.ok(!(await page.locator("body").innerText()).includes(SECRET));
      fs.writeFileSync(path.join(stateDir, "app-approved"), "approved");
      await page.waitForFunction(() => document.querySelector("#status").dataset.kind === "ok");
      assert.match(await page.locator("#status").innerText(), /Connected/);
      assert.ok(seen("connect-start"));
      assert.ok(!(await page.evaluate(() => window.fixtureOutput.join(""))).includes(SECRET));
      const result = await electronApp.evaluate(() => global.fixtureResult);
      assert.equal(result.ok, true);
      assert.deepEqual(result.identity, { serverUrl: SERVER_URL, hostId: HOST_ID });
      await page.screenshot({ path: path.join(recordDir, "connected.png") });

      await page.locator("#cancel").click();
      fs.unlinkSync(path.join(stateDir, "provider-approved"));
      fs.unlinkSync(path.join(stateDir, "app-approved"));
      fs.writeFileSync(path.join(stateDir, "ignore-worker-sigterm"), "ignore");
      const nextWindow = electronApp.waitForEvent("window");
      await electronApp.evaluate(() => global.startFixtureFlow());
      page = await nextWindow;
      await page.locator("#confirm").click();
      await waitUntil(
        () => readEvents().filter((event) => event.name === "setup-start").length === 2,
        "Second setup did not start",
      );
      const pending = readEvents()
        .filter((event) => event.name === "setup-start")
        .at(-1);
      await page.locator("#cancel").click();
      await waitUntil(
        () => fs.existsSync(pending.signaled),
        "Cancel did not signal the subprocess descendant",
      );
      await waitUntil(() => !processExists(pending.pid), "Cancel left the Arca subprocess running");
      await waitUntil(
        () => !processExists(pending.worker),
        "Cancel left the SIGTERM-resistant subprocess descendant running",
      );
      const canceled = await electronApp.evaluate(() => global.fixtureResult);
      assert.equal(canceled.canceled, true);
      assert.equal(readEvents().filter((event) => event.name === "connect-start").length, 1);
    } finally {
      if (electronApp) await electronApp.close();
      saveRecording(recordDir, "arca-sign-in");
      fs.writeFileSync(
        path.join(recordDir, "fixture-events.json"),
        JSON.stringify(readEvents(), null, 2),
      );
      if (fs.existsSync(path.join(stateDir, "process.json"))) {
        const { pid } = JSON.parse(fs.readFileSync(path.join(stateDir, "process.json"), "utf8"));
        try {
          process.kill(-pid, "SIGKILL");
        } catch {
          /* The fixture already exited. */
        }
      }
      fs.rmSync(stateDir, { recursive: true, force: true });
      console.log(`Arca fixture evidence: ${recordDir}`);
    }
  },
);
