"use strict";

// Real SPA → preload → main → CLI process → session API. The external dbcert
// sign-in and machine policy are fixtures; cloud allocation stays gated locally.
// This does not exercise macOS SSO or Companion's credential transfer.
const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const {
  APP_ROOT,
  REPO_ROOT,
  WEB_UI_DIST,
  desktopDepsAvailable,
  findFreePort,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();

it(
  "desktop supports refreshing or skipping an Arclet credential check",
  {
    skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}`,
    timeout: 90_000,
  },
  async () => {
    const { expect } = require("playwright/test");
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omni-dbcert-preflight-"));
    const recordDir = process.env.OMNIGENT_DESKTOP_RECORD_DIR || path.join(root, "recording");
    const port = await findFreePort();
    const serverUrl = `http://127.0.0.1:${port}`;
    const bin = path.join(root, "bin");
    fs.mkdirSync(bin);
    fs.mkdirSync(path.join(root, "config"));
    const agent = path.join(root, "credential_probe.yaml");
    fs.writeFileSync(
      agent,
      "name: credential_probe\nprompt: Answer the user.\nexecutor:\n  harness: openai-agents\n  model: gpt-4o-mini\n",
    );
    fs.writeFileSync(
      path.join(bin, "dbcert"),
      `#!/usr/bin/env node
const fs = require("node:fs");
const path = require("node:path");
const root = process.env.OMNIGENT_DBCERT_TEST_DIR;
const file = name => path.join(root, name);
const args = process.argv.slice(2);
fs.appendFileSync(file("commands.jsonl"), JSON.stringify(args) + "\\n");
if (args[0] === "status") {
  fs.writeFileSync(file("status-started"), "");
  const finishStatus = () => {
    if (!fs.existsSync(file("hold-status"))) process.exit(fs.existsSync(file("valid")) ? 0 : 1);
  };
  finishStatus();
  setInterval(finishStatus, 20);
} else {
  if (!args.includes("--update-kubeconfig=false")) process.exit(2);
  if (process.env.DBCERT_SKIP_ARCA_SYNC !== "1") process.exit(3);
  fs.writeFileSync(file("sign-in-pid"), String(process.pid));
  process.on("exit", () => fs.rmSync(file("sign-in-pid"), { force: true }));
  fs.writeFileSync(file("sign-in-started"), "");
  const poll = setInterval(() => {
    if (!fs.existsSync(file("complete-sign-in"))) return;
    clearInterval(poll);
    fs.writeFileSync(file("valid"), "");
    process.exit(0);
  }, 20);
}
`,
      { mode: 0o755 },
    );
    const policy = path.join(root, "machine-policy.cjs");
    fs.writeFileSync(
      policy,
      `
const path = require("node:path");
const src = ${JSON.stringify(path.join(APP_ROOT, "src"))};
require(path.join(src, "managed_preferences")).getDatabricksInternalFeaturesEnabled = () => true;
const urls = require(path.join(src, "url"));
urls.isDatabricksManagedServerUrl = url => url === ${JSON.stringify(serverUrl)} || url === ${JSON.stringify(`${serverUrl}/`)};
const arca = require(path.join(src, "arca"));
arca.resolveArcaPath = () => null;
arca.resolveArcaPathAsync = async () => null;
`,
    );
    const serverCode = `
import sys
from pathlib import Path
import uvicorn
from tests.e2e_ui.start_session._managed_replica_server import LocalManagedHost, replica_app
LocalManagedHost.provider = "arclet"
app, launcher = replica_app(Path(sys.argv[1]), "local", keyed_url=sys.argv[2], unkeyed_url=sys.argv[2])
launcher.fail_provision = True
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[3]))
`;
    const cleanEnv = Object.fromEntries(
      Object.entries(process.env).filter(
        ([key]) => !key.startsWith("OMNIGENT_RUNNER_") && !key.startsWith("OMNIGENT_HOST_"),
      ),
    );
    const serverLog = path.join(root, "server.log");
    const log = fs.openSync(serverLog, "w");
    const server = spawn(
      process.env.OMNIGENT_PYTHON || "python3",
      ["-c", serverCode, root, serverUrl, String(port)],
      {
        cwd: REPO_ROOT,
        env: {
          ...cleanEnv,
          PYTHONPATH: REPO_ROOT,
          OMNIGENT_CONFIG_HOME: path.join(root, "config"),
          OMNIGENT_CONFIG: "",
          OMNIGENT_BUILTIN_AGENT_DIRS: agent,
          OMNIGENT_DATA_DIR: path.join(root, "data"),
          OMNIGENT_SKIP_ONBOARD: "1",
          OMNIGENT_LOCAL_SINGLE_USER: "1",
          OMNIGENT_WEB_UI_DIST: WEB_UI_DIST,
        },
        stdio: ["ignore", log, log],
      },
    );
    let serverError;
    server.on("error", (error) => {
      serverError = error;
    });
    const serverClosed = new Promise((resolve) => {
      server.once("close", resolve);
    });
    let desktop;
    const creates = [];
    try {
      await expect
        .poll(
          async () => {
            assert.ifError(serverError);
            assert.equal(server.exitCode, null, fs.readFileSync(serverLog, "utf8"));
            return fetch(`${serverUrl}/health`)
              .then((response) => response.status)
              .catch(() => 0);
          },
          { timeout: 30_000 },
        )
        .toBe(200);
      desktop = await launchDesktop({
        serverUrl,
        recordDir,
        userDataDir: path.join(root, "desktop"),
        preload: [policy],
        env: { PATH: `${bin}${path.delimiter}${process.env.PATH}`, OMNIGENT_DBCERT_TEST_DIR: root },
      });
      const page = desktop.window;
      page.on("request", (request) => {
        if (request.method() === "POST" && new URL(request.url()).pathname === "/v1/sessions")
          creates.push(request);
      });
      await expect(page.getByTestId("new-chat-landing-input")).toBeVisible({ timeout: 20_000 });
      const agents = await fetch(`${serverUrl}/v1/agents`).then((response) => response.json());
      const agentId = agents.data.find((item) => item.name === "credential_probe").id;
      await page.getByTestId("new-chat-landing-agent-select").click();
      await page.getByTestId(`new-chat-landing-agent-${agentId}`).click();
      await expect(page.getByTestId("new-chat-landing-host-chip")).toHaveAttribute(
        "aria-label",
        "Host: Arclet Sandbox",
      );
      const prompt = "Keep this task until laptop sign-in finishes.";
      await page.getByTestId("new-chat-landing-input").fill(prompt);
      await page.getByTestId("new-chat-landing-submit").click();
      await expect(page.getByRole("button", { name: "Refresh dbcert" })).toBeVisible();
      assert.equal(creates.length, 0, "Arclet creation must wait for usable laptop credentials");
      assert.equal(fs.existsSync(path.join(root, "sign-in-started")), false);
      await expect(page.getByTestId("new-chat-landing-input")).toHaveValue(prompt);
      const before = await fetch(`${serverUrl}/__test/state`).then((response) => response.json());
      assert.equal(before.provisioning, false);
      await page.screenshot({ path: path.join(recordDir, "credentials-required.png") });
      await page.getByRole("button", { name: "Refresh dbcert" }).click();
      await expect.poll(() => fs.existsSync(path.join(root, "sign-in-started"))).toBe(true);
      await expect(page.getByText("Opening browser sign-in.", { exact: false })).toBeVisible();
      assert.equal(creates.length, 0);
      await page.screenshot({ path: path.join(recordDir, "waiting-for-sign-in.png") });
      fs.writeFileSync(path.join(root, "complete-sign-in"), "");
      await expect(page.getByRole("button", { name: "Refreshing…" })).toBeHidden();
      await expect(
        page.getByText("Your laptop's dbcert is missing or expired.", { exact: false }),
      ).toBeHidden();
      assert.equal(creates.length, 0, "Finishing sign-in must not automatically create an Arclet");
      await expect(page.getByTestId("new-chat-landing-input")).toHaveValue(prompt);
      const createdResponse = page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          new URL(response.url()).pathname === "/v1/sessions",
      );
      await page.getByTestId("new-chat-landing-submit").click();
      const response = await createdResponse;
      assert.equal(response.status(), 201, await response.text());
      assert.equal(creates.length, 1);
      const body = creates[0].postDataJSON();
      assert.equal(body.sandbox_provider, "arclet");
      assert.equal(body.initial_input.data.content[0].text, prompt);
      const commands = fs
        .readFileSync(path.join(root, "commands.jsonl"), "utf8")
        .trim()
        .split("\n")
        .map(JSON.parse);
      assert.deepEqual(commands, [
        ["status"],
        ["--force", "--caller-name", "omnigent", "--update-kubeconfig=false"],
        ["status"],
        ["status"],
      ]);
      await expect
        .poll(
          async () => (await fetch(`${serverUrl}/__test/state`).then((r) => r.json())).provisioning,
        )
        .toBe(true);
      // An already-valid laptop does not open another sign-in.
      assert.deepEqual(await page.evaluate(() => window.omnigentDesktop.checkDbcert()), {
        ok: true,
        valid: true,
      });
      const laterCommands = fs
        .readFileSync(path.join(root, "commands.jsonl"), "utf8")
        .trim()
        .split("\n")
        .map(JSON.parse);
      assert.deepEqual(laterCommands.at(-1), ["status"]);
      assert.equal(laterCommands.filter((args) => args[0] === "--force").length, 1);

      fs.rmSync(path.join(root, "valid"));
      fs.rmSync(path.join(root, "status-started"));
      fs.writeFileSync(path.join(root, "hold-status"), "");
      await page.goto(serverUrl);
      await page.getByTestId("new-chat-landing-agent-select").click();
      await page.getByTestId(`new-chat-landing-agent-${agentId}`).click();
      const skippedPrompt = "Continue without waiting for laptop credentials.";
      await page.getByTestId("new-chat-landing-input").fill(skippedPrompt);
      await page.getByTestId("new-chat-landing-submit").click();
      await expect(page.getByText("Checking laptop credentials…", { exact: true })).toBeVisible();
      await expect(page.getByTestId("new-chat-landing-input")).toBeDisabled();
      await expect(page.getByTestId("new-chat-landing-repo-chip")).toBeDisabled();
      await expect(page.getByTestId("new-chat-landing-agent-select")).toBeDisabled();
      await expect.poll(() => fs.existsSync(path.join(root, "status-started"))).toBe(true);
      assert.equal(creates.length, 1);
      await page.screenshot({ path: path.join(recordDir, "skippable-credential-check.png") });
      const pendingStatus = page.evaluate(() => window.omnigentDesktop.checkDbcert());
      const skippedCreateResponse = page.waitForResponse(
        (candidateResponse) =>
          candidateResponse.request().method() === "POST" &&
          new URL(candidateResponse.url()).pathname === "/v1/sessions",
      );
      await page.getByRole("button", { name: "Skip", exact: true }).click();
      const skippedResponse = await skippedCreateResponse;
      assert.equal(skippedResponse.status(), 201, await skippedResponse.text());
      assert.equal(creates.length, 2);
      assert.equal(creates[1].postDataJSON().initial_input.data.content[0].text, skippedPrompt);
      assert.equal(fs.existsSync(path.join(root, "hold-status")), true);
      await expect(page.getByText("Checking laptop credentials…", { exact: true })).toBeHidden();
      await page.screenshot({ path: path.join(recordDir, "skipped-credential-check.png") });
      fs.rmSync(path.join(root, "hold-status"));
      assert.deepEqual(await pendingStatus, { ok: true, valid: false });
      assert.equal(creates.length, 2);
      const finalCommands = fs
        .readFileSync(path.join(root, "commands.jsonl"), "utf8")
        .trim()
        .split("\n")
        .map(JSON.parse);
      assert.equal(finalCommands.filter((args) => args[0] === "--force").length, 1);
    } finally {
      fs.rmSync(path.join(root, "hold-status"), { force: true });
      if (fs.existsSync(path.join(root, "sign-in-pid"))) {
        const pid = Number(fs.readFileSync(path.join(root, "sign-in-pid"), "utf8"));
        try {
          process.kill(-pid, "SIGTERM");
        } catch {
          /* Already exited. */
        }
      }
      try {
        if (desktop) {
          try {
            await desktop.electronApp.close();
          } finally {
            await desktop.stopDisplayCapture();
          }
          saveRecording(recordDir, "dbcert-preflight");
        }
      } finally {
        await fetch(`${serverUrl}/__test/provision/release`, {
          method: "POST",
          signal: AbortSignal.timeout(2000),
        }).catch(() => {});
        server.kill("SIGTERM");
        const kill = setTimeout(() => server.kill("SIGKILL"), 5000);
        await serverClosed;
        clearTimeout(kill);
        fs.closeSync(log);
        fs.mkdirSync(recordDir, { recursive: true });
        fs.copyFileSync(serverLog, path.join(recordDir, "server.log"));
        fs.rmSync(root, { recursive: true, force: true });
      }
    }
  },
);
