// Real initial onboarding UI, preload, main process, and subprocess runners.
// Arca, SSH, Companion, SSO, and the destination server are local fixtures;
// this does not validate live sign-in or remote callback forwarding.
// Run after: pnpm --filter web run build:server-selector-v2
// Run: node --test web/electron/e2e/desktop_arca_onboarding.e2e.js
"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { setTimeout: sleep } = require("node:timers/promises");
const {
  APP_ROOT,
  REPO_ROOT,
  desktopDepsAvailable,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();
const HOST_ID = "abcdef0123456789abcdef0123456789";
const SECRET = "fixture-private-oauth-ticket";
const testOptions = {
  timeout: 90_000,
  skip: !deps.ok ? `missing deps: ${deps.missing.join(", ")}` : process.platform === "win32",
};

/* oxlint-disable no-shadow -- Serialized entrypoints run in separate processes. */
async function fixtureArca({ stateDir, serverUrl, hostId, secret }) {
  const assert = require("node:assert/strict");
  const fs = require("node:fs");
  const path = require("node:path");
  const { spawn } = require("node:child_process");
  const { setTimeout: sleep } = require("node:timers/promises");
  const file = (name) => path.join(stateDir, name);
  const event = (name, extra = {}) =>
    fs.appendFileSync(file("events.jsonl"), JSON.stringify({ name, ...extra }) + "\n");
  const waitFor = async (name) => {
    while (!fs.existsSync(file(name))) {
      // oxlint-disable-next-line no-await-in-loop -- Await simulated browser approval.
      await sleep(25);
    }
  };
  const args = process.argv.slice(2);
  event("command", { args, pid: process.pid });
  if (args[0] === "status") {
    process.stdout.write(JSON.stringify({ status: "running", instance: { shutdown_time: null } }));
    return;
  }
  assert.equal(args[0], "ssh");
  assert.ok(args.includes("ClearAllForwardings=yes"));
  assert.ok(args.includes(`'${serverUrl}'`));
  assert.ok(args.includes("--background"));
  const ready = () => {
    process.stdout.write("Arca host registered.\n__OMNIGENT_ARCA_IDENTITY_START__\n");
    process.stdout.write(
      JSON.stringify({
        daemons: [
          {
            mode: "server",
            process: "online",
            host_status: "online",
            error: null,
            target: serverUrl,
            server_url: serverUrl,
            host_id: hostId,
          },
        ],
      }),
    );
    process.stdout.write("\n__OMNIGENT_ARCA_IDENTITY_END__\n");
  };
  if (args.includes("-tt")) {
    assert.ok(!args.includes("--non-interactive"));
    assert.ok(args.includes('BROWSER="$HOME/.arca-client/tools/www-browser"'));
    const signaled = file(`worker-${process.pid}-signaled`);
    const workerReady = file(`worker-${process.pid}-ready`);
    const worker = spawn(
      process.execPath,
      [
        "-e",
        'const fs = require("node:fs"); process.on("SIGTERM", () => { fs.writeFileSync(process.argv[1], "signaled"); if (process.argv[3] !== "ignore") process.exit(0); }); fs.writeFileSync(process.argv[2], "ready"); setInterval(() => {}, 1000);',
        signaled,
        workerReady,
        fs.existsSync(file("ignore-worker-sigterm")) ? "ignore" : "exit",
      ],
      { stdio: "ignore" },
    );
    await waitFor(path.basename(workerReady));
    event("setup-start", { pid: process.pid, worker: worker.pid, signaled });
    process.stdout.write(`Provider OAuth URL: https://sso.example/authorize?state=${secret}\n`);
    event("provider-sign-in");
    await waitFor("provider-approved");
    event("provider-complete");
    process.stderr.write(`Omnigent OAuth URL: https://sso.example/authorize?code=${secret}\n`);
    event("app-sign-in");
    await waitFor("app-approved");
    event("app-complete");
    const exited = new Promise((resolve) => {
      worker.once("exit", resolve);
    });
    worker.kill("SIGTERM");
    await exited;
    ready();
    event("setup-complete");
    return;
  }
  assert.ok(args.includes("--non-interactive"));
  assert.ok(fs.existsSync(file("provider-approved")), "Host connect preceded provider sign-in");
  assert.ok(fs.existsSync(file("app-approved")), "Host connect preceded app sign-in");
  event("connect-start");
  ready();
}

/** Replace external policy and executable discovery before production main loads. */
function fixturePolicy({ appRoot, arcaPath, serverUrl }) {
  const path = require("node:path");
  const src = path.join(appRoot, "src");
  const managed = require(path.join(src, "managed_preferences"));
  managed.getDatabricksInternalFeaturesEnabled = () => true;
  managed.getManagedServerUrls = () => [serverUrl];
  managed.getManagedServerNames = () => ({ [serverUrl]: "Fixture team" });
  require(path.join(src, "url")).isDatabricksManagedServerUrl = (url) =>
    typeof url === "string" && new URL(url).origin === new URL(serverUrl).origin;
  const cli = require(path.join(src, "omnigent_cli"));
  cli.getCliStatus = async () => ({
    installed: true,
    path: null,
    version: "fixture",
    source: null,
  });
  cli.localServerHealthy = async () => null;
  cli.resolveCliPath = () => null;
  require(path.join(src, "isaac")).resolveIsaacPath = () => null;
  const arca = require(path.join(src, "arca"));
  arca.resolveArcaPath = () => arcaPath;
  arca.resolveArcaPathAsync = async () => arcaPath;
  const startConnect = arca.startArcaConnect;
  arca.startArcaConnect = (url, deps = {}) =>
    startConnect(url, {
      ...deps,
      resolveArcaPath: () => arcaPath,
      timeoutMs: 30_000,
      killGraceMs: 250,
    });
  const readStatus = arca.readArcaStatus;
  arca.readArcaStatus = (deps = {}) =>
    readStatus({
      ...deps,
      resolveArcaPath: () => arcaPath,
      timeoutMs: 2000,
    });
  const onboarding = require(path.join(src, "arca_onboarding"));
  const startSetup = onboarding.startArcaSetup;
  onboarding.startArcaSetup = (url, deps = {}) =>
    startSetup(url, {
      ...deps,
      resolveArcaPath: () => arcaPath,
      timeoutMs: 30_000,
      killGraceMs: 250,
    });
}
/* oxlint-enable no-shadow */

async function waitUntil(predicate, message) {
  const deadline = Date.now() + 15_000;
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

async function withFixture(name, run) {
  assert.ok(
    fs.existsSync(path.join(APP_ROOT, "server-selector-v2/server-selector-v2.html")),
    "Build onboarding first: pnpm --filter web run build:server-selector-v2",
  );
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-arca-onboarding-"));
  const recordDir = process.env.OMNIGENT_DESKTOP_RECORD_DIR
    ? path.join(path.resolve(process.env.OMNIGENT_DESKTOP_RECORD_DIR), `arca-onboarding-${name}`)
    : fs.mkdtempSync(path.join(os.tmpdir(), `omni-arca-onboarding-${name}-evidence-`));
  assert.ok(!recordDir.startsWith(REPO_ROOT + path.sep), "Keep recordings outside the checkout");
  fs.mkdirSync(recordDir, { recursive: true });
  const eventsFile = path.join(stateDir, "events.jsonl");
  const readEvents = () =>
    fs.existsSync(eventsFile)
      ? fs.readFileSync(eventsFile, "utf8").trim().split("\n").map(JSON.parse)
      : [];
  const server = http.createServer((req, res) => {
    if (req.url === "/.well-known/omnigent.json") {
      res.setHeader("Content-Type", "application/json");
      res.end(
        JSON.stringify({
          manifest_version: 1,
          server_version: "fixture",
          server_name: "Fixture team",
        }),
      );
    } else if (req.url === "/health") {
      res.end("ok");
    } else if (req.url === "/") {
      res.setHeader("Content-Type", "text/html");
      res.end(
        "<!doctype html><title>Fixture team</title><style>html{background:#f8fafc;color:#172033}body{margin:0;padding:48px;font:18px/1.6 system-ui,sans-serif}</style><h1>Onboarding completed</h1><p>Local server fixture; no live Arca or credentials.</p>",
      );
    } else {
      res.writeHead(404).end();
    }
  });
  let desktop;
  const runnerLogs = [];
  try {
    await new Promise((resolve) => {
      server.listen(0, "127.0.0.1", resolve);
    });
    const serverUrl = `http://127.0.0.1:${server.address().port}/`;
    const cliSource = path.join(stateDir, "arca-fixture.cjs");
    const arcaPath = path.join(stateDir, "arca");
    const policyPath = path.join(stateDir, "policy.cjs");
    const quote = (value) => `'${value.replaceAll("'", "'\\''")}'`;
    fs.writeFileSync(
      cliSource,
      `(${fixtureArca.toString()})(${JSON.stringify({ stateDir, serverUrl, hostId: HOST_ID, secret: SECRET })}).catch(error => { console.error(error); process.exitCode = 1; });\n`,
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
      userDataDir: path.join(stateDir, "desktop-profile"),
      preload: [policyPath],
      env: {
        OMNIGENT_SERVER_SELECTOR_V2: "1",
        OMNIGENT_SERVER_SELECTOR_V2_DEV_URL: "",
        OMNIGENT_ONBOARDING_MOCK: "",
        OMNIGENT_ARCA_AUTO_CONNECT: "",
        OMNIGENT_CONFIG_HOME: path.join(stateDir, "config"),
        OMNIGENT_DATA_DIR: path.join(stateDir, "data"),
      },
    });
    const page = desktop.window;
    await page.exposeFunction("recordFixtureRunnerLog", (line) => {
      runnerLogs.push(line);
    });
    await page.evaluate(() => {
      window.omnigentSetup.onRunnerConnectLog((line) => {
        void window.recordFixtureRunnerLog(line);
      });
    });
    const profilePath = await desktop.electronApp.evaluate(({ app }) => app.getPath("userData"));
    const settingsPath = path.join(profilePath, "settings.json");
    const settings = () => JSON.parse(fs.readFileSync(settingsPath, "utf8"));
    await run({
      ...desktop,
      page,
      stateDir,
      recordDir,
      serverUrl,
      readEvents,
      runnerLogs,
      settings,
    });
  } finally {
    if (desktop) {
      await desktop.electronApp.close();
      await desktop.stopDisplayCapture();
      saveRecording(recordDir, `arca-onboarding-${name}`);
    }
    fs.writeFileSync(
      path.join(recordDir, "fixture-events.json"),
      JSON.stringify(readEvents(), null, 2),
    );
    fs.writeFileSync(
      path.join(recordDir, "renderer-log.json"),
      JSON.stringify(runnerLogs, null, 2),
    );
    for (const event of readEvents().filter((entry) => entry.name === "command")) {
      try {
        process.kill(-event.pid, "SIGKILL");
      } catch {
        /* The fixture already exited. */
      }
    }
    server.closeAllConnections();
    await new Promise((resolve) => {
      server.close(resolve);
    });
    fs.rmSync(stateDir, { recursive: true, force: true });
    console.log(`Arca onboarding evidence: ${recordDir}`);
  }
}

async function chooseArca(page) {
  await page.getByRole("button", { name: /Join your team/ }).click();
  await page.getByText("Where do you work today?", { exact: true }).waitFor();
  assert.equal(await page.getByRole("radio", { name: "Arca", exact: true }).isChecked(), true);
}

it(
  "initial Arca onboarding completes both sign-ins before connecting and persists the remote runner",
  testOptions,
  async () => {
    await withFixture(
      "complete",
      async ({ page, stateDir, recordDir, serverUrl, readEvents, runnerLogs, settings }) => {
        await chooseArca(page);
        assert.equal(
          readEvents().some((event) => event.args?.[0] === "ssh"),
          false,
        );
        await page.getByRole("button", { name: "Open Omnigent", exact: true }).click();
        await waitUntil(
          () => readEvents().some((event) => event.name === "provider-sign-in"),
          "Provider sign-in did not start",
        );
        await page
          .getByText(
            "If sign-in is needed, a browser opens on your laptop. Finish signing in to continue.",
            { exact: true },
          )
          .waitFor();
        assert.equal(
          readEvents().some((event) => event.name === "connect-start"),
          false,
        );
        assert.ok(!runnerLogs.join("\n").includes(SECRET));
        assert.ok(!(await page.locator("body").innerText()).includes(SECRET));
        await page.screenshot({ path: path.join(recordDir, "waiting-for-sign-in.png") });
        fs.writeFileSync(path.join(stateDir, "provider-approved"), "approved");
        await waitUntil(
          () => readEvents().some((event) => event.name === "app-sign-in"),
          "App sign-in did not start",
        );
        assert.equal(
          readEvents().some((event) => event.name === "connect-start"),
          false,
        );
        assert.ok(!runnerLogs.join("\n").includes(SECRET));
        fs.writeFileSync(path.join(stateDir, "app-approved"), "approved");
        await page.getByRole("heading", { name: "Onboarding completed", exact: true }).waitFor();
        assert.equal(page.url(), serverUrl);
        assert.ok(!runnerLogs.join("\n").includes(SECRET));
        assert.deepEqual(
          readEvents()
            .filter((event) => event.name !== "command")
            .map((event) => event.name),
          [
            "setup-start",
            "provider-sign-in",
            "provider-complete",
            "app-sign-in",
            "app-complete",
            "setup-complete",
            "connect-start",
          ],
        );
        assert.equal(readEvents().filter((event) => event.args?.[0] === "ssh").length, 2);
        const saved = settings();
        assert.equal(saved.onboarding_runner.origin, new URL(serverUrl).origin);
        assert.equal(saved.onboarding_runner.runner, "remote");
        assert.equal(saved.arca_auto_connect, true);
        await page.screenshot({ path: path.join(recordDir, "onboarding-completed.png") });
      },
    );
  },
);

it(
  "closing initial onboarding cancels setup and kills a SIGTERM-resistant descendant",
  testOptions,
  async () => {
    await withFixture("cancel", async ({ electronApp, page, stateDir, readEvents, settings }) => {
      fs.writeFileSync(path.join(stateDir, "ignore-worker-sigterm"), "ignore");
      // Keep Electron alive after the setup window closes so escalation can finish on Linux.
      await electronApp.evaluate(({ BrowserWindow }) => {
        global.fixtureKeepAlive = new BrowserWindow({ show: false });
      });
      await chooseArca(page);
      await page.getByRole("button", { name: "Open Omnigent", exact: true }).click();
      await waitUntil(
        () => readEvents().some((event) => event.name === "setup-start"),
        "Setup did not start",
      );
      const pending = readEvents().find((event) => event.name === "setup-start");
      await page.close();
      await waitUntil(
        () => fs.existsSync(pending.signaled),
        "Closing onboarding did not signal the descendant",
      );
      await waitUntil(() => !processExists(pending.pid), "Closing onboarding left Arca running");
      await waitUntil(
        () => !processExists(pending.worker),
        "Closing onboarding left the SIGTERM-resistant descendant running",
      );
      await waitUntil(
        () => settings().arca_auto_connect !== true,
        "Canceled onboarding kept auto-connect enabled",
      );
      assert.equal(settings().onboarding_runner, undefined);
      assert.equal(
        readEvents().some((event) => event.name === "connect-start"),
        false,
      );
    });
  },
);
