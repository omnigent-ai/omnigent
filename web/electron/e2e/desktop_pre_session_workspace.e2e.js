// Real desktop journey for browsing a selected workspace before creating a session.
//
// The backend and host run through an isolated omnidev pod. The external
// profile prevents omnidev's normal conversation prefill, so any session row
// observed before Start is a product regression rather than fixture data.

"use strict";

// Host capability discovery must never consult the developer's OS keychain.
process.env.OMNIGENT_DISABLE_KEYRING = "1";

const { spawnSync } = require("node:child_process");
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
const {
  isolatedChildEnv,
  resolveShellWindow,
  startPodServices,
  startWorkspaceFixtures,
} = require("./desktop_pre_session_workspace_setup");

const deps = desktopDepsAvailable();
const RECORD_DIR =
  process.env.OMNIGENT_PRECHAT_EVIDENCE_DIR ||
  path.join(__dirname, "recordings", "desktop-pre-session-workspace");
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
  fs.appendFileSync(path.join(repo, "README.md"), "\nChanged before session creation.\n");
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
  const fixtureHome = path.join(root, "home");
  fs.mkdirSync(seedConfigDir, { recursive: true });
  fs.mkdirSync(fixtureHome, { recursive: true });
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
  const serverUrl = `http://127.0.0.1:${serverPort}`;
  const services = await startPodServices({
    mock: {
      command: path.join(REPO_ROOT, ".venv", "bin", "python"),
      args: [MOCK_LLM_SERVER, String(mockPort)],
      logPath: mockLogPath,
      options: {
        cwd: REPO_ROOT,
        env: isolatedChildEnv({
          HOME: fixtureHome,
          OMNIGENT_CONFIG_HOME: seedConfigDir,
          OMNIGENT_DISABLE_KEYRING: "1",
          PYTHONPATH: REPO_ROOT,
        }),
      },
    },
    pod: {
      command: "/usr/bin/expect",
      args: [expectPath],
      logPath,
      options: {
        cwd: REPO_ROOT,
        env: isolatedChildEnv({
          HOME: fixtureHome,
          OMNIGENT_CONFIG_HOME: seedConfigDir,
          OMNIGENT_DATA_DIR: path.join(root, "supervisor-data"),
          GH_CONFIG_DIR: path.join(root, "gh-config"),
          PYTHONPATH: REPO_ROOT,
          OMNIGENT_DISABLE_KEYRING: "1",
          OMNIGENT_NO_UPDATE_CHECK: "1",
          OPENAI_BASE_URL: `http://127.0.0.1:${mockPort}/v1`,
          OPENAI_API_KEY: "mock-key",
          ANTHROPIC_API_KEY: "",
        }),
      },
    },
    waitForMock: (proc, mockLog) =>
      waitForHttp(`http://127.0.0.1:${mockPort}/stats`, proc, mockLog, "mock LLM"),
    configureMock: () =>
      postJson(`http://127.0.0.1:${mockPort}/mock/set_fallback`, {
        key: "gpt-4o-mini",
        text: "DESKTOP_HANDOFF_OK",
      }),
    waitForPod: async (proc, podLog) => {
      const hosts = await waitForPod(serverUrl, proc, podLog);
      const servedHtml = await fetch(`${serverUrl}/`).then((response) => response.text());
      assert.equal(
        servedHtml,
        fs.readFileSync(path.join(WEB_UI_DIST, "index.html"), "utf8"),
        "isolated pod served a different frontend build",
      );
      return hosts;
    },
  });
  return {
    serverUrl,
    hostId: services.hosts.find((host) => host.status === "online").host_id,
    close: services.close,
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
      tmpDir = fs.mkdtempSync(
        path.join(process.env.OMNIGENT_PRECHAT_TMPDIR || os.tmpdir(), "omni-desktop-pre-session-"),
      );
      fixtureRepo = createFixtureRepo(tmpDir);
      ({ pod, demoPage } = await startWorkspaceFixtures(
        () => startIsolatedPod(tmpDir),
        startDemoPage,
      ));
    });

    after(async () => {
      try {
        await Promise.all([pod?.close(), demoPage?.close()]);
      } finally {
        try {
          if (tmpDir) {
            for (const [name, relative] of [
              ["host.log", "pod/logs/host.log"],
              ["server.log", "pod/logs/server.log"],
            ]) {
              const source = path.join(tmpDir, relative);
              if (fs.existsSync(source)) {
                fs.mkdirSync(RECORD_DIR, { recursive: true });
                fs.copyFileSync(source, path.join(RECORD_DIR, name));
              }
            }
          }
        } finally {
          if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
        }
      }
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
        let saved;
        let browserTabId;
        try {
          const shell = await resolveShellWindow(launched.electronApp, pod.serverUrl);
          shell.on("request", (request) => {
            const requestPath = new URL(request.url()).pathname;
            if (request.method() === "POST" && requestPath === "/v1/sessions") {
              sessionCreateRequests += 1;
            }
            if (
              !startClicked &&
              /^\/v1\/sessions\/(?:[^/]*)\/resources(?:\/|$)/.test(requestPath)
            ) {
              prematureSessionResourceRequests.push(`${request.method()} ${requestPath}`);
            }
          });
          shell.on("websocket", (socket) => {
            const socketPath = new URL(socket.url()).pathname;
            if (!startClicked && /^\/v1\/sessions\/(?:[^/]*)\/resources(?:\/|$)/.test(socketPath)) {
              prematureSessionResourceRequests.push(`WS ${socketPath}`);
            }
          });
          await windowReady(shell);
          assert.equal(sessionRows(await getJson(`${pod.serverUrl}/v1/sessions`)).length, 0);

          await chooseWorkspace(shell, fixtureRepo);
          const panel = shell.locator("[data-workspace-panel-content]");
          await panel.waitFor({ state: "hidden", timeout: 20_000 });
          await shell.screenshot({ path: path.join(RECORD_DIR, "initial-collapsed.png") });
          await shell.getByRole("button", { name: "Expand right panel" }).click();
          await panel.waitFor({ state: "visible", timeout: 20_000 });
          const panelShortcut = process.platform === "darwin" ? "Meta+Alt+]" : "Control+Alt+]";
          await shell.keyboard.press(panelShortcut);
          await panel.waitFor({ state: "hidden" });
          await shell.getByTestId("new-chat-landing-input").focus();
          await shell.keyboard.press(panelShortcut);
          await panel.waitFor({ state: "visible" });
          await shell.getByTestId("new-chat-button").click();
          await panel.waitFor({ state: "hidden", timeout: 20_000 });
          await shell.screenshot({
            path: path.join(RECORD_DIR, "same-page-new-session-collapsed.png"),
          });
          await shell.getByRole("button", { name: "Expand right panel" }).click();
          await panel.waitFor({ state: "visible", timeout: 20_000 });

          await shell.getByRole("button", { name: "Open new" }).click();
          await shell.getByText("Shell (bash)", { exact: true }).click();
          const terminal = panel.locator(".xterm-helper-textarea");
          await terminal.waitFor({ state: "visible", timeout: 20_000 });
          await terminal.click();
          await terminal.pressSequentially(
            "printf '%s' \"$PWD\" > .desktop-e2e-pwd; export OMNIGENT_PRESTART_MARKER=retained; pwd; git status --short",
          );
          await terminal.press("Enter");
          await waitForFile(
            path.join(fixtureRepo, ".desktop-e2e-pwd"),
            fs.realpathSync(fixtureRepo),
          );

          await shell.getByRole("button", { name: "Open new" }).click();
          await shell.getByRole("menuitem", { name: "Browser", exact: true }).click();
          const address = shell.getByRole("textbox", { name: "Address bar" });
          assert.ok(["", "about:blank"].includes(await address.inputValue()));
          await shell.screenshot({ path: path.join(RECORD_DIR, "before-start-browser-blank.png") });
          await address.fill(demoPage.url);
          await address.press("Enter");
          await shell.waitForFunction(
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
          const browserState = await shell.evaluate(() => {
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
          await shell.waitForFunction(
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
          const savedMessage = "Draft preserved while identity resolves";
          await shell.getByTestId("new-chat-landing-input").fill(savedMessage);
          await shell.waitForFunction((message) => {
            return Object.keys(localStorage).some(
              (key) =>
                key.startsWith("omnigent:landing-composer") &&
                JSON.parse(localStorage.getItem(key))?.message === message,
            );
          }, savedMessage);

          let releaseIdentity;
          let identityRequests = 0;
          const identityGate = new Promise((resolve) => {
            releaseIdentity = resolve;
          });
          await shell.route("**/v1/me", async (route) => {
            identityRequests += 1;
            await identityGate;
            await route.continue();
          });
          try {
            await shell.reload();
            await windowReady(shell);
            assert.ok(identityRequests > 0, "reload did not request identity");
            assert.equal(await shell.getByTestId("new-chat-landing-input").inputValue(), "");
          } finally {
            releaseIdentity();
          }
          await shell.waitForFunction(
            (message) =>
              document.querySelector('[data-testid="new-chat-landing-input"]')?.value === message,
            savedMessage,
          );
          await shell.unroute("**/v1/me");
          await panel.waitFor({ state: "hidden", timeout: 20_000 });
          await shell.screenshot({ path: path.join(RECORD_DIR, "delayed-identity-reload.png") });
          await shell.getByRole("button", { name: "Expand right panel" }).click();
          await panel.waitFor({ state: "visible", timeout: 20_000 });
          assert.equal(sessionRows(await getJson(`${pod.serverUrl}/v1/sessions`)).length, 0);
          assert.equal(sessionCreateRequests, 0, "reload created a session before Start");
          await shell
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
          await shell.getByRole("tab", { name: "Browser", exact: true }).click();
          await shell.getByRole("tab", { name: "Browser 1", exact: true }).click();
          await shell.waitForFunction(
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

          const browserPaneBounds = await shell
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
          const browserImage = await launched.electronApp.evaluate(async ({ webContents }, url) => {
            const contents = webContents
              .getAllWebContents()
              .find((entry) => entry.getURL() === url);
            return (await contents.capturePage()).toPNG().toString("base64");
          }, demoPage.url);
          fs.writeFileSync(
            path.join(RECORD_DIR, "embedded-browser.png"),
            Buffer.from(browserImage, "base64"),
          );
          for (const edge of ["x", "y", "width", "height"]) {
            assert.ok(
              Math.abs(nativeBrowserView.bounds[edge] - browserPaneBounds[edge]) <= 2,
              `native ${edge} did not match the BrowserPane placeholder`,
            );
          }

          await shell.getByRole("tab", { name: /Agents 0/ }).click();
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
          await shell.screenshot({ path: path.join(RECORD_DIR, "before-start-agents.png") });

          const toggle = shell.getByRole("button", { name: "Collapse right panel" });
          await toggle.click();
          await panel.waitFor({ state: "hidden" });
          await shell.getByRole("button", { name: "Expand right panel" }).click();
          await panel.waitFor({ state: "visible" });
          const widthBefore = (await panel.boundingBox()).width;
          const resizeHandle = shell.getByRole("separator", { name: "Resize panel" });
          await resizeHandle.press("ArrowLeft");
          await resizeHandle.press("ArrowLeft");
          const widthAfter = (await panel.boundingBox()).width;
          assert.ok(widthAfter > widthBefore, `${widthBefore} did not grow after resize`);

          await shell.getByTestId("new-chat-landing-agent-select").click();
          await shell.getByTestId("new-chat-landing-custom-agents").hover();
          await shell.getByText(/hello_world/i, { exact: true }).click();
          const visibleOverlays = shell.locator('[role="menu"]:visible, [role="dialog"]:visible');
          /* oxlint-disable no-await-in-loop */
          for (let attempt = 0; attempt < 4 && (await visibleOverlays.count()) > 0; attempt += 1) {
            await shell.keyboard.press("Escape");
            await shell.waitForTimeout(100);
          }
          /* oxlint-enable no-await-in-loop */
          await shell.waitForFunction(
            () => getComputedStyle(document.body).pointerEvents !== "none",
          );
          assert.equal(
            await visibleOverlays.count(),
            0,
            "agent picker remained visible after dismissal",
          );
          await shell
            .getByTestId("new-chat-landing-input")
            .fill("Deterministic handoff demo (mock response)");
          const startBounds = await shell.getByTestId("new-chat-landing-submit").boundingBox();
          assert.ok(startBounds, "Start button did not have renderer bounds");
          const overlapsBrowserPane = !(
            startBounds.x + startBounds.width <= browserPaneBounds.x ||
            browserPaneBounds.x + browserPaneBounds.width <= startBounds.x ||
            startBounds.y + startBounds.height <= browserPaneBounds.y ||
            browserPaneBounds.y + browserPaneBounds.height <= startBounds.y
          );
          assert.equal(overlapsBrowserPane, false, "native browser bounds overlapped Start");
          fs.writeFileSync(
            path.join(RECORD_DIR, "browser-view-bounds.json"),
            `${JSON.stringify({ browserPaneBounds, nativeBrowserView, startBounds }, null, 2)}\n`,
          );
          await shell.bringToFront();
          startClicked = true;
          await shell.getByTestId("new-chat-landing-submit").click();
          await shell.waitForURL(/\/c\/[^/]+$/, { timeout: 45_000 });
          assert.equal(sessionCreateRequests, 1, "Start did not issue exactly one session POST");
          const sessionId = new URL(shell.url()).pathname.split("/").filter(Boolean).at(-1);
          assert.ok(sessionId);
          const sessions = sessionRows(await getJson(`${pod.serverUrl}/v1/sessions`));
          assert.equal(sessions.length, 1);
          assert.equal(sessions[0].id, sessionId);
          const session = await getJson(
            `${pod.serverUrl}/v1/sessions/${sessionId}?include_items=false`,
          );
          assert.equal(session.host_id, pod.hostId);
          assert.equal(session.workspace, fs.realpathSync(fixtureRepo));
          await shell
            .getByText("DESKTOP_HANDOFF_OK", { exact: true })
            .waitFor({ state: "visible", timeout: 45_000 });

          const sessionPanel = shell.locator("[data-workspace-panel-content]");
          await sessionPanel.waitFor({ state: "visible", timeout: 20_000 });
          const retainedShellTab = shell
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
          await retainedTerminal.pressSequentially(
            "PS1='workspace$ '; clear; printf 'Retained shell marker: %s\\n' \"$OMNIGENT_PRESTART_MARKER\"",
          );
          await retainedTerminal.press("Enter");
          await wait(500);
          await shell.screenshot({ path: path.join(RECORD_DIR, "after-start-retained-shell.png") });

          await shell.getByRole("tab", { name: "Browser", exact: true }).click();
          await shell.getByRole("tab", { name: "Browser 1", exact: true }).click();
          await shell.getByRole("textbox", { name: "Address bar" }).waitFor({ state: "visible" });
          assert.equal(
            await shell.getByRole("textbox", { name: "Address bar" }).inputValue(),
            demoPage.url,
          );
          const adoptedBrowserViewId = `browser-tab:${encodeURIComponent(sessionId)}:${browserTabId}`;
          await shell.waitForFunction(
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
          fs.writeFileSync(
            path.join(RECORD_DIR, "handoff-result.json"),
            JSON.stringify(
              {
                sessionCreateRequests,
                prematureSessionResourceRequests,
                shellMarkerAfterReload: fs.readFileSync(
                  path.join(fixtureRepo, ".desktop-e2e-reload"),
                  "utf8",
                ),
                shellMarkerAfterStart: fs.readFileSync(
                  path.join(fixtureRepo, ".desktop-e2e-retained"),
                  "utf8",
                ),
                provider: "deterministic local mock",
              },
              null,
              2,
            ),
          );
        } catch (error) {
          try {
            const shell = await resolveShellWindow(launched.electronApp, pod.serverUrl, {
              timeoutMs: 2_000,
            });
            await shell.screenshot({ path: path.join(RECORD_DIR, "failure.png"), timeout: 2_000 });
            fs.writeFileSync(
              path.join(RECORD_DIR, "failure-ui.txt"),
              await shell.locator("body").innerText({ timeout: 2_000 }),
            );
          } catch {
            // Preserve the original failure when the window or evidence storage is unavailable.
          }
          throw error;
        } finally {
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
