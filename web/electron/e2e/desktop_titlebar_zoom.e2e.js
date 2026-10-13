"use strict";

const { it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { desktopDepsAvailable, spawnServer, launchDesktop } = require("./desktopHarness");

const deps = desktopDepsAvailable();

it(
  "keeps macOS titlebar clearance in window coordinates across page zoom and reload",
  {
    skip: process.platform !== "darwin" || !deps.ok,
    timeout: 120_000,
  },
  async () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "omnigent-titlebar-zoom-"));
    let server;
    let app;
    try {
      server = await spawnServer(tmpDir);
      app = await launchDesktop({
        serverUrl: server.serverUrl,
        recordDir: process.env.OMNIGENT_DESKTOP_RECORD_DIR || path.join(tmpDir, "recordings"),
      });
      const { window: page, electronApp } = app;
      const toolbar = page.locator(".electron-sidebar-header-actions");
      await toolbar.waitFor();
      await electronApp.evaluate(({ BrowserWindow }) => {
        const win = BrowserWindow.getAllWindows().find((w) =>
          w.webContents.getURL().startsWith("http"),
        );
        win.setFocusable(false);
        win.showInactive();
        win.setContentSize(1600, 900);
      });

      const setZoom = async (zoom) => {
        await electronApp.evaluate(({ BrowserWindow }, factor) => {
          BrowserWindow.getAllWindows()
            .find((w) => w.webContents.getURL().startsWith("http"))
            .webContents.setZoomFactor(factor);
        }, zoom);
      };
      const checkClearance = async (zoom) => {
        await page.waitForFunction((factor) => {
          const bounds = document
            .querySelector(".electron-sidebar-header-actions")
            ?.getBoundingClientRect();
          return bounds && Math.abs(bounds.x * factor - 88) < 0.1;
        }, zoom);
        const box = await toolbar.boundingBox();
        assert.ok(Math.abs((box.y + box.height / 2) * zoom - 24) < 0.1);
        const strip = await page.locator(".electron-drag-strip").boundingBox();
        assert.ok(Math.abs(strip.height * zoom - 48) < 0.1);
      };

      /* eslint-disable no-await-in-loop -- Zoom and sidebar actions share one window. */
      for (const rootSize of [14, 16]) {
        await page.evaluate((size) => {
          document.documentElement.style.fontSize = `${size}px`;
        }, rootSize);
        for (const zoom of [0.5, 0.67, 0.8, 1, 1.1, 1.25, 1.5]) {
          await setZoom(zoom);
          await checkClearance(zoom);
          await toolbar.getByRole("button", { name: "Close sidebar", exact: true }).click();
          await checkClearance(zoom);
          await toolbar.getByRole("button", { name: "Open sidebar", exact: true }).click();
        }
      }
      /* eslint-enable no-await-in-loop */

      await setZoom(0.8);
      await page.reload();
      await toolbar.waitFor();
      await checkClearance(0.8);
      await toolbar.getByRole("link", { name: "Settings", exact: true }).click();
      const back = page.locator(".settings-sidebar-header a");
      await back.waitFor();
      assert.ok((await back.boundingBox()).y * 0.8 >= 48);
    } finally {
      if (app) {
        await app.electronApp.close();
        await app.stopDisplayCapture();
        fs.rmSync(app.userDataDir, { recursive: true, force: true });
      }
      if (server) await server.close();
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  },
);
