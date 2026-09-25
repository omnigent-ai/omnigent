"use strict";

const assert = require("node:assert/strict");
const { spawn, spawnSync } = require("node:child_process");
const { createHash, randomBytes } = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { APP_ROOT, REPO_ROOT, WEB_UI_DIST, findFreePort } = require("./desktopHarness");

const PYTHON = process.env.OMNIGENT_PYTHON || "python3";
const MOCK_REPLY = "Design instruction received.";

async function eventually(probe, label, timeout = 30_000) {
  const deadline = Date.now() + timeout;
  let last;
  while (Date.now() < deadline) {
    try {
      // oxlint-disable-next-line no-await-in-loop -- Poll until the asynchronous state settles.
      const result = await probe();
      if (result) return result;
    } catch (error) {
      last = error;
    }
    // oxlint-disable-next-line no-await-in-loop -- Polling backoff.
    await new Promise((resolve) => {
      setTimeout(resolve, 100);
    });
  }
  throw new Error(`Timed out waiting for ${label}${last ? `: ${last.message}` : ""}`);
}

function isolatedEnv(configDir) {
  return {
    ...Object.fromEntries(
      Object.entries(process.env).filter(
        ([key]) =>
          !/^(OMNIGENT_|RUNNER_|OPENAI_|ANTHROPIC_|DATABRICKS_|MLFLOW_)/.test(key) &&
          key !== "ELECTRON_RUN_AS_NODE",
      ),
    ),
    PYTHONPATH: REPO_ROOT,
    OMNIGENT_CONFIG_HOME: configDir,
    OMNIGENT_DATA_DIR: path.join(configDir, "data"),
    OPENAI_API_KEY: "mock-key",
  };
}

async function jsonRequest(url, init) {
  const response = await fetch(url, { ...init, signal: AbortSignal.timeout(30_000) });
  const body = await response.text();
  assert.ok(response.ok, `${url}: HTTP ${response.status}: ${body}`);
  return JSON.parse(body);
}

async function startDesignBackend(tmpDir) {
  assert.ok(fs.existsSync(path.join(WEB_UI_DIST, "index.html")), "Build the real web SPA first");
  const children = [];
  const logHandles = [];
  const env = isolatedEnv(path.join(tmpDir, "config"));
  const start = (name, args, extraEnv = {}) => {
    const log = fs.openSync(path.join(tmpDir, `${name}.log`), "w");
    logHandles.push(log);
    const child = spawn(PYTHON, args, {
      cwd: tmpDir,
      env: { ...env, ...extraEnv },
      stdio: ["ignore", log, log],
    });
    child.on("error", () => {});
    children.push(child);
    return child;
  };
  const close = async () => {
    await Promise.all(
      children.reverse().map(async (child) => {
        if (!child.pid || child.exitCode !== null || child.signalCode !== null) return;
        const exited = new Promise((resolve) => {
          child.once("close", resolve);
        });
        child.kill("SIGTERM");
        const timeout = setTimeout(() => child.kill("SIGKILL"), 5_000);
        await exited;
        clearTimeout(timeout);
      }),
    );
    for (const handle of logHandles) fs.closeSync(handle);
  };
  try {
    const mockPort = await findFreePort();
    const mockUrl = `http://127.0.0.1:${mockPort}`;
    start("mock", [
      path.join(REPO_ROOT, "tests/server/integration/mock_llm_server.py"),
      String(mockPort),
    ]);
    await eventually(() => jsonRequest(`${mockUrl}/stats`), "mock LLM");
    await Promise.all(
      [
        ["gpt-4o-mini", MOCK_REPLY],
        ["_policy_llm_", '{"action":"allow","reason":""}'],
      ].map(([key, text]) =>
        jsonRequest(`${mockUrl}/mock/set_fallback`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ key, text, stream: true }),
        }),
      ),
    );

    const serverPort = await findFreePort();
    const serverUrl = `http://127.0.0.1:${serverPort}`;
    const providerEnv = { OPENAI_BASE_URL: `${mockUrl}/v1` };
    start(
      "server",
      [
        "-c",
        "from omnigent.cli import main; main()",
        "server",
        "--host",
        "127.0.0.1",
        "--port",
        String(serverPort),
        "--database-uri",
        `sqlite:///${path.join(tmpDir, "test.db")}`,
        "--artifact-location",
        path.join(tmpDir, "artifacts"),
      ],
      { ...providerEnv, OMNIGENT_WEB_UI_DIST: WEB_UI_DIST },
    );
    await eventually(() => jsonRequest(`${serverUrl}/health`), "real Omnigent server", 60_000);

    const bindingToken = randomBytes(32).toString("hex");
    const runnerId = `runner_token_${createHash("sha256").update(`omnigent-runner:${bindingToken}`).digest("hex").slice(0, 32)}`;
    start("runner", ["-m", "omnigent.runner._entry"], {
      ...providerEnv,
      RUNNER_SERVER_URL: serverUrl,
      OMNIGENT_RUNNER_ID: runnerId,
      OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN: bindingToken,
      OMNIGENT_RUNNER_PARENT_PID: String(process.pid),
      OMNIGENT_RUNNER_WORKSPACE: tmpDir,
      OMNIGENT_RUNNER_HOST_OWNS_GLOBAL_CLEANUP: "1",
    });
    await eventually(
      async () => (await jsonRequest(`${serverUrl}/v1/runners/${runnerId}/status`)).online,
      "isolated runner tunnel",
      60_000,
    );

    fs.writeFileSync(
      path.join(tmpDir, "hello_world.yaml"),
      [
        "name: hello_world",
        "prompt: Acknowledge design instructions with a short reply.",
        "executor:",
        "  model: gpt-4o-mini",
        "  harness: openai-agents",
        "os_env:",
        "  type: caller_process",
        `  cwd: ${JSON.stringify(tmpDir)}`,
        "  sandbox:",
        "    type: none",
        "",
      ].join("\n"),
    );
    const archive = spawnSync("tar", ["-czf", "-", "-C", tmpDir, "hello_world.yaml"], {
      env: { ...process.env, COPYFILE_DISABLE: "1" },
    });
    assert.equal(archive.status, 0, archive.stderr?.toString());
    const form = new FormData();
    form.set("metadata", JSON.stringify({}));
    form.set("bundle", new Blob([archive.stdout], { type: "application/gzip" }), "agent.tar.gz");
    const created = await jsonRequest(`${serverUrl}/v1/sessions`, { method: "POST", body: form });
    const sessionId = created.session_id;
    assert.equal(typeof sessionId, "string");
    await jsonRequest(`${serverUrl}/v1/sessions/${sessionId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ runner_id: runnerId }),
    });
    return { serverUrl, sessionId, mockUrl, close };
  } catch (error) {
    await close();
    throw error;
  }
}

const NATIVE_DIALOG_PAGE = `<!doctype html><html><meta charset="utf-8">
<title>Capacity planning fixture</title>
<style>
body{margin:0;padding:24px;background:#f4f6fa;color:#17243a;font:15px system-ui}
dialog{width:min(380px,calc(100vw - 70px));border:1px solid #d4dce8;border-radius:12px;padding:24px}
dialog::backdrop{background:#17243a66}h1{font-size:24px}h2{font-size:20px;margin:0 0 20px}
label{display:flex;flex-direction:column;gap:7px;margin:16px 0;font-size:13px}
input{box-sizing:border-box;width:100%;padding:9px;border:1px solid #ccd5e2;border-radius:6px;font:15px system-ui}
button{padding:9px 14px;border:0;border-radius:6px;background:#345ddd;color:white;font:14px system-ui}
</style><h1>Capacity planning</h1><p>Native modal form; no synthetic focus or value setters.</p>
<label for="standalone-period">Standalone period<input id="standalone-period" value="W41"></label>
<button id="create-scenario">Create capacity scenario</button>
<dialog id="capacity-dialog"><h2>Capacity assumptions</h2>
<label for="scenario-name">Name<input id="scenario-name" value="test"></label>
<label for="scenario-resource">Resource<input id="scenario-resource" value="Reno - Pack-out 2"></label>
<label for="scenario-period">Period<input id="scenario-period" value="W41"></label>
<label for="scenario-available-units">Available units<input id="scenario-available-units" value="3540"></label>
<button id="cancel-scenario">Cancel</button></dialog>
<script>
const dialog=document.getElementById('capacity-dialog');
document.getElementById('create-scenario').onclick=()=>dialog.showModal();
document.getElementById('cancel-scenario').onclick=()=>dialog.close();
window.__reproEvents=[];
for(const type of ['pointerdown','focusin','focusout','keydown','input']){
  document.addEventListener(type,event=>{
    window.__reproEvents.push({type,target:event.target.id,key:event.key,value:event.target.value,
      active:document.activeElement.id,trusted:event.isTrusted});
  },true);
}
dialog.showModal();
</script></html>`;

async function startFormFixture() {
  if (process.env.OMNIGENT_DESKTOP_FORM_URL) {
    return { url: process.env.OMNIGENT_DESKTOP_FORM_URL, close: async () => {} };
  }
  const server = http.createServer((_request, response) => {
    response.writeHead(200, {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
    });
    response.end(NATIVE_DIALOG_PAGE);
  });
  await new Promise((resolve) => {
    server.listen(0, "127.0.0.1", resolve);
  });
  return {
    url: `http://127.0.0.1:${server.address().port}/modal`,
    close: () =>
      new Promise((resolve) => {
        server.close(resolve);
      }),
  };
}

async function launchDesignDesktop(tmpDir, recordDir, serverUrl) {
  const { _electron } = require("playwright");
  const userDataDir = path.join(tmpDir, "user-data");
  fs.mkdirSync(userDataDir);
  fs.writeFileSync(
    path.join(userDataDir, "settings.json"),
    JSON.stringify({
      server_url: serverUrl,
      update_mode: "none",
      update_auto_install: false,
    }),
  );
  const executablePath = process.env.OMNIGENT_DESKTOP_EXECUTABLE;
  const args = [...(executablePath ? [] : [APP_ROOT]), `--user-data-dir=${userDataDir}`];
  if (process.env.OMNIGENT_PW_NO_SANDBOX) args.push("--no-sandbox", "--disable-dev-shm-usage");
  const electronApp = await _electron.launch({
    ...(executablePath ? { executablePath } : {}),
    args,
    cwd: APP_ROOT,
    env: {
      ...isolatedEnv(path.join(tmpDir, "config")),
      OMNIGENT_DESKTOP_VERSION_OVERRIDE: "999.0.0",
    },
    ...(recordDir ? { recordVideo: { dir: recordDir } } : {}),
  });
  electronApp.context().setDefaultTimeout(15_000);
  try {
    const window = await eventually(
      () => electronApp.windows().find((page) => page.url().startsWith(serverUrl)),
      "real SPA window",
    );
    return { electronApp, window };
  } catch (error) {
    await electronApp.close();
    throw error;
  }
}

async function startMacCapture(electronApp, windowId, recordDir) {
  if (process.platform !== "darwin" || process.env.OMNIGENT_DESKTOP_COMPOSITED_VIDEO !== "1") {
    return async () => {};
  }
  const captureId =
    process.env.OMNIGENT_DESKTOP_CAPTURE_WINDOW_ID ||
    (await electronApp.evaluate(async ({ BrowserWindow, desktopCapturer }, id) => {
      const window = BrowserWindow.fromId(id);
      const mediaSourceId = window.getMediaSourceId?.();
      if (mediaSourceId?.startsWith("window:")) return mediaSourceId.split(":")[1];
      const title = window.getTitle();
      const captureTitle = `Omnigent design prompt regression ${process.pid}`;
      window.setTitle(captureTitle);
      try {
        const sources = await desktopCapturer.getSources({
          types: ["window"],
          thumbnailSize: { width: 0, height: 0 },
        });
        return sources.find((source) => source.name === captureTitle)?.id.split(":")[1];
      } finally {
        window.setTitle(title);
      }
    }, windowId));
  assert.ok(captureId, "Could not resolve the isolated macOS window for composited recording");
  const output = path.join(recordDir, "design-prompt-native.mov");
  const capture = spawn("/usr/sbin/screencapture", ["-v", "-l", String(captureId), "-x", output], {
    stdio: ["ignore", "ignore", "pipe"],
  });
  let failure = "";
  capture.stderr.on("data", (chunk) => {
    failure += chunk;
  });
  capture.on("error", (error) => {
    failure += error.message;
  });
  const stopped = new Promise((resolve) => {
    capture.once("close", resolve);
  });
  return async () => {
    if (capture.exitCode === null && capture.signalCode === null) capture.kill("SIGINT");
    const timeout = setTimeout(() => capture.kill("SIGKILL"), 5_000);
    await stopped;
    clearTimeout(timeout);
    assert.ok(
      fs.existsSync(output) && fs.statSync(output).size > 0,
      `No composited recording: ${failure}`,
    );
  };
}

module.exports = {
  NATIVE_DIALOG_PAGE,
  MOCK_REPLY,
  eventually,
  startDesignBackend,
  startFormFixture,
  launchDesignDesktop,
  startMacCapture,
};
