"use strict";

// Standalone native-shell regression: the real Electron renderer talks to the
// guarded provider fixture and saves visual evidence outside the checkout.
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const readline = require("node:readline");

const checkout = path.resolve(__dirname, "../../..");
const { _electron: electron } = require(path.join(checkout, "web/electron/node_modules/playwright"));
const evidence = process.env.PROVIDER_ELECTRON_EVIDENCE;
if (!evidence) throw new Error("PROVIDER_ELECTRON_EVIDENCE is required");
const state = path.join(evidence, "electron-fixture-state");
const userData = path.join(evidence, "electron-user-data");
const recordings = path.join(evidence, "electron-recordings");
if (fs.existsSync(state) || fs.existsSync(userData)) {
  throw new Error("Electron fixture paths must be fresh");
}
fs.mkdirSync(recordings, { recursive: true });

function childEnvironment() {
  return {
    PATH: "/usr/bin:/bin",
    LANG: "en_US.UTF-8",
    PYTHONDONTWRITEBYTECODE: "1",
    OMNIGENT_DISABLE_KEYRING: "1",
    PYTHON_KEYRING_BACKEND: "keyring.backends.null.Keyring",
  };
}

function waitForFixture() {
  const python = path.join(checkout, ".venv", "bin", "python");
  const child = spawn(
    python,
    [path.join(__dirname, "electron_fixture_runtime.py"), "--state", state],
    { cwd: checkout, env: childEnvironment(), stdio: ["ignore", "pipe", "pipe"] },
  );
  const stderr = [];
  child.stderr.on("data", (chunk) => stderr.push(chunk));
  const ready = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("guarded provider fixture did not start")), 90_000);
    const lines = readline.createInterface({ input: child.stdout });
    lines.on("line", (line) => {
      try {
        const value = JSON.parse(line);
        if (value.url && value.mock_port) {
          clearTimeout(timer);
          resolve(value);
        }
      } catch {
        // Startup diagnostics are not readiness signals.
      }
    });
    child.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`guarded provider fixture exited (${code}): ${Buffer.concat(stderr).toString()}`));
    });
  });
  return { child, ready };
}

async function stopFixture(child) {
  if (child.exitCode !== null) return;
  child.kill("SIGTERM");
  await new Promise((resolve) => child.once("exit", resolve));
}

async function main() {
  const fixture = waitForFixture();
  let app;
  try {
    const { url: serverUrl, mock_port: mockPort } = await fixture.ready;
    fs.mkdirSync(userData, { recursive: true });
    fs.writeFileSync(
      path.join(userData, "settings.json"),
      JSON.stringify({ server_url: serverUrl, update_mode: "none" }),
    );
    app = await electron.launch({
      args: [path.join(__dirname, "electron_fixture_main.cjs"), `--user-data-dir=${userData}`],
      env: childEnvironment(),
    });
    const page = await app.firstWindow();
    await page.goto(`${serverUrl}/settings/providers`);
    await page.getByRole("heading", { name: "Providers", exact: true }).waitFor({ timeout: 30_000 });
    await page.getByTestId("settings-providers-host").click();
    await page.getByRole("option", { name: "Fixture computer A · online", exact: true }).click();
    await page.getByTestId("setup-agent-codex").click();
    const save = page.getByRole("button", { name: "Save gateway", exact: true });
    await page.getByRole("button", { name: "Compatible gateway", exact: true }).click();
    await page.getByLabel("Gateway name", { exact: true }).fill("electron-fixture-gateway");
    await page.getByLabel("Base URL", { exact: true }).fill(`http://127.0.0.1:${mockPort}/v1`);
    await page.getByLabel("Anthropic family", { exact: true }).uncheck();
    await page.getByLabel("OpenAI model", { exact: true }).fill("fixture-model");
    await page.getByLabel("API key or token", { exact: true }).fill("electron-fixture-secret");
    await save.click();
    const row = page.getByTestId("agent-provider-row-electron-fixture-gateway");
    await row.waitFor({ state: "visible", timeout: 20_000 });
    await row.getByRole("button", { name: "Use for new Codex sessions", exact: true }).click();
    await page.goto(`${serverUrl}/settings/appearance`);
    await page.getByTestId("theme-light").click();
    await page.goto(`${serverUrl}/settings/providers`);
    await page.getByTestId("setup-agent-codex").click();
    await page.screenshot({ path: path.join(recordings, "providers-light-saved.png"), fullPage: true });
    await page.goto(`${serverUrl}/settings/appearance`);
    await page.getByTestId("theme-dark").click();
    await page.goto(`${serverUrl}/settings/providers`);
    await page.getByTestId("setup-agent-codex").click();
    await page.screenshot({ path: path.join(recordings, "providers-dark-saved.png"), fullPage: true });
    await page.reload();
    await page.getByTestId("setup-agent-codex").click();
    await row.getByText("Used for new sessions", { exact: true }).waitFor({ timeout: 20_000 });
    assert.equal(await page.locator("body").innerText().then((text) => text.includes("electron-fixture-secret")), false);
    await page.screenshot({ path: path.join(recordings, "providers-dark-reloaded.png"), fullPage: true });
    console.log(JSON.stringify({ result: "passed", recordings }));
    const holdMs = Number.parseInt(process.env.PROVIDER_ELECTRON_HOLD_MS || "0", 10);
    if (holdMs > 0) {
      console.log(JSON.stringify({ result: "ready-for-cua", pid: app.process()?.pid, hold_ms: holdMs }));
      await new Promise((resolve) => setTimeout(resolve, holdMs));
    }
  } finally {
    if (app) await app.close().catch(() => {});
    await stopFixture(fixture.child);
  }
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
