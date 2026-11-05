// Desktop journeys for handing a browser-pane page to the system browser. Stand-ins:
// an in-process https handler serves Google's sign-in redirect chain, and
// shell.openExternal opens a window showing the URL. Run: see e2e/README.md.

"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { desktopDepsAvailable, launchDesktop, saveRecording } = require("./desktopHarness");

const deps = desktopDepsAvailable();
const skip = deps.ok ? false : `missing deps: ${deps.missing.join(", ")}`;

const DOC_URL = "https://docs.google.com/document/d/omni-launch-plan/edit";
const WIKI_URL = "https://wiki.example.com/onboarding";

// Main-process stand-ins, loaded before main.js. Only the fake hosts are
// served locally; every other request keeps the production policy and network.
const STAND_IN_PRELOAD = `
const { app, BrowserWindow, net, shell } = require("electron");
const mod = require(${JSON.stringify(path.resolve(__dirname, "../src/browserViewRegistry.js"))});

const FAKE_HOSTS = new Set([
  "docs.google.com",
  "accounts.google.com",
  "access.workspace.google.com",
  "wiki.example.com",
]);
const isFake = (url) => {
  try {
    return FAKE_HOSTS.has(new URL(url).hostname);
  } catch {
    return false;
  }
};

// The fake hosts "resolve" to a public address so the DNS policy passes, and
// the in-process handler reports no connected IP, which the stand-in accepts.
const create = mod.createBrowserViewRegistry;
mod.createBrowserViewRegistry = (opts) => {
  const resolveHost = opts.resolveHost;
  const registry = create({
    ...opts,
    resolveHost: (hostname, partition) =>
      FAKE_HOSTS.has(hostname) ? Promise.resolve(["142.250.80.46"]) : resolveHost(hostname, partition),
  });
  const response = registry.shouldCancelAgentResponse.bind(registry);
  registry.shouldCancelAgentResponse = (details, partition) =>
    isFake(details.url) && !details.ip ? Promise.resolve(false) : response(details, partition);
  return registry;
};

const page = (title, body) =>
  new Response(
    "<!doctype html><title>" + title + "</title>" +
      "<body style='font:16px Arial,sans-serif;margin:0;color:#202124'>" + body + "</body>",
    { headers: { "content-type": "text/html; charset=utf-8" } },
  );
const redirect = (location) => new Response(null, { status: 302, headers: { location } });

global.fakeGoogleRequests = [];
function serveFake(request) {
  global.fakeGoogleRequests.push(request.url);
  const url = new URL(request.url);
  if (url.hostname === "docs.google.com") {
    return redirect(
      "https://accounts.google.com/ServiceLogin?service=wise&continue=" +
        encodeURIComponent(url.href),
    );
  }
  if (url.hostname === "accounts.google.com" && url.pathname === "/ServiceLogin") {
    return redirect("https://accounts.google.com/signin/sso?flow=saml");
  }
  if (url.hostname === "accounts.google.com") {
    return redirect("https://access.workspace.google.com/remediate?flow=caa");
  }
  if (url.hostname === "access.workspace.google.com") {
    return page(
      "Access denied",
      "<div style='max-width:560px;margin:56px auto;padding:32px;border:1px solid #dadce0;border-radius:8px'>" +
        "<div style='font-size:13px;color:#5f6368'>Google Workspace (test stand-in)</div>" +
        "<h1 style='font-weight:400;font-size:24px'>Your organization isn't allowing access</h1>" +
        "<p>This application is only accessible through approved browsers.</p>" +
        "<p>Your device isn't meeting one or more of your organization's security requirements.</p>" +
        "</div>",
    );
  }
  return page(
    "Team onboarding",
    "<div style='max-width:640px;margin:48px auto'><h1 style='font-weight:500'>Team onboarding</h1>" +
      "<p>Welcome! Start with the setup checklist below.</p><ol><li>Request accounts</li>" +
      "<li>Install the desktop app</li><li>Join the team channel</li></ol></div>",
  );
}

app.on("session-created", (ses) => {
  ses.protocol.handle("https", (request) =>
    isFake(request.url) ? serveFake(request) : net.fetch(request, { bypassCustomProtocolHandlers: true }),
  );
});

// The user's default browser: a window that shows the URL it was handed.
global.systemBrowserOpened = [];
shell.openExternal = async (url) => {
  global.systemBrowserOpened.push(url);
  const escaped = url.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const win = new BrowserWindow({
    x: 340,
    y: 260,
    width: 760,
    height: 260,
    alwaysOnTop: true,
    title: "Default browser (test stand-in)",
    backgroundColor: "#ffffff",
    webPreferences: { sandbox: true },
  });
  const html =
    "<body style='margin:0;font:15px Arial,sans-serif;background:#fff;color:#202124'>" +
    "<div style='background:#dee1e6;padding:10px 14px;font-weight:600'>Default browser (test stand-in)</div>" +
    "<div style='margin:14px;padding:10px 14px;border-radius:18px;background:#f1f3f4;font-family:monospace'>" +
    escaped + "</div><p style='margin:14px'>Opened by Omnigent in your own browser, where your " +
    "organization's sign-in checks can pass.</p></body>";
  await win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(html));
};
`;

/** Launch the shell on a session page with the browser pane open. */
async function openBrowserPane({ scratch, recordDir }) {
  const { startDesignBackend } = require("./desktopDesignPromptHarness");
  const preload = path.join(scratch, "system-browser-stand-ins.cjs");
  fs.writeFileSync(preload, STAND_IN_PRELOAD);
  const backend = await startDesignBackend(scratch);
  let desktop;
  try {
    desktop = await launchDesktop({ serverUrl: backend.serverUrl, recordDir, preload: [preload] });
    const { window } = desktop;
    await window.goto(`${backend.serverUrl}/c/${backend.sessionId}`);
    await window.getByRole("button", { name: /^(Expand|Collapse) right panel$/ }).waitFor();
    const expand = window.getByRole("button", { name: "Expand right panel", exact: true });
    if (await expand.isVisible()) await expand.click();
    await window.waitForTimeout(1000);
    await window.getByRole("button", { name: "Open new", exact: true }).click();
    await window.waitForTimeout(600);
    await window.getByRole("menuitem", { name: "Browser", exact: true }).click();
    return { backend, desktop };
  } catch (error) {
    try {
      if (desktop) await closeDesktop(desktop, recordDir, null);
    } finally {
      await backend.close();
    }
    throw error;
  }
}

/** Run a journey in a fresh shell; the clip is saved as `clip`, or `failed-<clip>`. */
async function withBrowserPane({ name, clip }, journey) {
  const scratch = fs.mkdtempSync(path.join(os.tmpdir(), `omni-${name}-`));
  const recordDir = recordDirFor(name);
  try {
    const { backend, desktop } = await openBrowserPane({ scratch, recordDir });
    let passed = false;
    try {
      await journey(desktop);
      passed = true;
    } finally {
      try {
        await closeDesktop(desktop, recordDir, passed ? clip : `failed-${clip}`);
      } finally {
        await backend.close();
      }
    }
  } finally {
    fs.rmSync(scratch, { recursive: true, force: true });
  }
}

async function typeAddress(window, url) {
  const bar = window.getByRole("textbox", { name: "Address bar" });
  await bar.click();
  await bar.pressSequentially(url, { delay: 35 });
  await window.waitForTimeout(400);
  await bar.press("Enter");
}

async function waitForOpened(electronApp, url) {
  const { eventually } = require("./desktopDesignPromptHarness");
  await eventually(
    () =>
      electronApp.evaluate(
        (electron, target) =>
          global.systemBrowserOpened.includes(target) &&
          electron.BrowserWindow.getAllWindows().some(
            (win) => win.getTitle().startsWith("Default browser") && !win.webContents.isLoading(),
          ),
        url,
      ),
    `system browser opened ${url}`,
  );
  return electronApp.evaluate(() => global.systemBrowserOpened);
}

async function closeDesktop(desktop, recordDir, name) {
  // Stop filming first so the clip ends on the outcome, not on teardown.
  await desktop.stopDisplayCapture();
  await desktop.electronApp.close();
  if (name) console.log(saveRecording(recordDir, name));
  fs.rmSync(desktop.userDataDir, { recursive: true, force: true });
}

function recordDirFor(name) {
  const root = process.env.OMNIGENT_DESKTOP_RECORD_DIR;
  return root
    ? path.join(root, name)
    : fs.mkdtempSync(path.join(os.tmpdir(), `omni-${name}-recordings-`));
}

it(
  "continues a blocked Google Workspace sign-in in the system browser",
  { skip, timeout: 180_000 },
  () =>
    withBrowserPane({ name: "google-sign-in-handoff", clip: "demo-cuj-1" }, async (desktop) => {
      const { window, electronApp } = desktop;
      await typeAddress(window, DOC_URL);
      const notice = window.getByRole("status").filter({
        hasText: "Google Workspace doesn't allow sign-in from Omnigent's browser",
      });
      await notice.waitFor({ timeout: 30_000 });
      const blockedText = await electronApp.evaluate(async () => {
        const registry = global.omnigentTestBrowserRegistries[0];
        const entry = registry.get(registry.activeConversationId());
        return {
          url: entry.view.webContents.getURL(),
          text: await entry.view.webContents.executeJavaScript("document.body.innerText"),
          trail: entry.navigationTrail,
          requests: global.fakeGoogleRequests
            .map((url) => new URL(url))
            .map((url) => url.host + url.pathname),
        };
      });
      assert.match(blockedText.url, /^https:\/\/access\.workspace\.google\.com\/remediate/);
      assert.match(blockedText.text, /Your organization isn't allowing access/);
      assert.equal(blockedText.trail[0], DOC_URL, JSON.stringify(blockedText.trail));
      assert.deepEqual(blockedText.requests.slice(0, 4), [
        "docs.google.com/document/d/omni-launch-plan/edit",
        "accounts.google.com/ServiceLogin",
        "accounts.google.com/signin/sso",
        "access.workspace.google.com/remediate",
      ]);
      await window.waitForTimeout(3000);

      await window.getByRole("button", { name: "Continue in browser", exact: true }).click();
      assert.deepEqual(await waitForOpened(electronApp, DOC_URL), [DOC_URL]);
      await window.waitForTimeout(3500);
    }),
);

it(
  "opens the current page in the system browser from the toolbar",
  { skip, timeout: 180_000 },
  () =>
    withBrowserPane({ name: "open-in-browser", clip: "demo-cuj-2" }, async (desktop) => {
      const { window, electronApp } = desktop;
      await typeAddress(window, WIKI_URL);
      const { eventually } = require("./desktopDesignPromptHarness");
      await eventually(
        () =>
          electronApp.evaluate(async () => {
            const registry = global.omnigentTestBrowserRegistries[0];
            const wc = registry.get(registry.activeConversationId())?.view.webContents;
            if (!wc || wc.isLoading()) return false;
            return /Team onboarding/.test(await wc.executeJavaScript("document.body.innerText"));
          }),
        "wiki page loaded",
      );
      assert.equal(
        await window
          .getByRole("status")
          .filter({ hasText: "Google Workspace doesn't allow sign-in" })
          .count(),
        0,
      );
      await window.waitForTimeout(2500);

      await window.getByRole("button", { name: "Open in browser", exact: true }).click();
      assert.deepEqual(await waitForOpened(electronApp, WIKI_URL), [WIKI_URL]);
      await window.waitForTimeout(3500);
    }),
);
