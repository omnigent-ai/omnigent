"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { desktopDepsAvailable, saveRecording } = require("./desktopHarness");
const { startDesignBackend, eventually } = require("./desktopDesignPromptHarness");
const { launchThisMachineDesktop, openSessionBrowserTab } = require("./desktopBrowserTabHarness");
const { writeChromeProfile } = require("./fixtures/chromeProfileFixture");
const { startSsoSites } = require("./fixtures/ssoSiteFixture");

const deps = desktopDepsAvailable();
const FIXTURES = path.join(__dirname, "fixtures");
const RECORDING = Boolean(process.env.OMNIGENT_DESKTOP_RECORD_DIR);
const sqliteMissing = !fs.existsSync("/usr/bin/sqlite3") && "missing /usr/bin/sqlite3";

it(
  "signs in to a workspace with Chrome's cookies imported from its identity provider redirect",
  {
    skip: (!deps.ok && `missing deps: ${deps.missing.join(", ")}`) || sqliteMissing,
    timeout: 150_000,
  },
  async () => {
    const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "omni-chrome-cookie-sign-in-"));
    const recordDir = RECORDING
      ? process.env.OMNIGENT_DESKTOP_RECORD_DIR
      : path.join(scratch, "recordings");
    let sites;
    let backend;
    let desktop;
    try {
      // Chrome is signed in to the workspace; its IdP cookie alone does not skip the security key.
      writeChromeProfile(path.join(scratch, "machine"), [
        { hostKey: "workspace.localhost", name: "workspace_session", value: "chrome-workspace" },
        { hostKey: "idp.localhost", name: "idp_device", value: "chrome-device" },
      ]);
      sites = await startSsoSites({ workspaceSessions: ["chrome-workspace"] });
      backend = await startDesignBackend(scratch);
      ({ desktop } = await launchThisMachineDesktop({
        backend,
        scratch,
        recordDir,
        preload: [
          path.join(FIXTURES, "fakeSystemBrowser.cjs"),
          path.join(FIXTURES, "macChromeCookieImport.cjs"),
        ],
      }));
      const { window, electronApp } = desktop;
      const tabPage = () =>
        electronApp.evaluate(async () => {
          const registry = global.omnigentTestBrowserRegistries[0];
          const contents = registry.get(registry.activeConversationId())?.view.webContents;
          if (!contents) return null;
          const heading = await contents
            .executeJavaScript("document.querySelector('h1, h2')?.textContent ?? null")
            .catch(() => null);
          const cookies = await contents.session.cookies.get({});
          return {
            url: contents.getURL(),
            heading,
            cookies: cookies.map(({ name }) => name).sort(),
          };
        });

      await openSessionBrowserTab(window, backend.serverUrl, backend.sessionId);
      const bar = window.getByRole("textbox", { name: "Address bar" });
      await bar.fill(sites.workspaceUrl);
      await bar.press("Enter");
      await eventually(
        async () => (await tabPage())?.url.startsWith(sites.idpAuthorizePrefix),
        "the workspace's redirect to its identity provider",
        20_000,
      );

      await window.getByRole("button", { name: "Import cookies from Chrome" }).click();
      const dialog = window.getByRole("dialog", { name: "Import cookies from Chrome" });
      await dialog.getByRole("button", { name: "Import cookies", exact: true }).click();

      const signedIn = await eventually(
        async () => (await tabPage())?.heading === "Signed in to the workspace",
        "the workspace's signed-in page",
        20_000,
      ).catch(() => false);
      const landing = await tabPage();
      assert.ok(
        signedIn,
        `expected the workspace signed in, got ${JSON.stringify(landing)}; ` +
          `requests ${JSON.stringify(sites.requests)}`,
      );
      assert.equal(landing?.url, sites.workspaceUrl);
    } finally {
      try {
        if (desktop) {
          await desktop.window.unrouteAll({ behavior: "wait" }).catch(() => {});
          await desktop.electronApp.close().catch(() => {});
          await desktop.stopDisplayCapture().catch(() => {});
          console.log(saveRecording(recordDir, "browser-chrome-cookie-sign-in"));
          fs.rmSync(desktop.userDataDir, { recursive: true, force: true });
        }
      } finally {
        if (backend) await backend.close();
        if (sites) await sites.close();
        fs.rmSync(scratch, { recursive: true, force: true });
      }
    }
  },
);
