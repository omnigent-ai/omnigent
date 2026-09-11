// Real desktop journey for browsing a selected workspace before creating a session.
//
// The backend and host run through an isolated omnidev pod. The external
// profile prevents omnidev's normal conversation prefill, so any session row
// observed before Start is a product regression rather than fixture data.

"use strict";

// Host capability discovery must never consult the developer's OS keychain.
process.env.OMNIGENT_DISABLE_KEYRING = "1";

const { spawn, spawnSync } = require("node:child_process");
const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const {
  REPO_ROOT,
  WEB_UI_DIST,
  desktopDepsAvailable,
  findFreePort,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();
const RECORD_DIR = path.join(__dirname, "recordings", "desktop-pre-session-workspace");
const OMNIDEV = path.join(REPO_ROOT, "dev", "omnidev", "target", "release", "omnidev");
const MOCK_LLM_SERVER = path.join(
  REPO_ROOT,
  "tests",
  "server",
  "integration",
  "mock_llm_server.py",
);
function testAgentYaml(mockPort) {
  return `name: hello_world
prompt: You are a friendly assistant. Say hello and answer questions.

executor:
  model: gpt-4o-mini
  harness: openai-agents
  auth:
    type: api_key
    api_key: mock-key
    base_url: http://127.0.0.1:${mockPort}/v1

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
`;
}

function wait(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function isolatedChildEnv(overrides = {}) {
  const safe = Object.fromEntries(
    Object.entries(process.env).filter(
      ([key]) =>
        !/^(ANTHROPIC_|CLAUDE_|CODEX_|DATABRICKS_|GEMINI_|OPENAI_|AWS_)/.test(key) &&
        !/^(GH_TOKEN|GITHUB_TOKEN|GOOGLE_APPLICATION_CREDENTIALS|CURSOR_API_KEY)$/.test(key),
    ),
  );
  return { ...safe, ...overrides };
}

async function getJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url} returned ${response.status}`);
  return response.json();
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`${url} returned ${response.status}`);
  return response.json();
}

async function waitForPod(serverUrl, proc, logPath) {
  const deadline = Date.now() + 45_000;
  let lastError = "not attempted";
  /* oxlint-disable no-await-in-loop -- Readiness probes must remain sequential. */
  while (Date.now() < deadline) {
    if (proc.exitCode !== null) break;
    try {
      const health = await getJson(`${serverUrl}/health`);
      const hosts = await getJson(`${serverUrl}/v1/hosts`);
      if (health && hosts.hosts?.some((host) => host.status === "online")) return hosts.hosts;
      lastError = "server healthy, host not online yet";
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await wait(250);
  }
  /* oxlint-enable no-await-in-loop */
  const log = fs.existsSync(logPath) ? fs.readFileSync(logPath, "utf8") : "";
  throw new Error(`isolated omnidev pod did not become ready: ${lastError}\n${log.slice(-5000)}`);
}

function writeProfile(profilePath, agentYaml, hostWrapper) {
  const python = path.join(REPO_ROOT, ".venv", "bin", "omnigent");
  const quoted = (value) => JSON.stringify(value);
  fs.writeFileSync(
    profilePath,
    [
      'backend_dir = "omnigent"',
      'web_dir = "web"',
      "",
      "[server]",
      `command = [${quoted(python)}, "--log-to-stderr", "server", "--host", "127.0.0.1", "--port", "{server_port}", "--database-uri", "sqlite:///{pod_dir}/data/omnigent/chat.db", "--artifact-location", "{pod_dir}/artifacts", "--agent", ${quoted(agentYaml)}]`,
      "",
      "[host]",
      `command = [${quoted(path.join(REPO_ROOT, ".venv", "bin", "python"))}, ${quoted(hostWrapper)}, "--server", "http://127.0.0.1:{server_port}"]`,
      "",
      "[vite]",
      'command = ["/usr/bin/true"]',
      "",
    ].join("\n"),
  );
}

function tclWord(value) {
  return `{${value.replaceAll("\\", "\\\\").replaceAll("}", "\\}")}}`;
}

function writeExpectLauncher(expectPath, args) {
  fs.writeFileSync(
    expectPath,
    [
      "log_user 1",
      "set timeout -1",
      `spawn -noecho ${args.map(tclWord).join(" ")}`,
      'trap {send -- "q"; expect eof; exit} SIGTERM',
      "expect eof",
      "",
    ].join("\n"),
  );
}

function createFixtureRepo(root) {
  const repo = path.join(root, "workspace-fixture");
  fs.mkdirSync(path.join(repo, "src"), { recursive: true });
  fs.writeFileSync(path.join(repo, "README.md"), "# Before Start\n");
  const git = (...args) => {
    const result = spawnSync("git", args, { cwd: repo, encoding: "utf8" });
    assert.equal(result.status, 0, result.stderr);
  };
  git("init", "-b", "main");
  git("config", "user.name", "Desktop E2E");
  git("config", "user.email", "desktop-e2e@example.invalid");
  git("add", "README.md");
  git("commit", "-m", "Initial fixture");
  git("remote", "add", "origin", "https://github.com/omnigent-ai/omnigent.git");
  fs.appendFileSync(path.join(repo, "README.md"), "\nChanged before session creation.\n");
  fs.writeFileSync(path.join(repo, "src", "draft.txt"), "untracked workspace file\n");
  return repo;
}

async function startIsolatedPod(root) {
  assert.ok(
    fs.existsSync(OMNIDEV),
    `build omnidev first: cargo build --manifest-path dev/omnidev/Cargo.toml --locked --release`,
  );
  assert.ok(
    fs.existsSync(path.join(REPO_ROOT, ".venv", "bin", "omnigent")),
    "run uv sync --frozen first",
  );
  assert.ok(
    fs.existsSync(path.join(WEB_UI_DIST, "index.html")),
    "build the SPA before running this journey",
  );

  const podDir = path.join(root, "pod");
  const profilePath = path.join(root, "omnidev.e2e.toml");
  const agentYaml = path.join(root, "hello_world.yaml");
  const hostWrapper = path.join(root, "host.e2e.py");
  const expectPath = path.join(root, "omnidev.e2e.exp");
  const logPath = path.join(root, "omnidev.log");
  const serverPort = await findFreePort();
  const mockPort = await findFreePort();
  const mockLogPath = path.join(root, "mock-llm.log");
  const seedConfigDir = path.join(root, "seed-config");
  fs.mkdirSync(seedConfigDir, { recursive: true });
  fs.writeFileSync(
    path.join(seedConfigDir, "config.yaml"),
    [
      "auth:",
      "  type: none",
      "providers:",
      "  desktop-e2e-mock:",
      "    kind: key",
      "    default: [openai]",
      "    openai:",
      `      base_url: http://127.0.0.1:${mockPort}/v1`,
      "      api_key: mock-key",
      "      wire_api: responses",
      "      models:",
      "        default: gpt-4o-mini",
      "",
    ].join("\n"),
  );
  fs.writeFileSync(agentYaml, testAgentYaml(mockPort));
  fs.writeFileSync(
    hostWrapper,
    [
      "import omnigent.host.connect as connect",
      'connect.configured_harness_map = lambda: {"openai-agents": True}',
      "connect.gateway_inference_map = lambda: {}",
      "from omnigent.host._daemon_entry import main",
      "main()",
      "",
    ].join("\n"),
  );
  writeProfile(profilePath, agentYaml, hostWrapper);
  writeExpectLauncher(expectPath, [
    OMNIDEV,
    "--profile",
    profilePath,
    "--pod-dir",
    podDir,
    "--clean",
    "--no-vite",
    "--server-port",
    String(serverPort),
  ]);
  const log = fs.openSync(logPath, "w");
  const mockLog = fs.openSync(mockLogPath, "w");
  const mockProc = spawn(
    path.join(REPO_ROOT, ".venv", "bin", "python"),
    [MOCK_LLM_SERVER, String(mockPort)],
    {
      cwd: REPO_ROOT,
      env: isolatedChildEnv({
        OMNIGENT_CONFIG_HOME: seedConfigDir,
        OMNIGENT_DISABLE_KEYRING: "1",
        PYTHONPATH: REPO_ROOT,
      }),
      stdio: ["ignore", mockLog, mockLog],
    },
  );
  await waitForHttp(`http://127.0.0.1:${mockPort}/stats`, mockProc, mockLogPath, "mock LLM");
  await postJson(`http://127.0.0.1:${mockPort}/mock/set_fallback`, {
    key: "gpt-4o-mini",
    text: "DESKTOP_HANDOFF_OK",
  });
  const proc = spawn("/usr/bin/expect", [expectPath], {
    cwd: REPO_ROOT,
    env: isolatedChildEnv({
      OMNIGENT_CONFIG_HOME: seedConfigDir,
      OMNIGENT_DATA_DIR: path.join(root, "supervisor-data"),
      GH_CONFIG_DIR: path.join(root, "gh-config"),
      OMNIGENT_DISABLE_KEYRING: "1",
      OMNIGENT_NO_UPDATE_CHECK: "1",
      OPENAI_BASE_URL: `http://127.0.0.1:${mockPort}/v1`,
      OPENAI_API_KEY: "mock-key",
      ANTHROPIC_API_KEY: "",
    }),
    stdio: ["ignore", log, log],
  });
  const serverUrl = `http://127.0.0.1:${serverPort}`;
  let hosts;
  try {
    hosts = await waitForPod(serverUrl, proc, logPath);
  } catch (error) {
    if (proc.exitCode === null) proc.kill("SIGTERM");
    if (mockProc.exitCode === null) mockProc.kill("SIGTERM");
    fs.closeSync(log);
    fs.closeSync(mockLog);
    throw error;
  }
  return {
    serverUrl,
    hostId: hosts.find((host) => host.status === "online").host_id,
    async close() {
      const exited = new Promise((resolve) => {
        proc.once("exit", resolve);
      });
      if (proc.exitCode === null) proc.kill("SIGTERM");
      await Promise.race([exited, wait(10_000)]);
      if (proc.exitCode === null) {
        proc.kill("SIGTERM");
        await Promise.race([exited, wait(2_000)]);
      }
      if (mockProc.exitCode === null) mockProc.kill("SIGTERM");
      fs.closeSync(log);
      fs.closeSync(mockLog);
    },
  };
}

async function waitForHttp(url, proc, logPath, label) {
  const deadline = Date.now() + 30_000;
  let lastError = "not attempted";
  /* oxlint-disable no-await-in-loop -- Readiness probes must remain sequential. */
  while (Date.now() < deadline) {
    if (proc.exitCode !== null) break;
    try {
      const response = await fetch(url);
      if (response.ok) return;
      lastError = `HTTP ${response.status}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await wait(250);
  }
  /* oxlint-enable no-await-in-loop */
  const log = fs.existsSync(logPath) ? fs.readFileSync(logPath, "utf8") : "";
  throw new Error(`${label} did not become ready: ${lastError}\n${log.slice(-5000)}`);
}

async function startDemoPage() {
  const port = await findFreePort();
  const server = http.createServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    response.end(
      "<!doctype html><title>Workspace demo</title>" +
        '<main style="font:24px system-ui;padding:48px"><h1>Local workspace preview</h1>' +
        "<p>Opened safely before Start.</p></main>",
    );
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolve);
  });
  return {
    url: `http://127.0.0.1:${port}/`,
    close: () =>
      new Promise((resolve) => {
        server.close(resolve);
      }),
  };
}

async function waitForFile(filePath, expected, timeoutMs = 20_000) {
  const deadline = Date.now() + timeoutMs;
  /* oxlint-disable no-await-in-loop -- File polling must remain sequential. */
  while (Date.now() < deadline) {
    if (fs.existsSync(filePath) && fs.readFileSync(filePath, "utf8") === expected) return;
    await wait(100);
  }
  /* oxlint-enable no-await-in-loop */
  throw new Error(`${filePath} did not contain ${expected}`);
}

function sessionRows(payload) {
  if (Array.isArray(payload)) return payload;
  return payload.data ?? payload.sessions ?? payload.conversations ?? [];
}

async function captureLandingStorage(window) {
  return window.evaluate(() =>
    Object.fromEntries(
      Object.keys(localStorage)
        .filter(
          (key) =>
            key.startsWith("omnigent:landing-workspace") ||
            key.startsWith("omnigent:draft-workspace-contexts") ||
            key.startsWith("omnigent:new-chat"),
        )
        .map((key) => [key, JSON.parse(localStorage.getItem(key))]),
    ),
  );
}

async function chooseWorkspace(window, fixtureRepo) {
  await window.getByTestId("new-chat-landing-workspace-chip").click();
  await window.getByTestId("new-chat-landing-workspace-open-folder").click();
  const breadcrumbs = window.getByTestId("workspace-picker-breadcrumbs");
  await breadcrumbs.locator("button").last().click();
  const pathInput = window.getByTestId("workspace-picker-path-input");
  await pathInput.fill(fixtureRepo);
  await pathInput.press("Enter");
  await window.getByTestId("workspace-picker-select").click();

  // Draft tools operate on the selected checkout. Clear the default worktree
  // proposal so the workspace exists before Start and Shell is available.
  await window.getByTestId("new-chat-landing-branch-chip").click();
  await window.getByTestId("new-chat-landing-branch-input").fill("");
  await window.getByTestId("new-chat-landing-branch-input").press("Escape");
}

describe(
  "desktop shell — pre-session workspace",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let fixtureRepo;
    let pod;
    let demoPage;

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-pre-session-"));
      fixtureRepo = createFixtureRepo(tmpDir);
      [pod, demoPage] = await Promise.all([startIsolatedPod(tmpDir), startDemoPage()]);
    });

    after(async () => {
      if (pod) await pod.close();
      if (demoPage) await demoPage.close();
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it(
      "browses before Start, then retains the shell and browser in the real session",
      { timeout: 180_000 },
      async () => {
        const userDataDir = path.join(tmpDir, "electron-profile");
        const launched = await launchDesktop({
          serverUrl: pod.serverUrl,
          userDataDir,
          recordDir: RECORD_DIR,
        });
        let sessionCreateRequests = 0;
        const prematureSessionResourceRequests = [];
        let startClicked = false;
        launched.window.on("request", (request) => {
          const requestPath = new URL(request.url()).pathname;
          if (request.method() === "POST" && requestPath === "/v1/sessions") {
            sessionCreateRequests += 1;
          }
          if (!startClicked && /^\/v1\/sessions\/(?:[^/]*)\/resources(?:\/|$)/.test(requestPath)) {
            prematureSessionResourceRequests.push(`${request.method()} ${requestPath}`);
          }
        });
        launched.window.on("websocket", (socket) => {
          const socketPath = new URL(socket.url()).pathname;
          if (!startClicked && /^\/v1\/sessions\/(?:[^/]*)\/resources(?:\/|$)/.test(socketPath)) {
            prematureSessionResourceRequests.push(`WS ${socketPath}`);
          }
        });
        let saved;
        let browserTabId;
        let workspaceContextId;
        try {
          await windowReady(launched.window);
          assert.equal(sessionRows(await getJson(`${pod.serverUrl}/v1/sessions`)).length, 0);

          await chooseWorkspace(launched.window, fixtureRepo);
          const panel = launched.window.locator("[data-workspace-panel-content]");
          await panel.waitFor({ state: "hidden", timeout: 20_000 });
          await launched.window.getByRole("button", { name: "Expand right panel" }).click();
          await panel.waitFor({ state: "visible", timeout: 20_000 });

          await launched.window.getByRole("tab", { name: "Files" }).click();
          await panel.getByText("README.md", { exact: true }).waitFor({ state: "visible" });

          await launched.window.getByRole("tab", { name: /Changes/ }).click();
          await panel.getByText("draft.txt", { exact: true }).waitFor({ state: "visible" });

          await launched.window.getByRole("tab", { name: "GitHub" }).click();
          const githubState = panel
            .getByText(
              /omnigent-ai\/omnigent|no open pr|pull requests created|upstream repo|sign in|authentication/i,
            )
            .first();
          await githubState.waitFor({ state: "visible" });
          fs.writeFileSync(
            path.join(RECORD_DIR, "github-state.txt"),
            `${await githubState.innerText()}\n`,
          );

          await launched.window.getByRole("button", { name: "Open new" }).click();
          await launched.window.getByText("Shell (bash)", { exact: true }).click();
          const terminal = panel.locator(".xterm-helper-textarea");
          await terminal.waitFor({ state: "visible", timeout: 20_000 });
          workspaceContextId = await launched.window.evaluate(() => {
            const key = Object.keys(localStorage).find((candidate) =>
              candidate.startsWith("omnigent:draft-workspace-contexts:v1:"),
            );
            const contexts = key ? JSON.parse(localStorage.getItem(key)) : [];
            return contexts[0]?.id ?? null;
          });
          assert.ok(workspaceContextId, "draft workspace context was not persisted");
          await terminal.click();
          await terminal.pressSequentially(
            "printf '%s' \"$PWD\" > .desktop-e2e-pwd; export OMNIGENT_PRESTART_MARKER=retained; pwd; git status --short",
          );
          await terminal.press("Enter");
          await waitForFile(
            path.join(fixtureRepo, ".desktop-e2e-pwd"),
            fs.realpathSync(fixtureRepo),
          );

          await launched.window.getByRole("button", { name: "Open new" }).click();
          await launched.window.getByRole("menuitem", { name: "Browser", exact: true }).click();
          const address = launched.window.getByRole("textbox", { name: "Address bar" });
          await address.fill(demoPage.url);
          await address.press("Enter");
          await launched.window.waitForFunction(
            () => {
              for (const key of Object.keys(localStorage)) {
                try {
                  const candidate = JSON.parse(localStorage.getItem(key));
                  if (candidate?.panel?.selectedBrowserId && candidate?.browserNamespace)
                    return true;
                } catch {
                  // Other isolated app preferences need not contain JSON.
                }
              }
              return false;
            },
            null,
            { timeout: 20_000 },
          );
          const browserState = await launched.window.evaluate(() => {
            for (const key of Object.keys(localStorage)) {
              try {
                const candidate = JSON.parse(localStorage.getItem(key));
                if (candidate?.panel?.selectedBrowserId && candidate?.browserNamespace) {
                  return {
                    tabId: candidate.panel.selectedBrowserId,
                    viewId: `browser-tab:${encodeURIComponent(candidate.browserNamespace)}:${candidate.panel.selectedBrowserId}`,
                  };
                }
              } catch {
                // Other isolated app preferences need not contain JSON.
              }
            }
            throw new Error("landing browser state was not persisted");
          });
          browserTabId = browserState.tabId;
          await launched.window.waitForFunction(
            async ([viewId, expected]) => {
              const result = await window.omnigentDesktop.browserExecute(
                viewId,
                "document.body.innerText",
              );
              return result.ok && String(result.result).includes(expected);
            },
            [browserState.viewId, "Opened safely before Start."],
            { timeout: 20_000 },
          );
          const storageBeforeReload = await captureLandingStorage(launched.window);

          await launched.window.reload();
          await windowReady(launched.window);
          await panel.waitFor({ state: "hidden", timeout: 20_000 });
          await launched.window.getByRole("button", { name: "Expand right panel" }).click();
          await panel.waitFor({ state: "visible", timeout: 20_000 });
          const storageAfterReload = await captureLandingStorage(launched.window);
          fs.writeFileSync(
            path.join(RECORD_DIR, "reload-storage.json"),
            `${JSON.stringify({ before: storageBeforeReload, after: storageAfterReload }, null, 2)}\n`,
          );
          assert.equal(sessionRows(await getJson(`${pod.serverUrl}/v1/sessions`)).length, 0);
          assert.equal(sessionCreateRequests, 0, "reload created a session before Start");
          await launched.window
            .locator('[role="button"][title]')
            .filter({ hasText: /bash/i })
            .first()
            .click();
          const reloadedTerminal = panel.locator(".xterm-helper-textarea");
          await reloadedTerminal.waitFor({ state: "visible", timeout: 20_000 });
          await reloadedTerminal.click();
          await reloadedTerminal.pressSequentially(
            "printf '%s' \"$OMNIGENT_PRESTART_MARKER\" > .desktop-e2e-reload",
          );
          await reloadedTerminal.press("Enter");
          await waitForFile(path.join(fixtureRepo, ".desktop-e2e-reload"), "retained");
          await launched.window.getByRole("tab", { name: "Browser", exact: true }).click();
          await launched.window.getByRole("tab", { name: "Browser 1", exact: true }).click();
          await launched.window.waitForFunction(
            async ([viewId, expected]) => {
              const result = await window.omnigentDesktop.browserExecute(
                viewId,
                "document.body.innerText",
              );
              return result.ok && String(result.result).includes(expected);
            },
            [browserState.viewId, "Opened safely before Start."],
            { timeout: 20_000 },
          );

          const browserPaneBounds = await launched.window
            .locator(`[data-browser-pane-conversation="${browserState.viewId}"]`)
            .evaluate((pane) => {
              const rect = pane.lastElementChild.getBoundingClientRect();
              return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
            });
          const nativeViews = await launched.electronApp.evaluate(({ BrowserWindow }) => {
            return BrowserWindow.getAllWindows().flatMap((window) =>
              window.contentView.children
                .filter((view) => view.webContents)
                .map((view) => ({
                  bounds: view.getBounds(),
                  url: view.webContents.getURL(),
                  windowUrl: window.webContents.getURL(),
                })),
            );
          });
          fs.writeFileSync(
            path.join(RECORD_DIR, "browser-view-children.json"),
            `${JSON.stringify({ expectedUrl: demoPage.url, browserPaneBounds, nativeViews }, null, 2)}\n`,
          );
          const nativeBrowserView = nativeViews.find((view) => view.url === demoPage.url);
          assert.ok(nativeBrowserView, "native browser view was not attached");
          for (const edge of ["x", "y", "width", "height"]) {
            assert.ok(
              Math.abs(nativeBrowserView.bounds[edge] - browserPaneBounds[edge]) <= 2,
              `native ${edge} did not match the BrowserPane placeholder`,
            );
          }

          await launched.window.getByRole("tab", { name: /Agents 0/ }).click();
          await panel
            .getByText("No agents yet. Start a chat to add an agent.", { exact: true })
            .waitFor({
              state: "visible",
            });
          assert.equal(sessionRows(await getJson(`${pod.serverUrl}/v1/sessions`)).length, 0);
          assert.equal(sessionCreateRequests, 0, "a session POST occurred before Start");
          assert.deepEqual(
            prematureSessionResourceRequests,
            [],
            "a session-scoped resource request occurred before Start",
          );

          const toggle = launched.window.getByRole("button", { name: "Collapse right panel" });
          await toggle.click();
          await panel.waitFor({ state: "hidden" });
          await launched.window.getByRole("button", { name: "Expand right panel" }).click();
          await panel.waitFor({ state: "visible" });
          const widthBefore = (await panel.boundingBox()).width;
          const resizeHandle = launched.window.getByRole("separator", { name: "Resize panel" });
          await resizeHandle.press("ArrowLeft");
          await resizeHandle.press("ArrowLeft");
          const widthAfter = (await panel.boundingBox()).width;
          assert.ok(widthAfter > widthBefore, `${widthBefore} did not grow after resize`);

          await launched.window.getByTestId("new-chat-landing-agent-select").click();
          await launched.window.getByTestId("new-chat-landing-custom-agents").hover();
          await launched.window.getByText(/hello_world/i, { exact: true }).click();
          await launched.window.keyboard.press("Escape");
          await launched.window.waitForFunction(
            () => getComputedStyle(document.body).pointerEvents !== "none",
          );
          assert.equal(
            await launched.window.locator('[role="menu"]:visible, [role="dialog"]:visible').count(),
            0,
            "agent picker remained visible after dismissal",
          );
          await launched.window
            .getByTestId("new-chat-landing-input")
            .fill("Start the demo workspace");
          const startBounds = await launched.window
            .getByTestId("new-chat-landing-submit")
            .boundingBox();
          assert.ok(startBounds, "Start button did not have renderer bounds");
          const overlapsBrowserPane = !(
            startBounds.x + startBounds.width <= browserPaneBounds.x ||
            browserPaneBounds.x + browserPaneBounds.width <= startBounds.x ||
            startBounds.y + startBounds.height <= browserPaneBounds.y ||
            browserPaneBounds.y + browserPaneBounds.height <= startBounds.y
          );
          assert.equal(overlapsBrowserPane, false, "native browser bounds overlapped Start");
          const startHitTarget = await launched.window.evaluate(({ x, y, width, height }) => {
            const target = document.elementFromPoint(x + width / 2, y + height / 2);
            const button = document.querySelector('[data-testid="new-chat-landing-submit"]');
            return {
              html: target?.outerHTML.slice(0, 500) ?? null,
              ancestors: button
                ? Array.from(
                    (function* () {
                      let element = button;
                      while (element) {
                        yield element;
                        element = element.parentElement;
                      }
                    })(),
                    (element) => ({
                      tag: element.tagName,
                      className: element.className,
                      pointerEvents: getComputedStyle(element).pointerEvents,
                    }),
                  )
                : [],
            };
          }, startBounds);
          fs.writeFileSync(
            path.join(RECORD_DIR, "browser-view-bounds.json"),
            `${JSON.stringify({ browserPaneBounds, nativeBrowserView, startBounds, startHitTarget }, null, 2)}\n`,
          );
          await launched.electronApp.evaluate(({ BrowserWindow }) => {
            BrowserWindow.getAllWindows()[0].webContents.focus();
          });
          startClicked = true;
          await launched.window.getByTestId("new-chat-landing-submit").click();
          await launched.window.waitForURL(/\/c\/[^/]+$/, { timeout: 45_000 });
          assert.equal(sessionCreateRequests, 1, "Start did not issue exactly one session POST");
          const sessionId = new URL(launched.window.url()).pathname
            .split("/")
            .filter(Boolean)
            .at(-1);
          assert.ok(sessionId);
          const sessions = sessionRows(await getJson(`${pod.serverUrl}/v1/sessions`));
          assert.equal(sessions.length, 1);
          assert.equal(sessions[0].id, sessionId);
          const session = await getJson(
            `${pod.serverUrl}/v1/sessions/${sessionId}?include_items=false`,
          );
          assert.equal(session.host_id, pod.hostId);
          assert.equal(session.workspace, fs.realpathSync(fixtureRepo));
          await launched.window
            .getByText("DESKTOP_HANDOFF_OK", { exact: true })
            .waitFor({ state: "visible", timeout: 45_000 });

          const sessionPanel = launched.window.locator("[data-workspace-panel-content]");
          await sessionPanel.waitFor({ state: "visible", timeout: 20_000 });
          const retainedShellTab = launched.window
            .locator('[role="button"][title]')
            .filter({ hasText: /bash/i })
            .first();
          await retainedShellTab.click();
          const retainedTerminal = sessionPanel.locator(".xterm-helper-textarea");
          await retainedTerminal.waitFor({ state: "visible", timeout: 20_000 });
          await retainedTerminal.click();
          await retainedTerminal.pressSequentially(
            "printf '%s' \"$OMNIGENT_PRESTART_MARKER\" > .desktop-e2e-retained; echo __$OMNIGENT_PRESTART_MARKER__",
          );
          await retainedTerminal.press("Enter");
          await waitForFile(path.join(fixtureRepo, ".desktop-e2e-retained"), "retained");

          await launched.window.getByRole("tab", { name: "Browser", exact: true }).click();
          await launched.window.getByRole("tab", { name: "Browser 1", exact: true }).click();
          await launched.window
            .getByRole("textbox", { name: "Address bar" })
            .waitFor({ state: "visible" });
          assert.equal(
            await launched.window.getByRole("textbox", { name: "Address bar" }).inputValue(),
            demoPage.url,
          );
          const adoptedBrowserViewId = `browser-tab:${encodeURIComponent(sessionId)}:${browserTabId}`;
          await launched.window.waitForFunction(
            async ([viewId, expected]) => {
              const result = await window.omnigentDesktop.browserExecute(
                viewId,
                "document.body.innerText",
              );
              return result.ok && String(result.result).includes(expected);
            },
            [adoptedBrowserViewId, "Opened safely before Start."],
            { timeout: 20_000 },
          );
          await wait(1_000);
        } finally {
          if (workspaceContextId) {
            await fetch(
              `${pod.serverUrl}/v1/hosts/${pod.hostId}/workspace-contexts/${workspaceContextId}`,
              { method: "DELETE" },
            ).catch(() => {});
          }
          await launched.electronApp.close();
          await launched.stopDisplayCapture();
          saved = saveRecording(RECORD_DIR, "pre-session-workspace");
          if (saved[0]) {
            spawnSync(
              "ffmpeg",
              [
                "-y",
                "-sseof",
                "-1",
                "-i",
                saved[0],
                "-frames:v",
                "1",
                path.join(RECORD_DIR, "after-start.png"),
              ],
              { stdio: "ignore" },
            );
          }
        }
        assert.ok(saved.length > 0, "no desktop recording was produced");
      },
    );
  },
);

async function windowReady(window) {
  await window.getByTestId("new-chat-landing").waitFor({ state: "visible", timeout: 30_000 });
}
