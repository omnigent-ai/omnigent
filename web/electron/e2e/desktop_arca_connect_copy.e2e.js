"use strict";

// Real SPA → Run on Arca → shell-owned Connect Arca console. The Databricks
// gates are patched locally and a stand-in arca replays an auth-required run.
const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  APP_ROOT,
  desktopDepsAvailable,
  launchDesktop,
  saveRecording,
  spawnServer,
} = require("./desktopHarness");

const deps = desktopDepsAvailable();
const SENTINEL = "clipboard-before-copy";
const RECORDING = Boolean(process.env.OMNIGENT_DESKTOP_RECORD_DIR);
const HOLD_MS = RECORDING ? 1500 : 200;
const CONSOLE_PATH = "/arca-connect/index.html";

const HOST_OUTPUT = [
  "Setting up the Omnigent runtime for `isaac omni` (first run, 25s)...",
  "Updated plugin: eng-plugin-builder@eng-plugin-marketplace",
];

function writeStandInArca(bin, argvLog) {
  fs.writeFileSync(
    path.join(bin, "arca"),
    `#!/usr/bin/env node
const fs = require("node:fs");
const args = process.argv.slice(2);
fs.appendFileSync(${JSON.stringify(argvLog)}, JSON.stringify(args) + "\\n");
if (args.includes("host")) {
  const server = (args[args.indexOf("--server") + 1] || "").replaceAll("'", "");
  process.stdout.write(${JSON.stringify(`${HOST_OUTPUT.join("\n")}\n`)});
  process.stderr.write("Error: OMNIGENT_AUTH_REQUIRED: No usable Omnigent login for this server. Run \\\`isaac omni login '" + server + "'\\\` on this machine, then retry.\\n");
}
setTimeout(() => process.exit(1), ${HOLD_MS});
`,
    { mode: 0o755 },
  );
}

/** Enable the Arca host option for the local server and record every context menu shown. */
function writeGatePolicy(file, serverUrl) {
  const src = path.join(APP_ROOT, "src");
  fs.writeFileSync(
    file,
    `const path = require("node:path");
const { Menu } = require("electron");
const src = ${JSON.stringify(src)};
require(path.join(src, "managed_preferences")).getDatabricksInternalFeaturesEnabled = () => true;
const urls = require(path.join(src, "url"));
urls.isDatabricksManagedServerUrl = url => url === ${JSON.stringify(serverUrl)} || url === ${JSON.stringify(`${serverUrl}/`)};
global.omnigentTestMenus = [];
const popup = Menu.prototype.popup;
Menu.prototype.popup = function (options = {}) {
  global.omnigentTestMenus.push({
    url: options.window?.webContents?.getURL?.() ?? null,
    items: this.items.map(item => item.role ?? item.label),
  });
  global.omnigentTestLastMenu = this;
  return popup.call(this, options);
};
`,
  );
}

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

async function showPointer(page, x, y) {
  if (!RECORDING) return;
  await page.evaluate(
    ([left, top]) => {
      let ring = document.getElementById("e2e-pointer");
      if (!ring) {
        ring = document.createElement("div");
        ring.id = "e2e-pointer";
        ring.style.cssText =
          "position:fixed;width:18px;height:18px;margin:-9px 0 0 -9px;border:3px solid #ff4d4f;" +
          "border-radius:50%;pointer-events:none;z-index:2147483647;user-select:none";
        document.body.append(ring);
      }
      ring.style.left = `${left}px`;
      ring.style.top = `${top}px`;
    },
    [x, y],
  );
}

async function pointAt(page, locator) {
  if (!RECORDING) return;
  const box = await locator.boundingBox();
  if (box) await showPointer(page, box.x + box.width / 2, box.y + box.height / 2);
}

it(
  "Connect Arca console lets the user copy its command, status and output",
  {
    skip: deps.ok ? false : `missing deps: ${deps.missing.join(", ")}`,
    timeout: 150_000,
  },
  async (t) => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "omni-arca-copy-"));
    const recordDir = process.env.OMNIGENT_DESKTOP_RECORD_DIR || path.join(root, "recording");
    const server = await spawnServer(root);
    const bin = path.join(root, "bin");
    fs.mkdirSync(bin);
    const argvLog = path.join(root, "arca-argv.jsonl");
    writeStandInArca(bin, argvLog);
    const policy = path.join(root, "arca-policy.cjs");
    writeGatePolicy(policy, server.serverUrl);

    const observed = {};
    let desktop;
    try {
      desktop = await launchDesktop({
        recordDir,
        serverUrl: server.serverUrl,
        preload: [policy],
        env: { PATH: `${bin}${path.delimiter}${process.env.PATH}` },
      });
      const { electronApp, window } = desktop;
      const seedClipboard = () =>
        electronApp.evaluate(({ clipboard }, text) => clipboard.writeText(text), SENTINEL);
      const copiedText = async (deadline = Date.now() + 5_000) => {
        await sleep(100);
        const text = await electronApp.evaluate(({ clipboard }) => clipboard.readText());
        return text !== SENTINEL || Date.now() >= deadline ? text : copiedText(deadline);
      };
      const menuCount = () => electronApp.evaluate(() => global.omnigentTestMenus.length);
      /** Open a context menu, then copy through its Copy item as the user would. */
      const rightClickCopy = async (rightClick) => {
        const before = await menuCount();
        await rightClick();
        await sleep(HOLD_MS * 2);
        const menus = await electronApp.evaluate(
          (_electron, start) => global.omnigentTestMenus.slice(start),
          before,
        );
        await seedClipboard();
        const activated = await electronApp.evaluate(({ BrowserWindow }, start) => {
          if (global.omnigentTestMenus.length === start) return false;
          const menu = global.omnigentTestLastMenu;
          const copy = menu.items.find((item) => item.role === "copy");
          const owner = BrowserWindow.getAllWindows().find((win) =>
            win.webContents.getURL().includes("/arca-connect/"),
          );
          menu.closePopup();
          if (!copy || !owner) return false;
          copy.click(undefined, owner, owner.webContents);
          return true;
        }, before);
        return { menus, copied: activated ? await copiedText() : null };
      };

      const heading = window.getByText("What should we build?");
      await heading.waitFor({ state: "visible", timeout: 60_000 });
      await sleep(HOLD_MS);

      // Control: the app window's own context menu is observable from this harness.
      const composer = window.getByRole("textbox").first();
      await pointAt(window, composer);
      await composer.click();
      await composer.pressSequentially("copy me", { delay: 40 });
      await window.keyboard.press("ControlOrMeta+A");
      const controlBefore = await menuCount();
      await composer.click({ button: "right" });
      await sleep(HOLD_MS * 2);
      const controlMenus = await electronApp.evaluate(
        (_electron, start) => global.omnigentTestMenus.slice(start),
        controlBefore,
      );
      await electronApp.evaluate(() => global.omnigentTestLastMenu?.closePopup());
      assert.ok(
        controlMenus.some(
          (menu) => menu.url?.startsWith(server.serverUrl) && menu.items.includes("copy"),
        ),
        `control: app window context menu was not observed: ${JSON.stringify(controlMenus)}`,
      );
      await window.keyboard.press("Delete");

      const consoleOpened = electronApp.waitForEvent("window", {
        predicate: (page) => page.url().includes(CONSOLE_PATH),
        timeout: 30_000,
      });
      const hostChip = window.getByTestId("new-chat-landing-host-chip");
      await pointAt(window, hostChip);
      await sleep(HOLD_MS / 2);
      await hostChip.click();
      const runOnArca = window.getByTestId("new-chat-landing-run-on-arca");
      await runOnArca.waitFor({ state: "visible", timeout: 15_000 });
      await pointAt(window, runOnArca);
      await sleep(HOLD_MS);
      await runOnArca.click();
      const arcaConsole = await consoleOpened;
      const command = arcaConsole.locator("#command");
      await arcaConsole.waitForFunction(() =>
        document.getElementById("command").textContent.startsWith("arca ssh"),
      );
      await sleep(HOLD_MS);

      // Copy the host command before connecting.
      observed.hostCommand = (await command.textContent()).trim();
      await seedClipboard();
      const commandBox = await command.boundingBox();
      const commandPoint = { x: 40, y: commandBox.height / 2 };
      await showPointer(arcaConsole, commandBox.x + commandPoint.x, commandBox.y + commandPoint.y);
      await command.click({ clickCount: 3, position: commandPoint });
      await sleep(HOLD_MS);
      await arcaConsole.keyboard.press("ControlOrMeta+C");
      observed.commandShortcut = await copiedText();
      observed.commandMenu = await rightClickCopy(() =>
        command.click({ button: "right", position: commandPoint }),
      );

      // Run the connect; the stand-in fails with OMNIGENT_AUTH_REQUIRED, then sign-in fails.
      const confirm = arcaConsole.locator("#confirm");
      await pointAt(arcaConsole, confirm);
      await sleep(HOLD_MS / 2);
      await confirm.click();
      await arcaConsole
        .locator("#cancel", { hasText: "Close" })
        .waitFor({ state: "visible", timeout: 30_000 });
      await arcaConsole.waitForFunction(
        () => document.getElementById("status").dataset.kind === "error",
      );
      observed.arcaCommands = fs
        .readFileSync(argvLog, "utf8")
        .trim()
        .split("\n")
        .map((line) => JSON.parse(line).slice(3, 6).join(" "));
      await sleep(HOLD_MS);

      // Copy the red status line.
      const status = arcaConsole.locator("#status");
      observed.statusText = (await status.textContent()).trim();
      await seedClipboard();
      const statusPoint = { x: 40, y: 8 };
      const statusBox = await status.boundingBox();
      await showPointer(arcaConsole, statusBox.x + statusPoint.x, statusBox.y + statusPoint.y);
      await status.click({ clickCount: 3, position: statusPoint });
      observed.statusSelection = await arcaConsole.evaluate(() =>
        window.getSelection().toString().trim(),
      );
      await sleep(HOLD_MS);
      await arcaConsole.keyboard.press("ControlOrMeta+C");
      observed.statusShortcut = await copiedText();
      observed.statusMenu = await rightClickCopy(() =>
        status.click({ button: "right", position: statusPoint }),
      );

      // Drag-select the OMNIGENT_AUTH_REQUIRED line in the terminal output and copy it.
      const errorRow = arcaConsole
        .locator(".xterm-rows > div", { hasText: "OMNIGENT_AUTH_REQUIRED" })
        .first();
      const rowBox = await errorRow.boundingBox();
      const rowY = rowBox.y + rowBox.height / 2;
      await seedClipboard();
      await showPointer(arcaConsole, rowBox.x + 2, rowY);
      await arcaConsole.mouse.move(rowBox.x + 2, rowY);
      await arcaConsole.mouse.down();
      await arcaConsole.mouse.move(rowBox.x + rowBox.width - 4, rowY, { steps: 12 });
      await arcaConsole.mouse.up();
      await showPointer(arcaConsole, rowBox.x + rowBox.width - 4, rowY);
      observed.outputSelection = await arcaConsole.evaluate(() => term.getSelection());
      await sleep(HOLD_MS);
      await arcaConsole.keyboard.press("ControlOrMeta+C");
      observed.outputShortcut = await copiedText();
      observed.outputMenu = await rightClickCopy(() =>
        arcaConsole.mouse.click(rowBox.x + 80, rowY, { button: "right" }),
      );

      // Close the console and paste the copied output into the composer.
      const consoleClosed = arcaConsole.waitForEvent("close");
      // Close can destroy the window before Playwright finishes the click.
      await arcaConsole
        .locator("#cancel")
        .click()
        .catch((error) => {
          if (!arcaConsole.isClosed()) throw error;
        });
      await consoleClosed;
      await pointAt(window, composer);
      await composer.click();
      await window.keyboard.press("ControlOrMeta+V");
      observed.pasted = await composer.evaluate((el) => ("value" in el ? el.value : el.innerText));
      await sleep(HOLD_MS * 2);
    } finally {
      t.diagnostic(`observed ${JSON.stringify(observed)}`);
      if (desktop) {
        // Stop the display capture first so the clip ends on the pasted output, not app teardown.
        await desktop.stopDisplayCapture();
        await desktop.electronApp.close().catch(() => {});
        const saved = saveRecording(recordDir, "arca-connect-copy");
        t.diagnostic(`recordings ${JSON.stringify(saved)}`);
        fs.rmSync(desktop.userDataDir, { recursive: true, force: true });
      }
      await server.close();
      fs.rmSync(root, { recursive: true, force: true });
    }

    assert.deepEqual(observed.arcaCommands, ["isaac omni host", "isaac omni login"]);
    const failures = [];
    const check = (label, { shortcut, menu }, expected) => {
      if (shortcut !== expected) {
        failures.push(
          `${label}: shortcut copied ${JSON.stringify(shortcut)}, expected ${JSON.stringify(expected)}`,
        );
      }
      const consoleMenus = menu.menus.filter((shown) => shown.url?.includes(CONSOLE_PATH));
      if (!consoleMenus.some((shown) => shown.items.includes("copy"))) {
        failures.push(`${label}: right-click showed no Copy menu`);
      } else if (menu.copied !== expected) {
        failures.push(
          `${label}: menu Copy copied ${JSON.stringify(menu.copied)}, expected ${JSON.stringify(expected)}`,
        );
      }
    };
    check(
      "command",
      { shortcut: observed.commandShortcut, menu: observed.commandMenu },
      observed.hostCommand,
    );
    if (observed.statusSelection !== observed.statusText) {
      failures.push(`status: selection ${JSON.stringify(observed.statusSelection)}`);
    }
    check(
      "status",
      { shortcut: observed.statusShortcut, menu: observed.statusMenu },
      observed.statusText,
    );
    assert.match(observed.outputSelection, /^Error: OMNIGENT_AUTH_REQUIRED/);
    check(
      "output",
      { shortcut: observed.outputShortcut, menu: observed.outputMenu },
      observed.outputSelection,
    );
    if (observed.pasted !== observed.outputSelection) {
      failures.push(`output: composer received ${JSON.stringify(observed.pasted)}`);
    }
    assert.deepEqual(failures, []);
  },
);
