// Desktop-shell e2e: where a plain click on a chat-response link opens.
//
// Default: a bare `target="_blank"` chat link rides the shell's
// setWindowOpenHandler -> decideWindowOpen -> shell.openExternal, landing in
// the user's default OS browser, and nothing attaches to the in-app browser
// pane. Settings (General -> Links) offers an "Open links in the in-app
// browser" toggle; once enabled, a plain click routes the link into the
// conversation's embedded browser view (a WebContentsView attaches) and
// never calls shell.openExternal, while a ctrl/cmd-click keeps the
// external-open behavior.
//
// Journey: boot the shell into a seeded conversation whose assistant reply
// contains a web link -> plain-click it (default: external) -> enable the
// toggle in Settings -> back in the conversation, ctrl-click (still
// external) then plain-click (in-app view attaches, no external call).
//
// Run from web/electron after building the SPA:
//   OMNIGENT_PW_NO_SANDBOX=1 OMNIGENT_PYTHON=<venv python> \
//     xvfb-run -a node --test e2e/desktop_link_opens_in_app_browser.e2e.js

"use strict";

const { describe, it, before, after } = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  REPO_ROOT,
  desktopDepsAvailable,
  spawnServer,
  launchDesktop,
  saveRecording,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();
const RECORD_DIR = path.join(__dirname, "recordings", "desktop-link-opens-in-app-browser");

// The external link the seeded assistant message links to. Loopback so a click
// never reaches the public internet; the point is only WHERE the click routes.
const LINK_URL = "http://127.0.0.1:9/in-app-docs";
const PYTHON = process.env.OMNIGENT_PYTHON || "python3";

// Inline seeding: create a hello_world session via the server API and append a
// committed user+assistant exchange whose assistant reply is a markdown link.
// No runner is needed to render a settled transcript. Prints the session id.
const SEED_SCRIPT = `
import json, sys, httpx
from omnigent.entities import MessageData, NewConversationItem
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.e2e_ui.conftest import _build_hello_world_bundle
base_url, db_uri, link = sys.argv[1], sys.argv[2], sys.argv[3]
r = httpx.post(
    f"{base_url}/v1/sessions",
    data={"metadata": json.dumps({})},
    files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
    timeout=30.0,
)
r.raise_for_status()
sid = r.json()["session_id"]
SqlAlchemyConversationStore(db_uri).append(sid, [
    NewConversationItem(type="message", response_id="resp_seed_link",
        data=MessageData(role="user", content=[{"type": "input_text", "text": "share the docs link"}])),
    NewConversationItem(type="message", response_id="resp_seed_link",
        data=MessageData(role="assistant",
            content=[{"type": "output_text", "text": f"Here are the docs: [{link}]({link})"}],
            agent="hello_world")),
])
print(sid)
`;

function seedLinkSession(serverUrl, dbUri) {
  const res = spawnSync(PYTHON, ["-c", SEED_SCRIPT, serverUrl, dbUri, LINK_URL], {
    cwd: REPO_ROOT,
    env: { ...process.env, PYTHONPATH: REPO_ROOT },
    encoding: "utf8",
  });
  if (res.status !== 0) {
    throw new Error(`seed script failed (status ${res.status}):\n${res.stdout}\n${res.stderr}`);
  }
  const sid = res.stdout.trim().split(/\s+/).pop();
  assert.match(sid, /^[0-9a-f]{16,}$/, `unexpected session id from seed script: ${res.stdout}`);
  return sid;
}

/**
 * Resolve the SHELL window's Playwright page: firstWindow() can hand back an
 * auxiliary window, so pick the page whose URL is the http(s) app page.
 */
async function shellWindow(electronApp, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const hit = electronApp.windows().find((p) => /^https?:/.test(p.url()));
    if (hit) return hit;
    if (Date.now() > deadline) {
      throw new Error(
        `no http(s) shell window appeared — windows: ${electronApp
          .windows()
          .map((p) => p.url())
          .join(", ")}`,
      );
    }
    // oxlint-disable-next-line no-await-in-loop -- Poll until the window exists.
    await new Promise((resolve) => {
      setTimeout(resolve, 500);
    });
  }
}

/**
 * Count native child views attached to the SHELL window's contentView. The
 * in-app browser pane attaches exactly one WebContentsView while painting, so
 * a rise above baseline means the link opened in the in-app browser.
 */
async function attachedChildViewCount(electronApp) {
  return electronApp.evaluate(({ BrowserWindow }) => {
    const win = BrowserWindow.getAllWindows().find(
      (w) => !w.isDestroyed() && /^https?:/.test(w.webContents.getURL()),
    );
    if (!win) return -1;
    return win.contentView.children.length;
  });
}

/** Wrap shell.openExternal in the main process so we can see if a click routed
 *  the link to the external OS browser. Read the record via readExternal. */
async function instrumentOpenExternal(electronApp) {
  await electronApp.evaluate(({ shell }) => {
    globalThis.externalOpenRecord = [];
    shell.openExternal = (url) => {
      globalThis.externalOpenRecord.push(url);
      // Don't actually launch a browser under CI; just record the intent.
      return Promise.resolve();
    };
  });
}

async function readExternal(electronApp) {
  return electronApp.evaluate(() => globalThis.externalOpenRecord || []);
}

describe(
  "desktop shell — chat links: external by default, in-app once opted in",
  { skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}` },
  () => {
    let tmpDir;
    let server;
    let sessionId;

    before(async () => {
      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omni-desktop-link-"));
      server = await spawnServer(tmpDir);
      const dbUri = `sqlite:///${path.join(tmpDir, "test.db")}`;
      sessionId = seedLinkSession(server.serverUrl, dbUri);
    });

    after(async () => {
      if (server) await server.close();
      if (tmpDir) fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it(
      "routes a plain click in-app once the setting is on; external otherwise",
      { timeout: 300_000 },
      async () => {
        const { electronApp, userDataDir, stopDisplayCapture } = await launchDesktop({
          recordDir: RECORD_DIR,
          serverUrl: server.serverUrl,
        });

        let baseline;
        let externalAfterDefaultClick;
        let externalAfterModifiedClick;
        let externalFinal;
        let attachedAfterOptedInClick;
        try {
          const shell = await shellWindow(electronApp);
          await shell
            .getByText("What should we build?")
            .waitFor({ state: "visible", timeout: 90_000 })
            .catch(() => {
              /* the shell may already be past home; the nav below is what matters */
            });

          // Open the seeded conversation that holds the link.
          await shell.goto(`${server.serverUrl}/c/${sessionId}`);
          const chatLink = shell.getByRole("link", { name: /in-app-docs/ });
          await chatLink.waitFor({ state: "visible", timeout: 60_000 });

          await instrumentOpenExternal(electronApp);
          baseline = await attachedChildViewCount(electronApp);

          // Default (setting off): a plain click stays on the external path.
          await chatLink.click();
          await shell.waitForTimeout(2_000);
          externalAfterDefaultClick = await readExternal(electronApp);

          // Opt in through the real Settings control (General -> Links).
          await shell.goto(`${server.serverUrl}/settings/general`);
          const toggle = shell.getByTestId("open-links-in-app-toggle");
          await toggle.waitFor({ state: "visible", timeout: 60_000 });
          await toggle.click();
          await shell.waitForTimeout(500);

          await shell.goto(`${server.serverUrl}/c/${sessionId}`);
          await chatLink.waitFor({ state: "visible", timeout: 60_000 });

          // A modified click keeps the external-open behavior.
          await chatLink.click({ modifiers: ["ControlOrMeta"] });
          await shell.waitForTimeout(2_000);
          externalAfterModifiedClick = await readExternal(electronApp);

          // A plain click now opens the in-app browser pane, on film.
          await chatLink.click();
          await shell.waitForTimeout(3_000);
          externalFinal = await readExternal(electronApp);
          attachedAfterOptedInClick = await attachedChildViewCount(electronApp);
          console.log(
            `[in-app-link] baseline=${baseline} afterOptedInClick=${attachedAfterOptedInClick} ` +
              `external=${JSON.stringify(externalFinal)}`,
          );
        } finally {
          await electronApp.close();
          await stopDisplayCapture();
          saveRecording(RECORD_DIR, "after-link-in-app");
          fs.rmSync(userDataDir, { recursive: true, force: true });
        }

        assert.deepEqual(
          externalAfterDefaultClick,
          [LINK_URL],
          "with the setting off, a plain click must keep opening externally",
        );
        assert.deepEqual(
          externalAfterModifiedClick,
          [LINK_URL, LINK_URL],
          "a ctrl/cmd-click must keep opening externally even with the setting on",
        );
        assert.deepEqual(
          externalFinal,
          [LINK_URL, LINK_URL],
          "with the setting on, a plain click must not reach shell.openExternal",
        );
        assert.ok(
          attachedAfterOptedInClick > baseline,
          `the in-app browser did not open on a plain chat-link click ` +
            `(attached views ${baseline} -> ${attachedAfterOptedInClick})`,
        );
      },
    );
  },
);
