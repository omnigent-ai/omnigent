// Desktop e2e: sign-out destroys embedded views, but the tab retains its address
// and title. Reload opens the saved address only when the user requests it.

"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const { desktopDepsAvailable, spawnServer, saveRecording } = require("./desktopHarness");
const {
  launchThisMachineDesktop,
  openSessionBrowserTab,
  openSessionRail,
} = require("./desktopBrowserTabHarness");
const { eventually } = require("./desktopDesignPromptHarness");
const { startFakeOidcIdp, oidcServerEnv } = require("./fixtures/fakeOidcIdp");

const deps = desktopDepsAvailable();
const FAKE_BROWSER = path.join(__dirname, "fixtures", "fakeSystemBrowser.cjs");
const RECORD_DIR =
  process.env.OMNIGENT_DESKTOP_RECORD_DIR ??
  path.join(__dirname, "recordings", "desktop-browser-tab-relogin");
const RECORDING = Boolean(process.env.OMNIGENT_DESKTOP_RECORD_DIR);
const PAGE_TITLE = "Release checklist";

// Holds each state on screen long enough to read in a recording.
const pause = (ms = 2_500) =>
  new Promise((resolve) => {
    setTimeout(resolve, RECORDING ? ms : 0);
  });

/** URL and title of every embedded browser view the shell holds. */
const embeddedPages = (electronApp) =>
  electronApp.evaluate(() => {
    const pages = [];
    for (const registry of global.omnigentTestBrowserRegistries) {
      registry.forEach((entry, id) => {
        const contents = entry.view.webContents;
        pages.push({ id, url: contents.getURL(), title: contents.getTitle() });
      });
    }
    return pages;
  });

async function signIn(window, serverUrl) {
  const urlField = window.locator("#url");
  await urlField.waitFor({ state: "visible", timeout: 15_000 });
  await urlField.fill(serverUrl);
  await window.locator("#connect").click();
  await window.getByText("What should we build?").waitFor({ state: "visible", timeout: 30_000 });
}

it(
  "keeps a Browser tab's address after re-login and opens it on Reload",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}`, timeout: 180_000 },
  async () => {
    const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "omni-browser-relogin-"));
    const fixture = http.createServer((_request, response) => {
      response.writeHead(200, { "Content-Type": "text/html", "Cache-Control": "no-store" });
      response.end(
        `<!doctype html><title>${PAGE_TITLE}</title>` +
          `<body style="font:20px system-ui;padding:24px"><h1>${PAGE_TITLE}</h1>` +
          "<ul><li>Freeze the branch</li><li>Run the smoke tests</li><li>Tag the build</li></ul>",
      );
    });
    await new Promise((resolve) => {
      fixture.listen(0, "127.0.0.1", resolve);
    });
    const pageUrl = `http://localhost:${fixture.address().port}/releases/checklist?view=open`;
    let idp;
    let server;
    let app;
    try {
      idp = await startFakeOidcIdp();
      server = await spawnServer(scratch, { env: (url) => oidcServerEnv(idp, url) });
      // Loopback pages open only in this machine's sessions; the helper reports
      // this machine as the host of `backend.sessionId`, set once it exists.
      const backend = { serverUrl: server.serverUrl, sessionId: null };
      ({ desktop: app } = await launchThisMachineDesktop({
        backend,
        scratch,
        recordDir: RECORD_DIR,
        preload: [FAKE_BROWSER],
      }));
      const { window, electronApp } = app;

      // 1. Sign in, open a session, and load a page in a Browser tab.
      await signIn(window, server.serverUrl);
      const sessionId = await window.evaluate(async () => {
        const agents = await (await fetch("/v1/agents?limit=100")).json();
        const agent = agents.data.find((candidate) => candidate.name === "hello_world");
        if (!agent) throw new Error("the server has no hello_world agent");
        const response = await fetch("/v1/sessions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ agent_id: agent.id }),
        });
        if (!response.ok) throw new Error(`create session: ${response.status}`);
        return (await response.json()).id;
      });
      assert.equal(typeof sessionId, "string");
      backend.sessionId = sessionId;
      await openSessionBrowserTab(window, server.serverUrl, sessionId);
      const bar = window.getByRole("textbox", { name: "Address bar" });
      await bar.fill(pageUrl);
      await bar.press("Enter");
      await eventually(
        async () => (await embeddedPages(electronApp)).some((page) => page.title === PAGE_TITLE),
        "the page to load in the Browser tab",
      );
      await pause();

      // 2. Leave the session and sign out from the profile menu: the shell
      // destroys every embedded view. Signing out from the session page races
      // its polling's 401 sign-in redirect against the shell's connect screen.
      await window.goto(server.serverUrl);
      await window.getByText("What should we build?").waitFor({ state: "visible" });
      await window.getByTestId("sidebar-server-picker").click();
      await window.getByTestId("sidebar-server-sign-out").click();
      await window.locator("#connect").waitFor({ state: "visible", timeout: 15_000 });
      assert.deepEqual(await embeddedPages(electronApp), [], "sign-out kept a browser view");
      await pause();

      // 3. Sign back in: metadata returns while the page stays unloaded.
      await signIn(window, server.serverUrl);
      await openSessionRail(window, server.serverUrl, sessionId);
      await eventually(
        async () => (await bar.inputValue()) === pageUrl,
        "the address bar to show the tab's page after signing back in",
      );
      await window.getByRole("tab", { name: PAGE_TITLE }).waitFor({ timeout: 10_000 });
      await window
        .getByText("Click Reload or press Enter in the address bar to load this page.")
        .waitFor();
      assert.deepEqual(
        await embeddedPages(electronApp),
        [],
        "re-login loaded a page automatically",
      );
      await pause();

      // 4. Explicit Reload uses the normal navigation path to create the view.
      await window.getByRole("button", { name: "Reload", exact: true }).click();
      const reopened = await eventually(
        async () =>
          (await embeddedPages(electronApp)).find(
            (page) => page.url === pageUrl && page.title === PAGE_TITLE,
          ),
        "Reload to open the saved page",
      );
      assert.match(reopened.id, new RegExp(`^browser-tab:${sessionId}:`));
      await window.getByRole("tab", { name: PAGE_TITLE }).waitFor({ timeout: 10_000 });
      await pause(5_000);
    } finally {
      if (app) {
        // Stop filming first, so the clip ends on the journey rather than the window closing.
        await app.stopDisplayCapture().catch(() => {});
        await app.electronApp.close().catch(() => {});
        saveRecording(RECORD_DIR, "browser-tab-relogin");
        fs.rmSync(app.userDataDir, { recursive: true, force: true });
      }
      await new Promise((resolve) => {
        fixture.close(resolve);
      });
      if (server) await server.close();
      if (idp) await idp.close();
      fs.rmSync(scratch, { recursive: true, force: true });
    }
  },
);
